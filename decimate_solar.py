#!/usr/bin/env python3
"""Thin long stretches of zero production in the solar-log archive.

Samples with wNow above 0 stay as they are. A run of local zeros that lasts
at least one 15-minute block is reduced to the first sample of each block,
plus the last sample of the run. That keeps the moment production stops, the
moment it returns, and proof that the array stayed at zero overnight or
through a blockage. Shorter dips stay intact. Enlighten rows stay too: they
are already one per 15 minutes, and a later merge rebuilds them from the
cloud archive.

The first run reads every solar-log file. Later runs read the last 48 hours,
or back to the previous run when that was longer ago. Today's file is rewritten
under the same lock the scraper uses for an append, and the last line stays
the newest sample.

Usage:
  python3 decimate_solar.py --dry-run   # report the first full pass
  python3 decimate_solar.py             # rewrite; later calls look back 48 hours
  python3 decimate_solar.py --all       # scan the whole archive again
  python3 decimate_solar.py --hours 48
  python3 decimate_solar.py --self-test
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from enphase_common import data_root, load_config, log, solar_log_lock
from merge_enlighten import (
    SOURCE,
    is_number,
    log_path,
    parse_records,
    read_text,
    write_records,
)

BIN_SECONDS = 15 * 60
LOOKBACK_HOURS = 48
NAME = re.compile(r"^solar-log-(\d{4})-(\d{2})-(\d{2})\.json(?:\.gz)?$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Thin long zero stretches in the solar-log archive")
    parser.add_argument("--all", action="store_true", help="read every solar-log file")
    parser.add_argument(
        "--hours",
        type=float,
        default=None,
        help=f"look back this many hours instead of {LOOKBACK_HOURS} (or the whole archive on a first run)",
    )
    parser.add_argument("--dry-run", action="store_true", help="report changes without rewriting files")
    parser.add_argument("--self-test", action="store_true", help="run offline checks and exit")
    return parser.parse_args()


def is_local_zero(row: dict) -> bool:
    """A scraper sample at exactly 0 W with a usable timestamp."""
    if row.get("source") == SOURCE:
        return False
    if not is_number(row.get("wNow")) or row.get("wNow") != 0:
        return False
    return is_number(row.get("readingTime"))


def decimate_rows(rows: list[dict], bin_seconds: int = BIN_SECONDS) -> list[dict]:
    """Drop extra local zeros inside a long zero stretch.

    One kept sample covers each `bin_seconds` block. The last sample of the
    stretch is always kept, so an open day's tail stays the newest reading.
    """
    if bin_seconds < 1:
        raise ValueError("bin_seconds must be at least 1")
    keep = [True] * len(rows)
    index = 0
    count = len(rows)
    while index < count:
        if not is_local_zero(rows[index]):
            index += 1
            continue
        end = index + 1
        while end < count and is_local_zero(rows[end]):
            end += 1
        span = rows[end - 1]["readingTime"] - rows[index]["readingTime"]
        if span >= bin_seconds:
            for offset in _dropped_offsets(rows[index:end], bin_seconds):
                keep[index + offset] = False
        index = end
    return [row for row, wanted in zip(rows, keep) if wanted]


def _dropped_offsets(run: list[dict], bin_seconds: int) -> list[int]:
    """Indexes inside one zero run that are safe to remove."""
    chosen = {0, len(run) - 1}
    seen_bins: set[int] = set()
    for index, row in enumerate(run):
        bin_id = int(row["readingTime"] // bin_seconds)
        if bin_id not in seen_bins:
            chosen.add(index)
            seen_bins.add(bin_id)
    return [index for index in range(len(run)) if index not in chosen]


def file_day(path: Path) -> date | None:
    match = NAME.match(path.name)
    if match is None:
        return None
    return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))


def solar_files(root: Path, start: date | None, end: date) -> list[tuple[date, Path]]:
    """Solar-log files from `start` through `end`. `start` None means all of them."""
    found: dict[date, Path] = {}
    if start is None:
        paths = root.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/solar-log-*.json*")
        for path in paths:
            day = file_day(path)
            if day is None:
                continue
            chosen = log_path(root, day)
            if chosen is not None:
                found[day] = chosen
    else:
        day = start
        while day <= end:
            chosen = log_path(root, day)
            if chosen is not None:
                found[day] = chosen
            day += timedelta(days=1)
    return sorted(found.items())


def window_start(
    now: datetime,
    state: dict,
    *,
    all_files: bool,
    hours: float | None,
) -> datetime | None:
    """None means the whole archive.

    An explicit `--hours` is that far back. After a saved run, the window is
    48 hours or back to that run, whichever reaches further.
    """
    if all_files:
        return None
    if hours is not None:
        if hours <= 0:
            raise SystemExit("--hours must be greater than 0")
        return now - timedelta(hours=hours)
    raw = state.get("last_run")
    if not raw:
        return None
    try:
        previous = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if previous.tzinfo is None and now.tzinfo is not None:
        previous = previous.replace(tzinfo=now.tzinfo)
    recent = now - timedelta(hours=LOOKBACK_HOURS)
    return min(recent, previous)


def state_path() -> Path:
    return Path(__file__).resolve().parent / "secrets" / "decimate-state.json"


def load_state(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log(f"{path.name} is not readable JSON; this run scans the whole archive")
        return {}
    return loaded if isinstance(loaded, dict) else {}


def save_state(path: Path, when: datetime) -> None:
    path.parent.mkdir(mode=0o700, exist_ok=True)
    payload = {"last_run": when.isoformat(timespec="seconds")}
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def decimate_path(path: Path, *, today: date, dry_run: bool) -> dict:
    """Rewrite one file. Today's file is locked for the read and the replace."""

    def once() -> dict:
        text = read_text(path)
        if text is None:
            return {"kind": "unreadable", "before": 0, "after": 0}
        records = parse_records(text, path)
        if records is None:
            return {"kind": "unreadable", "before": 0, "after": 0}
        kept = decimate_rows(records)
        before = len(records)
        after = len(kept)
        if after == before:
            return {"kind": "unchanged", "before": before, "after": after}
        if not dry_run:
            write_records(path, kept)
        return {"kind": "updated", "before": before, "after": after}

    if file_day(path) == today:
        with solar_log_lock():
            return once()
    return once()


