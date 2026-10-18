#!/usr/bin/env python3
"""Sample the local Enphase gateway and append production JSONL under data/.

Firmware D8.3.5433 requires an Enlighten owner token. This process logs in,
caches the JWT in secrets/envoy-token.json, and refreshes it before it expires
or when the gateway returns 401.

The high-rate file stays compatible with the older scraper: one JSON object per
line, the production meter from production.json?details=1. Extra keys record
the microinverter daily total, because this firmware copies lifetime energy
into whToday.

Usage:
  python3 scrape_local.py                 # loop every SAMPLE_SECONDS (default 30)
  python3 scrape_local.py --once          # one sample, then exit
  python3 scrape_local.py --interval 30
  python3 scrape_local.py --self-test

systemd: scrape-solar-data.service runs scrape-solar-data.sh, which holds a
lock and execs this script.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime
from pathlib import Path

from enphase_common import (
    AuthError,
    HttpClient,
    TokenStore,
    apply_firmware_fix,
    brief_body,
    choose_gateway,
    data_root,
    envoy_hosts,
    jwt_claims,
    load_config,
    log,
    sample_seconds,
    self_test,
    solar_log_lock,
)

INVERTER_EVERY = 300
SNAPSHOT_EVERY = 6 * 3600
# Micros stop talking after sunset. Count one as reporting if it spoke during
# this window, so the evening summary still shows the whole array.
REPORTING_WINDOW = 16 * 3600
# How long to wait while deciding that a gateway address is unreachable.
CONNECT_TIMEOUT = 5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape the local Enphase gateway")
    parser.add_argument("--once", action="store_true", help="take one sample and exit")
    parser.add_argument("--interval", type=float, default=None, help="seconds between samples")
    parser.add_argument("--self-test", action="store_true", help="run offline checks and exit")
    return parser.parse_args()


def envoy_url(host: str, path: str) -> str:
    return f"https://{host}{path}"


def get_local(client: HttpClient, url: str, token: str | None, timeout: float = 25) -> tuple[int, bytes]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.request(url, headers=headers, local=True, timeout=timeout)


def parse_json(status: int, body: bytes, what: str):
    if status != 200:
        raise RuntimeError(f"{what} HTTP {status}: {brief_body(body)}")
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{what} was not JSON: {brief_body(body)}") from exc


def pick_production_meter(payload: dict) -> dict | None:
    for item in payload.get("production") or []:
        if item.get("type") == "eim" and item.get("measurementType") == "production":
            return item
    return None


def day_path(root: Path, when: datetime, prefix: str, suffix: str = ".json") -> Path:
    folder = root / f"{when:%Y}" / f"{when:%m}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{prefix}-{when:%Y-%m-%d}{suffix}"


def append_jsonl(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        handle.flush()


def gateway_connected(client: HttpClient, host: str) -> bool:
    """True when the gateway answered. HTTP errors still count as a connection."""
    status, _body = get_local(client, envoy_url(host, "/info"), token=None, timeout=CONNECT_TIMEOUT)
    return status != 0


def select_host(client: HttpClient, hosts: list[str], previous: str | None) -> str:
    """Use the primary address. Try the next address when the primary does not connect."""
    if len(hosts) == 1:
        return hosts[0]
    selected = choose_gateway(hosts, lambda host: gateway_connected(client, host))
    if selected == previous:
        return selected
    if selected == hosts[0]:
        if previous is not None:
            log(f"primary gateway {selected} connected")
    else:
        log(f"primary gateway {hosts[0]} did not connect; using {selected}")
    return selected


def software_version(client: HttpClient, host: str) -> str:
    status, body = get_local(client, envoy_url(host, "/info"), token=None, timeout=10)
    if status != 200:
        return ""
    match = re.search(rb"<software>([^<]+)</software>", body)
    return match.group(1).decode() if match else ""


def summarize_inverters(inverters: list) -> dict:
    now = time.time()
    producing = 0
    reporting = 0
    watts = 0
    for item in inverters:
        watts_now = item.get("lastReportWatts") or 0
        watts += watts_now
        if watts_now > 0:
            producing += 1
        reported = item.get("lastReportDate") or 0
        if reported and now - reported < REPORTING_WINDOW:
            reporting += 1
    return {
        "count": len(inverters),
        "producing": producing,
        "reporting": reporting,
        "watts": watts,
    }


def fetch_inverters(client: HttpClient, host: str, token: str, root: Path) -> dict:
    status, body = get_local(
        client, envoy_url(host, "/api/v1/production/inverters"), token, timeout=40
    )
    if status == 401:
        raise AuthError("inverters returned 401")
    payload = parse_json(status, body, "inverters")
    if not isinstance(payload, list):
        raise RuntimeError("inverters response was not a list")
    when = datetime.now()
    slim = [
        {
            "serialNumber": item.get("serialNumber"),
            "lastReportWatts": item.get("lastReportWatts"),
            "maxReportWatts": item.get("maxReportWatts"),
            "lastReportDate": item.get("lastReportDate"),
            "devType": item.get("devType"),
        }
        for item in payload
    ]
    record = {"scrapedAt": int(time.time()), "inverters": slim}
    append_jsonl(day_path(root, when, "inverters", ".jsonl"), record)
    return summarize_inverters(payload)


def save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(path)


def save_snapshots(client: HttpClient, host: str, token: str, root: Path) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d")
    folder = root / "snapshots"
    targets = {
        "home": "/home.json",
        "inventory": "/inventory.json",
        "meters": "/ivp/meters",
    }
    for name, path in targets.items():
        status, body = get_local(client, envoy_url(host, path), token, timeout=30)
        if status == 401:
            raise AuthError(f"{name} returned 401")
        if status != 200:
            log(f"snapshot {name} skipped HTTP {status}")
            continue
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            log(f"snapshot {name} was not JSON")
            continue
        save_json(folder / f"{name}-{stamp}.json", payload)
        if name == "inventory" and isinstance(payload, list):
            counts = [
                f"{item.get('type')}={len(item.get('devices') or [])}" for item in payload
            ]
            log("inventory " + " ".join(counts))
        if name == "home" and isinstance(payload, dict):
            log(
                "gateway "
                f"tz={payload.get('timezone')} "
                f"date={payload.get('current_date')} {payload.get('current_time')} "
                f"link={((payload.get('network') or {}).get('primary_interface'))}"
            )


def write_sample(
    client: HttpClient,
    store: TokenStore,
    root: Path,
    inverter_summary: dict | None,
    host: str,
) -> dict:
    token = store.ensure()
    prod_url = envoy_url(host, "/production.json?details=1")
    pdm_url = envoy_url(host, "/ivp/pdm/energy")

    def fetch(url: str, what: str, token_value: str):
        status, body = get_local(client, url, token_value)
        if status == 401:
            return 401, body
        return status, parse_json(status, body, what)

    status, production = fetch(prod_url, "production.json", token)
    if status == 401:
        token = store.ensure(force=True)
        status, production = fetch(prod_url, "production.json", token)
        if status == 401:
            store.invalidate()
            raise AuthError("production.json still returned 401 after a token refresh")
    pdm_status, pdm = fetch(pdm_url, "pdm/energy", token)
    if pdm_status == 401:
        token = store.ensure(force=True)
        pdm_status, pdm = fetch(pdm_url, "pdm/energy", token)
    if pdm_status == 401:
        pdm = None
        log("pdm/energy returned 401; saving the meter sample without the daily-energy repair")
    elif not isinstance(pdm, dict):
        pdm = None

    meter = pick_production_meter(production)
    if meter is None:
        raise RuntimeError("production.json has no eim production meter")
    record = {"scrapedAt": int(time.time())}
    record.update(apply_firmware_fix(meter, pdm if isinstance(pdm, dict) else None))
    if inverter_summary:
        record["inverterSummary"] = inverter_summary
    path = day_path(root, datetime.now(), "solar-log", ".json")
    with solar_log_lock():
        append_jsonl(path, record)
    return record


def run(cfg: dict[str, str], once: bool, interval: float) -> None:
    client = HttpClient()
    store = TokenStore(client, cfg)
    root = data_root(cfg)
    hosts = envoy_hosts(cfg)
    host = select_host(client, hosts, None)
    version = software_version(client, host)
    log(
        f"local scrape hosts={','.join(hosts)} using={host} "
        f"serial={cfg['ENVOY_SERIAL']} software={version or 'unknown'} "
        f"interval={interval}s data={root}"
    )
    try:
        token = store.ensure()
    except AuthError as exc:
        log(f"token is not available yet: {exc}")
        if once:
            raise
        token = None
    else:
        claims = jwt_claims(token)
        days_left = round((int(claims["exp"]) - time.time()) / 86400, 1)
        log(f"using {claims.get('enphaseUser')} token expires_in_days={days_left}")

    next_inverters = 0.0
    next_snapshot = 0.0
    summary = None
    failures = 0
    taken = 0
    while True:
        started = time.monotonic()
        try:
            now = time.monotonic()
            host = select_host(client, hosts, host)
            if now >= next_snapshot:
                save_snapshots(client, host, store.ensure(), root)
                next_snapshot = time.monotonic() + SNAPSHOT_EVERY
            if now >= next_inverters:
                summary = fetch_inverters(client, host, store.ensure(), root)
                next_inverters = time.monotonic() + INVERTER_EVERY
                log(
                    "inverters "
                    f"{summary['producing']}/{summary['count']} producing "
                    f"{summary['watts']}W reporting={summary['reporting']}"
                )
            record = write_sample(client, store, root, summary, host)
            taken += 1
            failures = 0
            log(
                f"sample wNow={record.get('wNow')} whToday={record.get('whToday')} "
                f"whLifetime={record.get('whLifetime')} "
                f"source={record.get('whTodaySource', 'meter')}"
            )
        except AuthError as exc:
            failures += 1
            store.invalidate()
            delay = min(3600, 30 * 2 ** min(failures, 5))
            log(f"auth error ({failures}): {exc}; retrying in {delay}s")
            if once:
                raise
            time.sleep(delay)
            continue
        except Exception as exc:
            failures += 1
            log(f"sample failed ({failures}): {exc}")
            if once:
                raise
            if failures >= 8:
                # A long outage should not hammer Enlighten. The gateway itself
                # is retried on the normal sample interval until this trips.
                delay = min(900, 15 * failures)
                log(f"backing off {delay}s")
                time.sleep(delay)
        if once:
            break
        remain = interval - (time.monotonic() - started)
        if remain > 0:
            time.sleep(remain)


def main() -> None:
    args = parse_args()
    if args.self_test:
        self_test()
        return
    cfg = load_config()
    interval = args.interval if args.interval is not None else sample_seconds(cfg)
    if interval < 5:
        raise SystemExit("--interval must be at least 5 seconds")
    run(cfg, once=args.once, interval=interval)


if __name__ == "__main__":
    main()
