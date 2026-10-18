#!/usr/bin/env python3
"""Pull Enlighten history into data/enlighten/.

The website session, after the same login used for the local token, exposes:

  /pv/systems/<id>/lifetime_energy.json
      one watt-hour total per day from the system's first day

  /pv/systems/<id>/daily_energy.json?start_date=&end_date=
      96 bins of watt-hours per day, each bin 900 seconds (15 minutes)

That is finer than the hourly graphs in the Enlighten UI. The official
Monitoring API v4 is a separate developer app and is not used here.

Usage:
  python3 scrape_enlighten.py                 # lifetime file + last 45 days
  python3 scrape_enlighten.py --full          # lifetime + every day since startup
  python3 scrape_enlighten.py --from 2024-01-01 --to 2024-01-31
  python3 scrape_enlighten.py --refresh       # rewrite interval months in range
  python3 scrape_enlighten.py --self-test

Saved layout (all under data/, which is gitignored):
  enlighten/lifetime-energy.json     daily watt-hours, one series
  enlighten/daily-wh.jsonl           same series, one day per line
  enlighten/intervals/YYYY-MM.json   15-minute watt-hours
  enlighten/local-gaps.txt           cloud days that have no local solar-log
  enlighten/inverters.json           latest per-microinverter status
  enlighten/devices.json             gateway and device roster
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from enphase_common import (
    AuthError,
    HttpClient,
    brief_body,
    data_root,
    enlighten_login,
    load_config,
    log,
)

SITE_TZ = ZoneInfo("America/New_York")
CHUNK_DAYS = 90
RECENT_DAYS = 45
# Re-fetch a finished month when its file is missing. Always re-fetch a month
# that still contains a day inside this window, because Enlighten revises it.
REWRITE_DAYS = 10
OTHER_SERIES = (
    "consumption",
    "import",
    "export",
    "grid_import",
    "solar_home",
    "solar_grid",
    "grid_home",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download Enlighten production history")
    parser.add_argument("--full", action="store_true", help="walk back to the system start date")
    parser.add_argument("--refresh", action="store_true", help="rewrite interval files even if present")
    parser.add_argument("--from", dest="start", help="YYYY-MM-DD")
    parser.add_argument("--to", dest="end", help="YYYY-MM-DD")
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args()


def parse_day(value: str) -> date:
    return date.fromisoformat(value)


def iter_chunks(start: date, end: date, days: int = CHUNK_DAYS):
    cursor = start
    while cursor <= end:
        stop = min(end, cursor + timedelta(days=days - 1))
        yield cursor, stop
        cursor = stop + timedelta(days=1)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def cloud_json(client: HttpClient, url: str) -> dict:
    status, body = client.request(url, timeout=90, max_bytes=20_000_000)
    if status == 0:
        raise RuntimeError(brief_body(body) or "connection failed")
    if status != 200:
        raise RuntimeError(f"HTTP {status}: {brief_body(body)}")
    if not body.startswith(b"{") and not body.startswith(b"["):
        raise RuntimeError(f"not JSON: {brief_body(body)}")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise RuntimeError("expected a JSON object")
    return payload


def lifetime_url(system_id: str) -> str:
    return f"https://enlighten.enphaseenergy.com/pv/systems/{system_id}/lifetime_energy.json"


def daily_url(system_id: str, start: date, end: date) -> str:
    query = urllib.parse.urlencode(
        {"start_date": start.isoformat(), "end_date": end.isoformat()}
    )
    return (
        f"https://enlighten.enphaseenergy.com/pv/systems/{system_id}/daily_energy.json?{query}"
    )


def fetch_lifetime(client: HttpClient, system_id: str, folder: Path) -> dict:
    payload = cloud_json(client, lifetime_url(system_id))
    payload["fetched_at"] = datetime.now(SITE_TZ).isoformat(timespec="seconds")
    atomic_write(folder / "lifetime-energy.json", json.dumps(payload))
    start = date.fromisoformat(payload["start_date"])
    series = payload.get("production") or []
    lines = []
    for index, watt_hours in enumerate(series):
        day = start + timedelta(days=index)
        lines.append(json.dumps({"date": day.isoformat(), "wh": watt_hours}, separators=(",", ":")))
    atomic_write(folder / "daily-wh.jsonl", "\n".join(lines) + ("\n" if lines else ""))
    log(
        f"lifetime {payload.get('start_date')} days={len(series)} "
        f"last_wh={series[-1] if series else 'n/a'}"
    )
    return payload


def day_record(stat: dict, fallback: date) -> dict:
    start_time = stat.get("start_time")
    if isinstance(start_time, (int, float)) and start_time > 0:
        day = datetime.fromtimestamp(start_time, SITE_TZ).date()
    else:
        day = fallback
    production = stat.get("production") or []
    totals = stat.get("totals") if isinstance(stat.get("totals"), dict) else {}
    total = totals.get("production")
    if not isinstance(total, (int, float)):
        total = sum(value for value in production if isinstance(value, (int, float)))
    record = {
        "date": day.isoformat(),
        "start_time": start_time,
        "interval_seconds": stat.get("interval_length"),
        "total_wh": total,
        "production_wh": production,
    }
    for key in OTHER_SERIES:
        series = stat.get(key)
        if isinstance(series, list) and any(series):
            record[f"{key}_wh"] = series
    changes = stat.get("grid_changes")
    if isinstance(changes, dict) and changes:
        record["grid_changes"] = changes
    return record


def load_month(path: Path) -> dict[str, dict]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {item["date"]: item for item in payload.get("days") or [] if item.get("date")}


def write_month(path: Path, system_id: str, month: str, days: dict[str, dict]) -> None:
    ordered = [days[key] for key in sorted(days)]
    intervals = {item.get("interval_seconds") for item in ordered if item.get("interval_seconds")}
    payload = {
        "system_id": int(system_id),
        "month": month,
        "timezone": "America/New_York",
        "interval_seconds": next(iter(intervals)) if len(intervals) == 1 else sorted(intervals),
        "production_unit": "Wh per interval",
        "fetched_at": datetime.now(SITE_TZ).isoformat(timespec="seconds"),
        "days": ordered,
    }
    atomic_write(path, json.dumps(payload))


def months_between(start: date, end: date) -> list[str]:
    months = []
    cursor = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    while cursor <= last:
        months.append(f"{cursor:%Y-%m}")
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    return months


def month_is_fresh(path: Path, month: str, today: date) -> bool:
    """Skip a month file that already exists and is old enough to be stable."""
    if not path.is_file() or path.stat().st_size == 0:
        return False
    year, month_num = (int(part) for part in month.split("-"))
    # The month still overlaps the rewrite window if its last day is recent.
    if month_num == 12:
        last_day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(year, month_num + 1, 1) - timedelta(days=1)
    return last_day < today - timedelta(days=REWRITE_DAYS)


def month_span(month: str, start: date, end: date) -> tuple[date, date] | None:
    first = date.fromisoformat(month + "-01")
    if first.month == 12:
        last = date(first.year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(first.year, first.month + 1, 1) - timedelta(days=1)
    span_start = max(start, first)
    span_end = min(end, last)
    if span_end < span_start:
        return None
    return span_start, span_end


def pending_spans(pending: list[str], start: date, end: date) -> list[tuple[date, date]]:
    spans: list[tuple[date, date]] = []
    for month in pending:
        span = month_span(month, start, end)
        if span is None:
            continue
        if spans and span[0] <= spans[-1][1] + timedelta(days=1):
            spans[-1] = (spans[-1][0], max(spans[-1][1], span[1]))
        else:
            spans.append(span)
    return spans


def fetch_intervals(
    client: HttpClient,
    cfg: dict[str, str],
    system_id: str,
    folder: Path,
    start: date,
    end: date,
    refresh: bool,
) -> None:
    today = datetime.now(SITE_TZ).date()
    interval_dir = folder / "intervals"
    months = months_between(start, end)
    pending = []
    for month in months:
        path = interval_dir / f"{month}.json"
        if refresh or not month_is_fresh(path, month, today):
            pending.append(month)
        else:
            log(f"intervals {month} already saved")
    if not pending:
        log("intervals are up to date")
        return
    wanted = set(pending)
    spans = pending_spans(pending, start, end)
    log(f"fetching intervals for {len(pending)} months in {len(spans)} span(s)")
    for span_start, span_end in spans:
        for chunk_start, chunk_end in iter_chunks(span_start, span_end, CHUNK_DAYS):
            payload = None
            for attempt in range(3):
                try:
                    payload = cloud_json(client, daily_url(system_id, chunk_start, chunk_end))
                    break
                except RuntimeError as exc:
                    log(f"daily_energy {chunk_start}..{chunk_end} attempt {attempt + 1}: {exc}")
                    message = str(exc)
                    if "HTTP 401" in message or "not JSON" in message:
                        try:
                            enlighten_login(client, cfg)
                        except AuthError as auth_exc:
                            log(f"re-login failed: {auth_exc}")
                    time.sleep(2 + attempt * 3)
            if payload is None:
                log(f"gave up on {chunk_start}..{chunk_end}")
                continue
            stats = payload.get("stats") or []
            expected = (chunk_end - chunk_start).days + 1
            if len(stats) != expected:
                log(
                    f"daily_energy {chunk_start}..{chunk_end} returned "
                    f"{len(stats)} days, expected {expected}"
                )
            by_month: dict[str, dict[str, dict]] = {}
            for index, stat in enumerate(stats):
                fallback = chunk_start + timedelta(days=index)
                record = day_record(stat, fallback)
                month = record["date"][:7]
                if month not in wanted:
                    continue
                by_month.setdefault(month, {})[record["date"]] = record
            for month, updates in by_month.items():
                path = interval_dir / f"{month}.json"
                days = load_month(path)
                days.update(updates)
                write_month(path, system_id, month, days)
            first = stats[0].get("interval_length") if stats else None
            bins = len((stats[0].get("production") or [])) if stats else 0
            log(
                f"intervals {chunk_start}..{chunk_end} days={len(stats)} "
                f"interval_seconds={first} bins={bins}"
            )
            time.sleep(0.4)


def local_log_dates(root: Path) -> set[str]:
    found = set()
    if not root.is_dir():
        return found
    for path in root.glob("20*/*/*solar-log-*.json*"):
        match = re.search(r"(\d{4}-\d{2}-\d{2})", path.name)
        if match and path.stat().st_size > 0:
            found.add(match.group(1))
    return found


def write_gap_report(folder: Path, lifetime: dict, local_dates: set[str]) -> None:
    start = date.fromisoformat(lifetime["start_date"])
    series = lifetime.get("production") or []
    missing = []
    for index in range(len(series)):
        day = (start + timedelta(days=index)).isoformat()
        if day not in local_dates:
            missing.append(day)
    ranges = []
    for day in missing:
        current = date.fromisoformat(day)
        if not ranges or current != ranges[-1][1] + timedelta(days=1):
            ranges.append([current, current])
        else:
            ranges[-1][1] = current
    lines = [
        f"Cloud daily totals: {len(series)} days from {start.isoformat()}",
        f"Local solar-log days with data: {len(local_dates)}",
        f"Cloud days with no local solar-log: {len(missing)}",
        "",
    ]
    for first, last in ranges:
        if first == last:
            lines.append(first.isoformat())
        else:
            lines.append(f"{first.isoformat()} .. {last.isoformat()}")
    atomic_write(folder / "local-gaps.txt", "\n".join(lines) + "\n")
    log(
        f"gaps cloud_days={len(series)} local_days={len(local_dates)} "
        f"missing_local={len(missing)} ranges={len(ranges)}"
    )


def compare_totals(folder: Path, lifetime: dict) -> None:
    start = date.fromisoformat(lifetime["start_date"])
    series = lifetime.get("production") or []
    by_date = {
        (start + timedelta(days=index)).isoformat(): watt_hours
        for index, watt_hours in enumerate(series)
    }
    mismatches = []
    compared = 0
    for path in sorted((folder / "intervals").glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for day in payload.get("days") or []:
            expected = by_date.get(day.get("date"))
            actual = day.get("total_wh")
            if expected is None or not isinstance(actual, (int, float)):
                continue
            compared += 1
            if abs(actual - expected) > max(50, abs(expected) * 0.02):
                mismatches.append((day.get("date"), actual, expected))
    log(f"interval totals checked against lifetime on {compared} days, mismatches={len(mismatches)}")
    for item in mismatches[:8]:
        log(f"  mismatch {item[0]} intervals={item[1]} lifetime={item[2]}")


def save_roster(client: HttpClient, system_id: str, folder: Path) -> None:
    base = f"https://enlighten.enphaseenergy.com/app-api/{system_id}"
    for name in ("inverters", "devices"):
        try:
            payload = cloud_json(client, f"{base}/{name}.json")
        except RuntimeError as exc:
            log(f"{name} snapshot skipped: {exc}")
            continue
        payload["fetched_at"] = datetime.now(SITE_TZ).isoformat(timespec="seconds")
        atomic_write(folder / f"{name}.json", json.dumps(payload))
        if name == "inverters":
            log(
                f"cloud inverters total={payload.get('total')} "
                f"not_reporting={payload.get('not_reporting')} "
                f"normal={payload.get('normal_count')}"
            )


def resolve_range(args: argparse.Namespace, lifetime: dict) -> tuple[date, date]:
    today = datetime.now(SITE_TZ).date()
    start_default = date.fromisoformat(lifetime["start_date"])
    if args.start or args.end:
        start = parse_day(args.start) if args.start else start_default
        end = parse_day(args.end) if args.end else today
    elif args.full:
        start, end = start_default, today
    else:
        start, end = today - timedelta(days=RECENT_DAYS - 1), today
    if end < start:
        raise SystemExit("--to is before --from")
    return start, end


def self_test_chunks() -> None:
    chunks = list(iter_chunks(date(2020, 3, 28), date(2020, 3, 30), 2))
    assert chunks == [
        (date(2020, 3, 28), date(2020, 3, 29)),
        (date(2020, 3, 30), date(2020, 3, 30)),
    ]
    record = day_record(
        {
            "start_time": 1792209600,
            "interval_length": 900,
            "production": [0, 10, 0],
            "consumption": [],
            "totals": {"production": 10},
        },
        date(2000, 1, 1),
    )
    assert record["date"] == "2026-10-17", record["date"]
    assert record["total_wh"] == 10
    assert "consumption_wh" not in record
    spans = pending_spans(["2020-03", "2020-04", "2026-10"], date(2020, 3, 28), date(2026, 10, 17))
    assert spans == [
        (date(2020, 3, 28), date(2020, 4, 30)),
        (date(2026, 10, 1), date(2026, 10, 17)),
    ], spans
    print("scrape_enlighten self-test ok")


def main() -> None:
    args = parse_args()
    if args.self_test:
        self_test_chunks()
        return
    cfg = load_config()
    system_id = cfg["ENLIGHTEN_SYSTEM_ID"]
    folder = data_root(cfg) / "enlighten"
    client = HttpClient()
    try:
        login = enlighten_login(client, cfg)
    except AuthError as exc:
        raise SystemExit(f"Enlighten login failed: {exc}") from exc
    log(f"enlighten login ok system_id={login.get('system_id') or system_id}")
    lifetime = fetch_lifetime(client, system_id, folder)
    start, end = resolve_range(args, lifetime)
    fetch_intervals(client, cfg, system_id, folder, start, end, refresh=args.refresh)
    save_roster(client, system_id, folder)
    compare_totals(folder, lifetime)
    from merge_enlighten import merge_store

    merge_store(data_root(cfg))
    write_gap_report(folder, lifetime, local_log_dates(data_root(cfg)))


if __name__ == "__main__":
    main()