def run_decimate(root: Path, *, now: datetime, state: dict, args: argparse.Namespace) -> dict:
    start = window_start(now, state, all_files=args.all, hours=args.hours)
    today = now.date()
    start_day = None if start is None else start.date()
    files = solar_files(root, start_day, today)
    if start is None:
        log(f"scanning {len(files)} solar-log files")
    else:
        log(f"scanning {len(files)} solar-log files since {start_day.isoformat()}")
    updated = unchanged = unreadable = 0
    before = after = 0
    for _day, path in files:
        outcome = decimate_path(path, today=today, dry_run=args.dry_run)
        kind = outcome["kind"]
        if kind == "updated":
            updated += 1
        elif kind == "unchanged":
            unchanged += 1
        else:
            unreadable += 1
            log(f"skip {path}")
        before += outcome["before"]
        after += outcome["after"]
    removed = before - after
    mode = "dry run " if args.dry_run else ""
    log(
        f"{mode}decimate files={len(files)} updated={updated} unchanged={unchanged} "
        f"unreadable={unreadable} rows={before} kept={after} removed={removed}"
    )
    wrote = not args.dry_run and (updated or unchanged)
    return {
        "files": len(files),
        "updated": updated,
        "unchanged": unchanged,
        "unreadable": unreadable,
        "before": before,
        "after": after,
        "removed": removed,
        "save_state": wrote,
    }


def self_test() -> None:
    def row(at: int, watts: float, source: str | None = None) -> dict:
        record = {"readingTime": at, "wNow": watts}
        if source is not None:
            record["source"] = source
        return record

    # Production, a brief zero, and a sample with no watts stay.
    brief = [row(0, 10), row(30, 0), row(60, 0), row(90, 4), row(120, None)]
    assert decimate_rows(brief) == brief

    # Two hours of 30-second zeros become one sample per 15 minutes, plus the end.
    start = 1_700_000_000
    night = [row(start + offset, 0) for offset in range(0, 2 * 3600 + 1, 30)]
    thinned = decimate_rows(night)
    assert thinned[0] == night[0]
    assert thinned[-1] == night[-1]
    assert len(thinned) < len(night) / 10
    gaps = [thinned[index + 1]["readingTime"] - thinned[index]["readingTime"] for index in range(len(thinned) - 1)]
    assert max(gaps) <= BIN_SECONDS
    assert decimate_rows(thinned) == thinned

    # A producing sample splits the night. Both sides keep their own edges.
    noon = start + 3600
    split = night[:60] + [row(noon, 25)] + night[60:]
    kept = decimate_rows(split)
    assert any(item.get("wNow") == 25 for item in kept)
    assert kept[0]["readingTime"] == night[0]["readingTime"]
    assert kept[-1] == night[-1]

    # Cloud rows are already one bin apart and are left alone.
    cloud = [row(start + index * BIN_SECONDS, 0, SOURCE) for index in range(8)]
    assert decimate_rows(cloud) == cloud

    # The zero just before production returns is the end of the stretch.
    edge = [row(start + offset, 0) for offset in range(0, 3600, 30)]
    edge.append(row(start + 3600, 3))
    kept = decimate_rows(edge)
    assert kept[-2]["wNow"] == 0
    assert kept[-2]["readingTime"] == edge[-2]["readingTime"]
    assert kept[-1]["wNow"] == 3

    now = datetime(2026, 11, 4, 9, 0, tzinfo=datetime.now().astimezone().tzinfo)
    assert window_start(now, {}, all_files=False, hours=None) is None
    assert window_start(now, {}, all_files=True, hours=24) is None
    exact = window_start(now, {"last_run": "2026-11-04T08:00:00"}, all_files=False, hours=6)
    assert exact == now - timedelta(hours=6)
    recent = window_start(now, {"last_run": now.isoformat()}, all_files=False, hours=None)
    assert recent == now - timedelta(hours=LOOKBACK_HOURS)
    missed = window_start(now, {"last_run": "2026-10-28T09:00:00"}, all_files=False, hours=None)
    assert missed == datetime.fromisoformat("2026-10-28T09:00:00").replace(tzinfo=now.tzinfo)
    try:
        window_start(now, {}, all_files=False, hours=0)
    except SystemExit:
        pass
    else:
        raise AssertionError("zero hours should be rejected")

    print("decimate_solar self-test ok")


def main() -> None:
    args = parse_args()
    if args.self_test:
        self_test()
        return
    now = datetime.now().astimezone()
    root = data_root(load_config())
    path = state_path()
    outcome = run_decimate(root, now=now, state=load_state(path), args=args)
    if outcome["save_state"]:
        save_state(path, now)


if __name__ == "__main__":
    main()
