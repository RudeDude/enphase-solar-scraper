#!/usr/bin/env python3
"""Merge Enlighten 15-minute history into the solar-log files.

Local samples stay as they are. A cloud bin is inserted only when that
15-minute window has no local production reading. Inserted rows use the
original production-meter keys so the status script can keep reading
`.wNow` and the latest-sample fields. Rows are tagged `"source": "enlighten"`
and replaced on the next run.

Today's file is still being appended. Bins that end after the latest local
reading are left out, so the last line stays a live sample. Closed days are
filled through the final bin, including the stretch after the last local sample.

Usage:
  python3 merge_enlighten.py
  python3 merge_enlighten.py --self-test
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from enphase_common import data_root, load_config, log, solar_log_lock

SITE_TZ = ZoneInfo("America/New_York")
SOURCE = "enlighten"
READING_TIME = re.compile(r'"readingTime"\s*:\s*(-?\d+(?:\.\d+)?)')
SOURCE_MARK = re.compile(r'"source"\s*:\s*"enlighten"')
REQUIRED_KEYS = (
    "activeCount",
    "measurementType",
    "wNow",
    "rmsCurrent",
    "rmsVoltage",
    "reactPwr",
    "apprntPwr",
    "whToday",
    "vahToday",
)


@dataclass(frozen=True)
class CloudDay:
    day: date
    start: int
    interval: int
    bins: tuple[float, ...]

    def edges(self, index: int) -> tuple[int, int]:
        start = self.start + index * self.interval
        return start, start + self.interval


def whole_number(value: float) -> int | float:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def interval_watts(watt_hours: float, seconds: int) -> int | float:
    if seconds <= 0:
        return 0
    if seconds == 900 and isinstance(watt_hours, int):
        return watt_hours * 4
    return whole_number(watt_hours * 3600 / seconds)


def synthetic_record(
    reading_time: int,
    watts: int | float,
    wh_today: int | float,
    wh_lifetime: int | float,
    wh_seven: int | float,
) -> dict:
    """Original eim production object, plus the tag that makes a rerun safe."""
    return {
        "type": "eim",
        "activeCount": 1,
        "measurementType": "production",
        "readingTime": reading_time,
        "wNow": watts,
        "whLifetime": wh_lifetime,
        "varhLeadLifetime": None,
        "varhLagLifetime": None,
        "vahLifetime": None,
        "rmsCurrent": None,
        "rmsVoltage": None,
        "reactPwr": None,
        "apprntPwr": None,
        "pwrFactor": None,
        "whToday": wh_today,
        "whLastSevenDays": wh_seven,
        "vahToday": None,
        "varhLeadToday": None,
        "varhLagToday": None,
        "source": SOURCE,
    }


def is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_local_production(record: dict) -> bool:
    if record.get("source") == SOURCE:
        return False
    if record.get("type") not in (None, "eim"):
        return False
    if record.get("measurementType") not in (None, "production"):
        return False
    return is_number(record.get("readingTime"))


def uncovered_indexes(times: list[float], cloud: CloudDay, *, open_day: bool, now: float) -> list[int]:
    """Bins with no local reading in [start, end).

    On an open day, also skip a bin that ends after the latest local reading
    (or after now, when the day has no local readings yet). The scraper appends
    in file order, and the status script prints the last line.
    """
    ordered = sorted(times)
    last = ordered[-1] if ordered else None
    cursor = 0
    missing = []
    count = len(ordered)
    for index in range(len(cloud.bins)):
        start, end = cloud.edges(index)
        while cursor < count and ordered[cursor] < start:
            cursor += 1
        if cursor < count and ordered[cursor] < end:
            continue
        if open_day:
            limit = last if last is not None else now
            if end > limit:
                continue
        missing.append(index)
    return missing


def build_synthetics(
    cloud: CloudDay,
    indexes: list[int],
    lifetime_before: int | float,
    prior_six: int | float,
) -> list[dict]:
    wanted = set(indexes)
    wh_today = 0
    rows = []
    for index, watt_hours in enumerate(cloud.bins):
        wh_today = whole_number(wh_today + watt_hours)
        if index not in wanted:
            continue
        _start, end = cloud.edges(index)
        rows.append(
            synthetic_record(
                end,
                interval_watts(watt_hours, cloud.interval),
                wh_today,
                whole_number(lifetime_before + wh_today),
                whole_number(prior_six + wh_today),
            )
        )
    return rows


def canon(record: dict) -> str:
    return json.dumps(record, separators=(",", ":"), sort_keys=True)


def merge_rows(local_rows: list[dict], synthetics: list[dict]) -> list[dict]:
    """Local rows sort after a cloud row that shares their readingTime."""
    combined = [(index, row) for index, row in enumerate(local_rows + synthetics)]

    def sort_key(item: tuple[int, dict]) -> tuple:
        index, row = item
        reading = row.get("readingTime") if isinstance(row, dict) else None
        if not is_number(reading):
            reading = 10**18
        local_after = 0 if isinstance(row, dict) and row.get("source") == SOURCE else 1
        return (reading, local_after, index)

    combined.sort(key=sort_key)
    return [row for _index, row in combined]


def log_path(root: Path, day: date) -> Path | None:
    folder = root / f"{day:%Y}" / f"{day:%m}"
    plain = folder / f"solar-log-{day:%Y-%m-%d}.json"
    compressed = plain.with_suffix(".json.gz")
    if plain.is_file() and compressed.is_file():
        if plain.stat().st_size > 0:
            return plain
        return compressed
    if plain.is_file():
        return plain
    if compressed.is_file():
        return compressed
    return None


def output_path(root: Path, day: date, existing: Path | None) -> Path:
    if existing is not None:
        return existing
    folder = root / f"{day:%Y}" / f"{day:%m}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"solar-log-{day:%Y-%m-%d}.json"


def read_text(path: Path) -> str | None:
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                return handle.read()
        return path.read_text(encoding="utf-8")
    except (OSError, EOFError, gzip.BadGzipFile, UnicodeError) as exc:
        log(f"skip {path}: {exc}")
        return None


def parse_records(text: str, path: Path) -> list[dict] | None:
    if not text.strip():
        return []
    decoder = json.JSONDecoder()
    index = 0
    length = len(text)
    records = []
    try:
        while True:
            while index < length and text[index].isspace():
                index += 1
            if index >= length:
                break
            value, index = decoder.raw_decode(text, index)
            if not isinstance(value, dict):
                log(f"skip {path}: record is {type(value).__name__}, not an object")
                return None
            records.append(value)
    except json.JSONDecodeError:
        log(f"skip {path}: JSON stops at offset {index}")
        return None
    return records


def write_records(path: Path, records: list[dict]) -> None:
    payload = "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in records).encode()
    if path.suffix == ".gz":
        payload = gzip.compress(payload)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def load_cloud_days(folder: Path) -> dict[date, CloudDay]:
    days: dict[date, CloudDay] = {}
    interval_dir = folder / "intervals"
    if not interval_dir.is_dir():
        return days
    for path in sorted(interval_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log(f"skip intervals {path.name}: {exc}")
            continue
        for item in payload.get("days") or []:
            cloud = cloud_day(item)
            if cloud is not None:
                days[cloud.day] = cloud
    return days


def cloud_day(item: dict) -> CloudDay | None:
    try:
        day = date.fromisoformat(item["date"])
    except (KeyError, TypeError, ValueError):
        return None
    start = item.get("start_time")
    interval = item.get("interval_seconds")
    bins = item.get("production_wh")
    if not is_number(start) or not is_number(interval) or interval <= 0:
        return None
    if not isinstance(bins, list) or not bins:
        return None
    cleaned = []
    for value in bins:
        if not is_number(value):
            return None
        cleaned.append(whole_number(value))
    return CloudDay(day, int(start), int(interval), tuple(cleaned))


def load_lifetime(path: Path) -> tuple[dict[date, int], list[int]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    start = date.fromisoformat(payload["start_date"])
    series = payload.get("production") or []
    by_date: dict[date, int] = {}
    prefix = [0]
    for index, watt_hours in enumerate(series):
        if not is_number(watt_hours):
            watt_hours = 0
        watt_hours = int(watt_hours)
        by_date[start + timedelta(days=index)] = watt_hours
        prefix.append(prefix[-1] + watt_hours)
    return by_date, prefix


def lifetime_terms(by_date: dict[date, int], prefix: list[int], day: date) -> tuple[int, int] | None:
    if day not in by_date:
        return None
    index = (day - min(by_date)).days
    if index < 0 or index >= len(by_date):
        return None
    prior_six = prefix[index] - prefix[max(0, index - 6)]
    return prefix[index], prior_six


def _locked(operation):
    with solar_log_lock():
        return operation()


def refresh_gaps(root: Path) -> None:
    life_path = root / "enlighten" / "lifetime-energy.json"
    if not life_path.is_file():
        return
    from scrape_enlighten import local_log_dates, write_gap_report

    lifetime = json.loads(life_path.read_text(encoding="utf-8"))
    write_gap_report(root / "enlighten", lifetime, local_log_dates(root))


def merge_store(
    root: Path,
    *,
    today: date | None = None,
    now: float | None = None,
    update_gaps: bool = True,
) -> dict:
    folder = root / "enlighten"
    life_path = folder / "lifetime-energy.json"
    if not life_path.is_file():
        raise SystemExit(f"Missing {life_path}. Run scrape_enlighten.py --full first.")
    clouds = load_cloud_days(folder)
    by_date, prefix = load_lifetime(life_path)
    if today is None:
        today = datetime.now(SITE_TZ).date()
    if now is None:
        now = datetime.now(SITE_TZ).timestamp()
    missing_intervals = [day.isoformat() for day in sorted(by_date) if day not in clouds]
    if missing_intervals:
        log(
            f"lifetime days without intervals: {len(missing_intervals)} "
            f"first={missing_intervals[0]} last={missing_intervals[-1]}"
        )

    created = updated = unchanged = unreadable = 0
    synthetic_rows = local_rows_kept = 0
    ordered_days = sorted(clouds)
    for number, day in enumerate(ordered_days, start=1):
        if number % 400 == 0:
            log(f"merge progress {number}/{len(ordered_days)}")
        cloud = clouds[day]
        terms = lifetime_terms(by_date, prefix, day)
        if terms is None:
            log(f"skip {day.isoformat()}: not in the lifetime series")
            continue
        lifetime_before, prior_six = terms
        existing = log_path(root, day)
        open_day = day == today

        def merge_day():
            return merge_one_file(
                root,
                cloud,
                existing,
                lifetime_before,
                prior_six,
                open_day=open_day,
                now=now,
            )

        # Only today's file is appended by the live scraper. Hold the lock for
        # that rewrite and leave historical files alone so the scrape is not
        # stalled while the archive is scanned.
        outcome = merge_day() if not open_day else _locked(merge_day)
        kind = outcome["kind"]
        if kind == "created":
            created += 1
        elif kind == "updated":
            updated += 1
        elif kind == "unchanged":
            unchanged += 1
        else:
            unreadable += 1
        if kind in {"created", "updated"}:
            synthetic_rows += outcome["synthetic"]
            local_rows_kept += outcome["local"]
    stats = {
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "unreadable": unreadable,
        "synthetic_rows": synthetic_rows,
        "local_rows_kept": local_rows_kept,
        "cloud_days": len(clouds),
    }
    log(
        "merge "
        f"cloud_days={stats['cloud_days']} created={created} updated={updated} "
        f"unchanged={unchanged} unreadable={unreadable} "
        f"enlighten_rows_written={synthetic_rows} local_rows_rewritten={local_rows_kept}"
    )
    if update_gaps:
        refresh_gaps(root)
    return stats


def merge_one_file(
    root: Path,
    cloud: CloudDay,
    existing: Path | None,
    lifetime_before: int,
    prior_six: int,
    *,
    open_day: bool,
    now: float,
) -> dict:
    empty = {"kind": "unchanged", "synthetic": 0, "local": 0}
    text = None
    if existing is not None:
        text = read_text(existing)
        if text is None:
            return {"kind": "unreadable", "synthetic": 0, "local": 0}
        if not SOURCE_MARK.search(text):
            times = [float(match) for match in READING_TIME.findall(text)]
            if not uncovered_indexes(times, cloud, open_day=open_day, now=now):
                return empty

    records = [] if text is None else parse_records(text, existing)
    if records is None:
        return {"kind": "unreadable", "synthetic": 0, "local": 0}
    local_rows = [row for row in records if row.get("source") != SOURCE]
    old_synth = [row for row in records if row.get("source") == SOURCE]
    times = [row["readingTime"] for row in local_rows if is_local_production(row)]
    indexes = uncovered_indexes(times, cloud, open_day=open_day, now=now)
    synthetics = build_synthetics(cloud, indexes, lifetime_before, prior_six)
    if [canon(row) for row in old_synth] == [canon(row) for row in synthetics]:
        return {"kind": "unchanged", "synthetic": len(synthetics), "local": len(local_rows)}
    merged = merge_rows(local_rows, synthetics)
    path = output_path(root, cloud.day, existing)
    write_records(path, merged)
    kind = "created" if existing is None else "updated"
    return {"kind": kind, "synthetic": len(synthetics), "local": len(local_rows)}


def self_test() -> None:
    start = 1_000_000
    cloud = CloudDay(date(2020, 3, 28), start, 900, (0, 10, 0, 5))
    assert uncovered_indexes([], cloud, open_day=False, now=0) == [0, 1, 2, 3]
    rows = build_synthetics(cloud, [0, 1, 2, 3], 1000, 600)
    assert [row["wNow"] for row in rows] == [0, 40, 0, 20]
    assert [row["whToday"] for row in rows] == [0, 10, 10, 15]
    assert [row["whLifetime"] for row in rows] == [1000, 1010, 1010, 1015]
    assert [row["whLastSevenDays"] for row in rows] == [600, 610, 610, 615]
    assert [row["readingTime"] for row in rows] == [1000900, 1001800, 1002700, 1003600]
    for row in rows:
        for key in REQUIRED_KEYS:
            assert key in row
        assert is_number(row["wNow"])
        assert row["vahToday"] is None
        assert row["source"] == SOURCE
        assert row["measurementType"] == "production"
        assert row["activeCount"] == 1

    local = {
        "type": "eim",
        "activeCount": 1,
        "measurementType": "production",
        "readingTime": 1001900,
        "wNow": 2692.982,
        "rmsCurrent": 11.0,
        "rmsVoltage": 244.0,
        "reactPwr": 1.0,
        "apprntPwr": 2.0,
        "whToday": 12.5,
        "vahToday": 13.0,
        "whLifetime": 1012.5,
    }
    closed = uncovered_indexes([1001900], cloud, open_day=False, now=0)
    assert closed == [0, 1, 3], closed
    merged = merge_rows([local], build_synthetics(cloud, closed, 1000, 600))
    assert [row["readingTime"] for row in merged] == [1000900, 1001800, 1001900, 1003600]
    assert merged[2]["wNow"] == 2692.982
    assert merged[2].get("source") is None

    same_time = merge_rows(
        [dict(local, readingTime=1000900)],
        build_synthetics(cloud, [0], 1000, 600),
    )
    assert same_time[0]["source"] == SOURCE
    assert same_time[1].get("source") is None

    open_indexes = uncovered_indexes([1001900], cloud, open_day=True, now=1001900)
    assert open_indexes == [0, 1], open_indexes
    open_merged = merge_rows([local], build_synthetics(cloud, open_indexes, 1000, 600))
    assert open_merged[-1] is local

    # A sample exactly on a bin edge belongs to the bin that starts there.
    assert uncovered_indexes([1000900], cloud, open_day=False, now=0) == [0, 2, 3]

    spring = CloudDay(date(2021, 3, 14), start, 900, tuple([0] * 92))
    assert spring.edges(91)[1] == start + 92 * 900

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        enlighten = root / "enlighten" / "intervals"
        enlighten.mkdir(parents=True)
        day = date(2020, 5, 4)
        start_time = 1_588_598_000
        payload = {
            "days": [
                {
                    "date": day.isoformat(),
                    "start_time": start_time,
                    "interval_seconds": 900,
                    "production_wh": [0, 10, 0, 5],
                }
            ]
        }
        (enlighten / "2020-05.json").write_text(json.dumps(payload), encoding="utf-8")
        series_start = day - timedelta(days=6)
        (root / "enlighten" / "lifetime-energy.json").write_text(
            json.dumps(
                {
                    "start_date": series_start.isoformat(),
                    "production": [100, 100, 100, 100, 100, 100, 15],
                }
            ),
            encoding="utf-8",
        )
        # Pretty-printed local sample sitting in the third bin, plus an empty gzip day.
        local_path = root / "2020" / "05" / f"solar-log-{day.isoformat()}.json"
        local_path.parent.mkdir(parents=True)
        local_path.write_text(json.dumps(local, indent=2) + "\n", encoding="utf-8")
        # Move the sample's reading into this fixture's third bin.
        local_here = dict(local, readingTime=start_time + 1800 + 30)
        local_path.write_text(json.dumps(local_here, indent=2) + "\n", encoding="utf-8")
        empty_day = date(2020, 5, 5)
        empty_path = root / "2020" / "05" / f"solar-log-{empty_day.isoformat()}.json.gz"
        with gzip.open(empty_path, "wb") as handle:
            handle.write(b"")

        first = merge_store(root, today=date(2020, 5, 6), now=0, update_gaps=False)
        assert first["created"] == 0
        assert first["updated"] == 1, first
        assert first["unreadable"] == 0, first
        text = local_path.read_text(encoding="utf-8")
        saved = [json.loads(line) for line in text.splitlines() if line]
        assert len(saved) == 4
        assert any(row.get("wNow") == 2692.982 and row.get("source") is None for row in saved)
        assert sum(1 for row in saved if row.get("source") == SOURCE) == 3
        assert saved[-1]["readingTime"] == start_time + 3600
        for row in saved:
            assert is_number(row["wNow"])
            for key in REQUIRED_KEYS:
                assert key in row

        second = merge_store(root, today=date(2020, 5, 6), now=0, update_gaps=False)
        assert second["updated"] == 0, second
        assert second["unchanged"] == 1
        assert local_path.read_text(encoding="utf-8") == text

        # Open day: the bin after the local sample must not become the last line.
        third = merge_store(root, today=day, now=start_time + 1900, update_gaps=False)
        assert third["updated"] == 1, third
        open_rows = [json.loads(line) for line in local_path.read_text().splitlines() if line]
        assert open_rows[-1]["wNow"] == 2692.982
        assert open_rows[-1].get("source") is None
        assert all(row["readingTime"] <= open_rows[-1]["readingTime"] for row in open_rows)

    covered = CloudDay(date(2024, 10, 23), 0, 900, (1, 1))
    assert uncovered_indexes([100, 1000], covered, open_day=False, now=0) == []
    print("merge_enlighten self-test ok")


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge Enlighten history into solar-log files")
    parser.add_argument("--self-test", action="store_true", help="run offline checks and exit")
    parser.add_argument("--data-root", type=Path, default=None, help="override DATA_DIR")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    root = args.data_root if args.data_root is not None else data_root(load_config())
    merge_store(root)


if __name__ == "__main__":
    main()
