#!/usr/bin/env python3
"""Alert when some panels stay offline while others are producing.

A dark array is not an alert. Night, cloud, and snow on every panel all look
the same: every microinverter reports 0 W. A panel becomes a suspect only
while at least one other panel is producing. The clock keeps running through
the night and resets when that panel produces again. The alert waits
ALERT_TIMEOUT_HOURS (default 48) so snow on part of the array can sit for a
couple of days.

Low-light inventory flags are ignored. Those show up at dawn, in shade, and
on a sleeping array. Any other device status that stays set for the same
timeout is a fault, including on a panel that is still producing a few watts.

Usage:
  python3 alert_panels.py
  python3 alert_panels.py --dry-run
  python3 alert_panels.py --self-test

The user timer alert-panels.timer runs this every 15 minutes. It reads the
inverter JSONL the local scraper already writes. --production-only on the
scraper leaves nothing for this check to read.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from enphase_common import data_root, load_config, log

# A daytime report is a few minutes old. After dark the last report ages for hours.
FRESH_SECONDS = 30 * 60
# Inverter samples arrive about every five minutes. Older than this, skip the check.
STALE_SECONDS = 30 * 60
TIMEOUT_HOURS = 48
REPEAT_HOURS = 24

# Seen on this gateway at night, at dawn, and on shaded panels that are still working.
BENIGN_FLAGS = frozenset(
    {
        "envoy.global.ok",
        "envoy.cond_flags.obs_strs.failure",
        "envoy.cond_flags.pcu_ctrl.dc-pwr-low",
        "envoy.cond_flags.pcu_chan.dcvoltagetoolow",
    }
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Alert when some panels stay offline")
    parser.add_argument("--dry-run", action="store_true", help="check only; do not save or send")
    parser.add_argument("--self-test", action="store_true", help="run offline checks and exit")
    return parser.parse_args()


def alert_hours(cfg: dict[str, str], key: str, default: float) -> float:
    """Hours from secrets.env. Blank uses the default. Minimum 1."""
    raw = (cfg.get(key) or str(default)).strip()
    try:
        hours = float(raw)
    except ValueError as exc:
        raise SystemExit(f"{key} must be a number of hours, not {raw!r}") from exc
    if hours < 1:
        raise SystemExit(f"{key} must be at least 1 hour")
    return hours


def alert_recipient(cfg: dict[str, str]) -> str | None:
    """Where to send mail. Blank uses the Enlighten address. off sends no mail."""
    raw = cfg.get("ALERT_EMAIL")
    if raw is None or not raw.strip():
        recipient = (cfg.get("ENPHASE_EMAIL") or "").strip()
    elif raw.strip().lower() in {"off", "none", "false"}:
        recipient = ""
    else:
        recipient = raw.strip()
    if not recipient:
        return None
    if "@" not in recipient or any(char in recipient for char in "\r\n"):
        raise SystemExit("ALERT_EMAIL must be an email address, or off")
    return recipient


def serious_flags(status) -> list[str]:
    """Inventory flags that are not the normal dark or low-light set."""
    if status is None:
        return []
    if isinstance(status, str):
        flags = [status]
    elif isinstance(status, list):
        flags = [str(item) for item in status if item]
    else:
        return []
    return [flag for flag in flags if flag not in BENIGN_FLAGS]


def _number(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _producing(item: dict, now: float) -> tuple[bool, float, float | None]:
    watts = _number(item.get("lastReportWatts"))
    reported = _number(item.get("lastReportDate"))
    if reported <= 0:
        return False, watts, None
    age = now - reported
    fresh = 0 <= age <= FRESH_SECONDS
    return watts > 0 and fresh, watts, age


def _flag_map(devices: list[dict] | None) -> dict[str, list[str]] | None:
    """Serial to serious flags, or None when this snapshot cannot be trusted.

    Night snapshots mark every panel not communicating. Those flags are the
    array asleep, so they are not used.
    """
    if not devices:
        return None
    communicating = sum(1 for device in devices if device.get("communicating"))
    if communicating < 1 or communicating * 2 < len(devices):
        return None
    found: dict[str, list[str]] = {}
    for device in devices:
        serial = str(device.get("serial_num") or "")
        if serial:
            found[serial] = serious_flags(device.get("device_status"))
    return found


def evaluate_panels(
    inverters: list[dict],
    now: float,
    state: dict,
    timeout_s: float,
    repeat_s: float,
    inventory: list[dict] | None = None,
) -> tuple[dict, list[dict], dict]:
    """Return the next state, alert events, and a short summary.

    `now` is the sample time. Night does not start or clear a suspect clock.
    """
    state = copy.deepcopy(state)
    panels: dict = state.setdefault("panels", {})
    roster = list(state.get("roster") or [])
    seen: dict[str, dict] = {}
    for item in inverters:
        if not isinstance(item, dict):
            continue
        serial = str(item.get("serialNumber") or "")
        if serial:
            seen[serial] = item
    summary = {
        "awake": False,
        "skipped": None,
        "producing": 0,
        "count": len(seen),
        "max_watts": 0.0,
        "waiting": len(panels),
    }
    if roster and len(seen) < len(roster) * 0.8:
        summary["skipped"] = "incomplete"
        summary["count"] = len(roster)
        return state, [], summary

    classified = {serial: _producing(item, now) for serial, item in seen.items()}
    producing_watts = [watts for producing, watts, _age in classified.values() if producing]
    summary["producing"] = len(producing_watts)
    summary["max_watts"] = max(producing_watts) if producing_watts else 0.0
    summary["awake"] = bool(producing_watts)
    flags_by_serial = _flag_map(inventory)
    have_flags = flags_by_serial is not None

    if not summary["awake"]:
        state["roster"] = sorted(set(roster) | set(seen))
        summary["count"] = len(state["roster"])
        summary["waiting"] = len(panels)
        return state, [], summary

    events: list[dict] = []
    missing = [serial for serial in roster if serial not in seen]
    for serial in missing:
        seen[serial] = {}
        classified[serial] = (False, 0.0, None)

    for serial, item in seen.items():
        producing, watts, age = classified[serial]
        flags = list(flags_by_serial.get(serial, [])) if have_flags else None
        panel = dict(panels.get(serial) or {})
        down_since = panel.get("down_since")
        fault_since = panel.get("fault_since")
        alerted_at = panel.get("alerted_at")
        if producing:
            down_since = None
        elif down_since is None:
            down_since = now
        if have_flags:
            if flags:
                if fault_since is None:
                    fault_since = now
            else:
                fault_since = None
            stored_flags = flags
        else:
            stored_flags = list(panel.get("flags") or [])

        reasons = []
        since_values = []
        if down_since is not None:
            reasons.append("not producing")
            since_values.append(down_since)
        if fault_since is not None:
            reasons.append("fault")
            since_values.append(fault_since)
        if not reasons:
            if alerted_at is not None:
                events.append(
                    {
                        "action": "recovery",
                        "serial": serial,
                        "watts": watts,
                        "flags": stored_flags,
                    }
                )
            panels.pop(serial, None)
            continue

        since = min(since_values)
        elapsed = now - since
        record = {
            "down_since": down_since,
            "fault_since": fault_since,
            "alerted_at": alerted_at,
            "flags": stored_flags,
        }
        if elapsed >= timeout_s:
            repeat_due = alerted_at is not None and now - alerted_at >= repeat_s
            if alerted_at is None or repeat_due:
                record["alerted_at"] = now
                events.append(
                    {
                        "action": "repeat" if alerted_at is not None else "alert",
                        "serial": serial,
                        "since": since,
                        "hours": elapsed / 3600,
                        "reasons": reasons,
                        "flags": stored_flags,
                        "watts": watts,
                        "report_age_s": age,
                    }
                )
        panels[serial] = record

    state["panels"] = panels
    state["roster"] = sorted(set(roster) | set(seen))
    summary["count"] = len(state["roster"])
    summary["waiting"] = len(panels)
    return state, events, summary


def format_report(events: list[dict], summary: dict, timeout_hours: float) -> tuple[str, str]:
    alerts = [event for event in events if event["action"] in {"alert", "repeat"}]
    recovered = [event for event in events if event["action"] == "recovery"]
    subject_parts = []
    if alerts:
        subject_parts.append(f"{len(alerts)} of {summary['count']} panels offline")
    if recovered:
        noun = "panel" if len(recovered) == 1 else "panels"
        subject_parts.append(f"{len(recovered)} {noun} recovered")
    subject = "Solar: " + "; ".join(subject_parts)

    lines = []
    if alerts:
        verb = "is" if len(alerts) == 1 else "are"
        lines.append(
            f"{len(alerts)} of {summary['count']} panels {verb} not keeping up with the array."
        )
        lines.append(
            f"{summary['producing']} panels are producing, up to {summary['max_watts']:.0f} W."
        )
        lines.append(
            f"The wait is {timeout_hours:.0f} hours from the first time other panels "
            "were producing and this one was not. The clock keeps running overnight "
            "and resets if the panel produces again, so snow on part of the array "
            "can sit for a couple of days."
        )
        lines.append("")
        for event in sorted(alerts, key=lambda item: item["serial"]):
            age = event.get("report_age_s")
            if age is None:
                report = "missing from the latest inverter report"
            else:
                report = f"last report {age / 60:.0f} min ago"
            reason = " and ".join(event["reasons"])
            lines.append(
                f"{event['serial']}  {reason} for {event['hours']:.0f} h  "
                f"{event['watts']:.0f} W  {report}"
            )
            if event.get("flags"):
                lines.append("  status: " + ", ".join(event["flags"]))
    if recovered:
        if lines:
            lines.append("")
        lines.append("Producing again:")
        for event in sorted(recovered, key=lambda item: item["serial"]):
            lines.append(f"{event['serial']}  {event['watts']:.0f} W")
    return subject, "\n".join(lines) + "\n"


def inverter_path(root: Path, when: datetime) -> Path:
    return root / f"{when:%Y}" / f"{when:%m}" / f"inverters-{when:%Y-%m-%d}.jsonl"


def last_inverter_record(path: Path) -> dict | None:
    if not path.is_file():
        return None
    lines = path.read_text(encoding="utf-8").splitlines()
    for line in reversed(lines[-5:]):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and isinstance(record.get("inverters"), list):
            return record
    return None


def latest_inverter_record(root: Path, when: datetime) -> dict | None:
    best = None
    for offset in (0, 1):
        record = last_inverter_record(inverter_path(root, when - timedelta(days=offset)))
        if record is None:
            continue
        if best is None or _number(record.get("scrapedAt")) > _number(best.get("scrapedAt")):
            best = record
    return best


def latest_inventory(root: Path) -> list[dict] | None:
    folder = root / "snapshots"
    if not folder.is_dir():
        return None
    files = sorted(folder.glob("inventory-*.json"))
    if not files:
        return None
    try:
        payload = json.loads(files[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, list):
        return None
    devices: list[dict] = []
    for block in payload:
        if isinstance(block, dict) and block.get("type") == "PCU":
            devices.extend(device for device in (block.get("devices") or []) if isinstance(device, dict))
    return devices


def state_path() -> Path:
    return Path(__file__).resolve().parent / "secrets" / "panel-alert.json"


def load_state(path: Path) -> dict:
    if not path.is_file():
        return {"version": 1, "roster": [], "panels": {}}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"{path.name} is not readable JSON; leaving it in place") from exc
    if not isinstance(loaded, dict):
        raise SystemExit(f"{path.name} is not a JSON object; leaving it in place")
    loaded.setdefault("version", 1)
    loaded.setdefault("roster", [])
    loaded.setdefault("panels", {})
    return loaded


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(mode=0o700, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def append_alert_log(root: Path, subject: str, body: str) -> None:
    folder = root / "alerts"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "panel-alerts.log"
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {subject}\n{body}\n")


def send_mail(recipient: str, subject: str, body: str) -> None:
    message = f"To: {recipient}\nSubject: {subject}\n\n{body}"
    subprocess.run(
        ["/usr/sbin/sendmail", "-t", "-oi"],
        input=message.encode("utf-8"),
        check=True,
        timeout=60,
    )


def describe(summary: dict, timeout_hours: float) -> str:
    if summary.get("skipped") == "incomplete":
        return "inverter sample looks incomplete; alert state left unchanged"
    producing = summary["producing"]
    count = summary["count"]
    waiting = summary["waiting"]
    if summary["awake"]:
        return (
            f"array awake; producing {producing}/{count}; "
            f"max {summary['max_watts']:.0f} W; waiting {waiting} under {timeout_hours:.0f}h"
        )
    return f"array asleep; producing {producing}/{count}; waiting {waiting} under {timeout_hours:.0f}h"


def run_check(cfg: dict[str, str], dry_run: bool) -> None:
    timeout_hours = alert_hours(cfg, "ALERT_TIMEOUT_HOURS", TIMEOUT_HOURS)
    repeat_hours = alert_hours(cfg, "ALERT_REPEAT_HOURS", REPEAT_HOURS)
    recipient = alert_recipient(cfg)
    root = data_root(cfg)
    record = latest_inverter_record(root, datetime.now())
    if record is None:
        log("no inverter samples yet; panel alert skipped")
        return
    scraped_at = _number(record.get("scrapedAt"))
    age = time.time() - scraped_at
    if scraped_at <= 0 or age > STALE_SECONDS:
        log(f"latest inverter sample is {age / 60:.0f} min old; panel alert skipped")
        return

    path = state_path()
    previous = load_state(path)
    updated, events, summary = evaluate_panels(
        record["inverters"],
        scraped_at,
        previous,
        timeout_s=timeout_hours * 3600,
        repeat_s=repeat_hours * 3600,
        inventory=latest_inventory(root),
    )
    log(describe(summary, timeout_hours))
    if summary.get("skipped"):
        return
    if not events:
        if not dry_run:
            save_state(path, updated)
        return

    subject, body = format_report(events, summary, timeout_hours)
    log(subject)
    for line in body.rstrip().splitlines():
        log(line)
    if dry_run:
        log("dry run; alert not sent")
        return
    append_alert_log(root, subject, body)
    if recipient is None:
        log("ALERT_EMAIL is off; alert written to data/alerts only")
        save_state(path, updated)
        return
    try:
        send_mail(recipient, subject, body)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"alert mail failed: {exc}")
        raise SystemExit(1) from exc
    log("alert mail sent")
    save_state(path, updated)


def self_test() -> None:
    now = 1_700_000_000.0
    timeout = 48 * 3600
    repeat = 24 * 3600
    empty = {"version": 1, "roster": [], "panels": {}}

    def panel(serial: str, watts: float, age: float = 60, at: float | None = None) -> dict:
        stamp = now if at is None else at
        return {
            "serialNumber": serial,
            "lastReportWatts": watts,
            "lastReportDate": stamp - age,
        }

    both_dark = [panel("a", 0, age=4000), panel("b", 0, age=4000)]
    state, events, summary = evaluate_panels(both_dark, now, empty, timeout, repeat)
    assert summary["awake"] is False
    assert events == []
    assert state["panels"] == {}

    split = [panel("a", 120), panel("b", 0)]
    state, events, summary = evaluate_panels(split, now, empty, timeout, repeat)
    assert summary["awake"] is True
    assert summary["producing"] == 1
    assert events == []
    assert state["panels"]["b"]["down_since"] == now
    assert "a" not in state["panels"]

    # Night does not clear the panel that was already down.
    state, events, summary = evaluate_panels(both_dark, now + 10 * 3600, state, timeout, repeat)
    assert summary["awake"] is False
    assert events == []
    assert state["panels"]["b"]["down_since"] == now

    # The next day it produces again before the timeout, so there is no alert.
    back_at = now + 20 * 3600
    back = [panel("a", 100, at=back_at), panel("b", 40, at=back_at)]
    state, events, summary = evaluate_panels(back, back_at, state, timeout, repeat)
    assert events == []
    assert state["panels"] == {}

    # A panel that stays at 0 W while another produces, across two nights.
    state, events, _summary = evaluate_panels(split, now, empty, timeout, repeat)
    later = now + 49 * 3600
    split_later = [
        {"serialNumber": "a", "lastReportWatts": 80, "lastReportDate": later - 60},
        {"serialNumber": "b", "lastReportWatts": 0, "lastReportDate": later - 60},
    ]
    state, events, summary = evaluate_panels(split_later, later, state, timeout, repeat)
    assert len(events) == 1
    assert events[0]["action"] == "alert"
    assert events[0]["serial"] == "b"
    assert "not producing" in events[0]["reasons"]
    assert state["panels"]["b"]["alerted_at"] == later

    # Still down, but inside the repeat window.
    held, quiet, _summary = evaluate_panels(split_later, later + 3600, state, timeout, repeat)
    assert quiet == []
    assert held["panels"]["b"]["alerted_at"] == later

    # A day later the same panel is still down.
    repeat_at = later + 25 * 3600
    split_repeat = [
        {"serialNumber": "a", "lastReportWatts": 90, "lastReportDate": repeat_at - 60},
        {"serialNumber": "b", "lastReportWatts": 0, "lastReportDate": repeat_at - 60},
    ]
    held, again, _summary = evaluate_panels(split_repeat, repeat_at, held, timeout, repeat)
    assert [event["action"] for event in again] == ["repeat"]

    recovered_at = repeat_at + 3600
    producing = [
        {"serialNumber": "a", "lastReportWatts": 90, "lastReportDate": recovered_at - 60},
        {"serialNumber": "b", "lastReportWatts": 15, "lastReportDate": recovered_at - 60},
    ]
    cleared, events, _summary = evaluate_panels(producing, recovered_at, held, timeout, repeat)
    assert [event["action"] for event in events] == ["recovery"]
    assert cleared["panels"] == {}

    # Sunrise stagger starts the clock and the later sample clears it.
    dawn = [panel("a", 20), panel("b", 0), panel("c", 0)]
    state, events, summary = evaluate_panels(dawn, now, empty, timeout, repeat)
    assert events == []
    assert summary["waiting"] == 2
    risen_at = now + 1800
    risen = [panel("a", 40, at=risen_at), panel("b", 30, at=risen_at), panel("c", 25, at=risen_at)]
    state, events, summary = evaluate_panels(risen, risen_at, state, timeout, repeat)
    assert events == []
    assert summary["waiting"] == 0

    # Stuck last watts with a stale report counts as down once a peer is fresh.
    stuck = [panel("a", 100, age=60), panel("b", 180, age=3 * 3600)]
    state, events, _summary = evaluate_panels(stuck, now, empty, timeout, repeat)
    assert "b" in state["panels"]
    assert events == []

    # A short sample does not mark the rest of the roster offline.
    roster_state = {"version": 1, "roster": [str(i) for i in range(10)], "panels": {}}
    short = [panel("0", 50), panel("1", 40)]
    same, events, summary = evaluate_panels(short, now, roster_state, timeout, repeat)
    assert summary["skipped"] == "incomplete"
    assert events == []
    assert same["panels"] == {}

    # One missing serial, with the rest present, starts the clock.
    almost = [panel(str(i), 50) for i in range(9)]
    state, events, _summary = evaluate_panels(almost, now, roster_state, timeout, repeat)
    assert events == []
    assert state["panels"]["9"]["down_since"] == now

    # dc-pwr-low while producing is normal. A different flag is a fault.
    inventory = [
        {
            "serial_num": "a",
            "communicating": True,
            "device_status": ["envoy.cond_flags.pcu_ctrl.dc-pwr-low"],
        },
        {
            "serial_num": "b",
            "communicating": True,
            "device_status": ["envoy.cond_flags.ac-voltage-oos"],
        },
    ]
    both_up = [panel("a", 40), panel("b", 35)]
    state, events, _summary = evaluate_panels(both_up, now, empty, timeout, repeat, inventory)
    assert events == []
    assert "a" not in state["panels"]
    assert state["panels"]["b"]["fault_since"] == now
    fault_later = [
        {"serialNumber": "a", "lastReportWatts": 40, "lastReportDate": later - 60},
        {"serialNumber": "b", "lastReportWatts": 35, "lastReportDate": later - 60},
    ]
    _state, events, _summary = evaluate_panels(fault_later, later, state, timeout, repeat, inventory)
    assert len(events) == 1
    assert events[0]["reasons"] == ["fault"]
    assert events[0]["flags"] == ["envoy.cond_flags.ac-voltage-oos"]

    # A night inventory is all "not communicating" and must not create faults.
    asleep = [
        {
            "serial_num": serial,
            "communicating": False,
            "device_status": ["envoy.cond_flags.obs_strs.failure"],
        }
        for serial in ("a", "b")
    ]
    state, events, _summary = evaluate_panels(both_up, now, empty, timeout, repeat, asleep)
    assert events == []
    assert state["panels"] == {}

    subject, body = format_report(
        [
            {
                "action": "alert",
                "serial": "b",
                "since": now,
                "hours": 49,
                "reasons": ["not producing"],
                "flags": [],
                "watts": 0,
                "report_age_s": 120,
            }
        ],
        {"count": 35, "producing": 34, "max_watts": 200},
        48,
    )
    assert subject == "Solar: 1 of 35 panels offline"
    assert "b" in body
    assert "48 hours" in body
    assert "@" not in body

    assert alert_recipient({"ENPHASE_EMAIL": "owner@example.com"}) == "owner@example.com"
    assert alert_recipient({"ENPHASE_EMAIL": "owner@example.com", "ALERT_EMAIL": ""}) == "owner@example.com"
    assert alert_recipient({"ENPHASE_EMAIL": "owner@example.com", "ALERT_EMAIL": "off"}) is None
    assert alert_recipient({"ALERT_EMAIL": "other@example.com"}) == "other@example.com"
    assert alert_hours({}, "ALERT_TIMEOUT_HOURS", 48) == 48
    try:
        alert_hours({"ALERT_TIMEOUT_HOURS": "0"}, "ALERT_TIMEOUT_HOURS", 48)
    except SystemExit:
        pass
    else:
        raise AssertionError("timeout of 0 hours should be rejected")

    print("alert_panels self-test ok")


def main() -> None:
    args = parse_args()
    if args.self_test:
        self_test()
        return
    run_check(load_config(), dry_run=args.dry_run)


if __name__ == "__main__":
    main()
