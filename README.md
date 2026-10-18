# Enphase IQ Gateway scraper

Record production from a local Enphase IQ Gateway (also called an Envoy) about every 30 seconds, and optionally fill gaps from Enlighten's 15-minute cloud history.

Current IQ Gateway firmware does not serve `production.json` to an anonymous request. This project logs in to [Enlighten](https://enlighten.enphaseenergy.com/) with the homeowner account, asks [Entrez](https://entrez.enphaseenergy.com/) for an owner token for that gateway's serial number, and sends the token on each local request. A homeowner token lasts about a year. The scraper caches it and refreshes it before it expires, or when the gateway returns 401.

Tested against an IQ Gateway on firmware D8.3.5433 with microinverters and no batteries. Python 3.10 or newer is enough. There are no packages to install.

## Setup

You need `python3`, `jq`, and `flock` (util-linux). The gateway must be reachable on your LAN, and the Enlighten account must be able to log in without a multi-factor prompt. An MFA challenge stops an unattended token refresh.

```bash
git clone https://github.com/RudeDude/enphase-solar-scraper.git
cd enphase-solar-scraper
cp secrets.env.sample secrets.env
chmod 600 secrets.env
```

Edit `secrets.env`. Single-quote any value that contains spaces or shell characters. Do not add an inline comment on a value line.

| Variable | Where to find it |
| --- | --- |
| `ENPHASE_EMAIL`, `ENPHASE_PASSWORD` | The Enlighten login for this system. |
| `ENVOY_HOST` | The gateway's LAN address (the DHCP reservation or the sticker). |
| `ENVOY_SERIAL` | The gateway serial. It is printed on the unit and in the Enlighten devices list. `https://GATEWAY/info` also shows it and does not need a token. |
| `ENLIGHTEN_SYSTEM_ID` | The number in the Enlighten site URL, such as `https://enlighten.enphaseenergy.com/web/1234567`. |
| `SAMPLE_SECONDS` | Seconds between local samples. Default 30. |

`ENVOY_INSTALLER_PASSWORD` and `ENVOY_PASSWORD` are notes from the pre-token days (the installer password from the Enphase Installer Toolkit, and `envoy` / the last six digits of the serial). This scraper does not send them. Firmware 7 and newer reject those digest passwords for `production.json`.

The computer clock should match the gateway timezone. Daily filenames and the Enlighten merge use `America/New_York`. For another zone, set `TZ` in the systemd units and `SITE_TZ` in `scrape_enlighten.py` and `merge_enlighten.py`.

Check the login with one sample, then print it:

```bash
python3 scrape_local.py --self-test
python3 scrape_local.py --once
./stat-solar.sh
```

`secrets.env`, `secrets/`, and `data/` are gitignored. Keep them that way. `private-unit-info_SAMPLE.txt` is only a notepad for site details. The scripts do not read it.

## Run it all the time

`scrape-solar-data.sh` holds a lock and starts `scrape_local.py`. A second copy exits immediately, so install one service.

The unit in this repo is a **user** service. `loginctl enable-linger` keeps it running when you are logged out.

```bash
mkdir -p ~/.config/systemd/user
cp scrape-solar-data.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now scrape-solar-data.service
loginctl enable-linger "$USER"
journalctl --user -u scrape-solar-data.service -f
```

If the clone is not `~/src/enphase-solar-scraper`, change `WorkingDirectory` and `ExecStart` in the unit before enabling it. Set `TZ` to the gateway's zone.

Do not also install a system-wide unit that runs the same script or an older copy. Two scrapers will fight over the gateway, and a script that calls `production.json` without a token will exit on the 401 HTML page and restart forever.

Useful commands:

```bash
python3 scrape_local.py --once          # one sample
python3 scrape_local.py --interval 30   # loop
./stat-solar.sh                         # today's recent watts, latest sample, max
./show-loop.sh                          # stat-solar.sh every 30 seconds
```

## Data files

Samples land in `data/YYYY/MM/solar-log-YYYY-MM-DD.json`, one JSON object per line. The date is the computer's local date. Each line is the production meter from `https://GATEWAY/production.json?details=1` (`type` `eim`, `measurementType` `production`).

Fields the status script uses:

| Field | Meaning |
| --- | --- |
| `wNow` | Watts now. |
| `whToday` | Watt-hours since local midnight. |
| `whLifetime` | Lifetime watt-hours on the production meter. |
| `rmsVoltage`, `rmsCurrent` | Line voltage and current. |
| `reactPwr`, `apprntPwr`, `vahToday` | Reactive power, apparent power, volt-amp-hours today. |
| `readingTime` | Gateway timestamp, Unix seconds. |

On firmware D8.3.5433 the production meter copies lifetime energy into `whToday` and `whLastSevenDays`. When that happens the scraper keeps the raw numbers in `whTodayRaw` and `whLastSevenDaysRaw`, and sets `whToday` from the microinverters' `wattHoursToday` (`whTodaySource` is `pdm.pcu`). `wNow` still comes from the production meter. After sunset, `wNow` near 0 with line voltage still present is a real reading. The microinverters stop talking after dark. An inverter counts as reporting if it spoke in the last 16 hours, so the evening summary still shows the whole array.

Every five minutes the scraper appends per-inverter readings to `data/YYYY/MM/inverters-YYYY-MM-DD.jsonl`. Every six hours it writes `data/snapshots/home-DATE.json`, `inventory-DATE.json`, and `meters-DATE.json`.

`jq` reads the daily file directly:

```bash
jq '.wNow' data/2026/10/solar-log-2026-10-18.json | tail
tail -n 1 data/2026/10/solar-log-2026-10-18.json | jq '{wNow, whToday, rmsVoltage, whLifetime}'
```

## Enlighten history (optional)

Enlighten keeps 15-minute watt-hour bins for the life of the system, which is finer than the hourly charts on the website. Use it to fill days the local scraper missed. Skip it if the local service has been running the whole time.

```bash
python3 scrape_enlighten.py --full    # daily totals and 15-minute bins from the first day
python3 merge_enlighten.py            # insert bins into the solar-log files
```

`scrape_enlighten.py` with no arguments refreshes the lifetime series and the last 45 days. `--from` and `--to` take `YYYY-MM-DD`. The archive is `data/enlighten/`:

- `lifetime-energy.json` and `daily-wh.jsonl` — one watt-hour total per day
- `intervals/YYYY-MM.json` — 15-minute watt-hours
- `local-gaps.txt` — cloud days that have no solar-log file
- `inverters.json`, `devices.json` — latest roster

`merge_enlighten.py` keeps every local sample. It adds a row only for a 15-minute window that contains no local reading. Those rows have `"source": "enlighten"`. `wNow` on a cloud row is the average watts for that bin (watt-hours × 4 for a 15-minute bin), so a cloud row does not replace a higher instantaneous peak from the gateway. Voltage, current, and the other meter channels are JSON `null` on cloud rows. Run the merge again after a later download. It replaces the cloud rows and leaves the local ones.

While today is still being recorded, cloud rows stop at the latest local sample, so `stat-solar.sh` keeps printing the live gateway line.

`scrape-enlighten.timer` runs that download every day at 21:30. Leave it disabled unless you want the catch-up. The official Enlighten Monitoring API v4 is a separate developer key and is not used here.

## What gets stored on each local poll

- `production.json?details=1` — the production energy meter, every sample
- `/ivp/pdm/energy` — microinverter watt-hours today, used to repair `whToday` on the affected firmware
- `/api/v1/production/inverters` — every five minutes
- `/home.json`, `/inventory.json`, `/ivp/meters` — every six hours

The gateway's HTTPS certificate is self-signed. Local requests skip certificate verification. Cloud requests use normal TLS.
