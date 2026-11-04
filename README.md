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
| `ENVOY_HOST` | The gateway's wired address. |
| `ENVOY_HOST_WIFI` | Optional. The gateway's WiFi address. |
| `ENVOY_PRIMARY` | Which address to poll first: `wired`, `wifi`, or one of those two addresses. Default `wired`. |
| `ENVOY_SINGLE_HOST` | `true` polls only the primary address. `false` (the default) tries the other address when the primary does not connect within 5 seconds, then tries the primary again on the next sample. |
| `SAMPLE_SECONDS` | Polling frequency in seconds. Default 30. Minimum 5. `--interval` overrides this for one run. |
| `ENVOY_SERIAL` | The gateway serial. It is printed on the unit and in the Enlighten devices list. `https://GATEWAY/info` also shows it and does not need a token. |
| `ENLIGHTEN_SYSTEM_ID` | The number in the Enlighten site URL, such as `https://enlighten.enphaseenergy.com/web/1234567`. |

`ENVOY_INSTALLER_PASSWORD` and `ENVOY_PASSWORD` are notes from the pre-token days (the installer password from the Enphase Installer Toolkit, and `envoy` / the last six digits of the serial). This scraper does not send them. Firmware 7 and newer reject those digest passwords for `production.json`.

The computer clock should match the gateway timezone. Daily filenames and the Enlighten merge use `America/New_York`. For another zone, set `TZ` in the systemd units and `SITE_TZ` in `scrape_enlighten.py` and `merge_enlighten.py`.

Check the login with one sample, then print it:

```bash
python3 scrape_local.py --self-test
python3 scrape_local.py --once
python3 scrape_local.py --debug --once
python3 scrape_local.py --production-only --once
./stat-solar.sh
```

`--debug` prints each gateway response on the console. The dump is not written to the data files or to the timestamped status log. Leave it off the systemd service, because that service's stdout is the journal.

`--production-only` reads `production.json?details=1` and `/ivp/pdm/energy` on each sample. It skips the inverter list and the home, inventory, and meter snapshots. `/info` is still requested, without a token, to choose an address and to read the software version. The installed service does not pass this flag. Add `--production-only` to `ExecStart` in the user unit when that service should poll production only, then restart the user service. `scrape-solar-data.sh` forwards the extra argument.

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
python3 scrape_local.py --once                 # one sample
python3 scrape_local.py --interval 30          # loop
python3 scrape_local.py --production-only      # meter and daily energy only
./stat-solar.sh                                # today's recent watts, latest sample, max
./show-loop.sh                                 # stat-solar.sh every 30 seconds
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

Every five minutes the scraper appends per-inverter readings to `data/YYYY/MM/inverters-YYYY-MM-DD.jsonl`. Every six hours it writes `data/snapshots/home-DATE.json`, `inventory-DATE.json`, and `meters-DATE.json`. `--production-only` skips those requests. Each sample still includes the production meter and the `/ivp/pdm/energy` repair.

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
- `/api/v1/production/inverters` — every five minutes, unless `--production-only` is set
- `/home.json`, `/inventory.json`, `/ivp/meters` — every six hours, unless `--production-only` is set

`/info` is requested to choose a gateway address and to read the software version. That response is not stored. `--production-only` still requests it.

The gateway's HTTPS certificate is self-signed. Local requests skip certificate verification. Cloud requests use normal TLS.

## Panel alerts

`alert_panels.py` watches the five-minute inverter file. It mails when some panels stay offline while others are producing. A dark array is not an alert: night, heavy cloud, and snow on every panel all report 0 W.

A panel starts the clock the first time at least one other panel is producing and this one is at 0 W, missing, or still showing yesterday's watts. The clock keeps running through the night. It resets when the panel produces again. The mail waits `ALERT_TIMEOUT_HOURS` (default 48) so snow piled on part of the array for a couple of days does not alert on the first morning. While it is still down, a reminder goes out every `ALERT_REPEAT_HOURS` (default 24). When it produces again, a short recovery mail goes out.

Inventory status is included when that snapshot was taken while the panels were communicating. `dc-pwr-low`, low DC voltage, and the asleep flag `obs_strs.failure` are normal in shade and after dark. Any other status that stays set for the same timeout is a fault, even if the panel is still making a few watts.

Blank `ALERT_EMAIL` sends to `ENPHASE_EMAIL`. `ALERT_EMAIL='off'` writes the journal line and `data/alerts/panel-alerts.log` only. The suspect clock is `secrets/panel-alert.json`, which stays out of git. `--production-only` on the scraper skips inverter samples, so this check has nothing to read.

```bash
python3 alert_panels.py --self-test
python3 alert_panels.py --dry-run
mkdir -p ~/.config/systemd/user
cp alert-panels.service alert-panels.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now alert-panels.timer
```

The timer runs every 15 minutes. The scraper service does not need a restart.

## References

The local client is the Python standard library. These projects were the research for the token request and the gateway endpoints. This repository does not vendor their source.

- [pyenphase/pyenphase](https://github.com/pyenphase/pyenphase) is the library behind the Home Assistant Enphase Envoy integration. The owner token here follows that flow: `POST https://enlighten.enphaseenergy.com/login/login.json`, then `POST https://entrez.enphaseenergy.com/tokens` with the session id, the gateway serial, and the Enlighten username. That project treats firmware 7.0.0 as the first release that requires a token (`AUTH_TOKEN_MIN_VERSION`). From firmware 8.3.5433 (`PRODUCTION_TOTAL_IS_NET_CONSUMPTION`) it also repairs a meter bug in which total-consumption lifetime energy and watts copy net consumption, by adding production back onto total consumption. On the D8.3.5433 gateway this scraper was written against, the production meter's `whToday` and `whLastSevenDays` copy `whLifetime`. The daily total is taken from `production.pcu.wattHoursToday` in `/ivp/pdm/energy`. `wNow` stays the production meter's value. Paths taken from that work include `/production.json?details=1`, `/api/v1/production/inverters`, `/home.json`, `/inventory.json`, and `/ivp/meters`.

- [Matthew1471/Enphase-API](https://github.com/Matthew1471/Enphase-API) documents Enlighten cloud authentication and local gateway tokens. Its commissioned-gateway request posts to `https://entrez.enphaseenergy.com/entrez_tokens`. The token cached here comes from `POST /tokens`.

- [csmcolo/Enphase-Envoy-JWT-Tools](https://github.com/csmcolo/Enphase-Envoy-JWT-Tools) is a shell script, `envoy_generate_token.sh`, that logs in with `login.json` and posts `{session_id, serial_num, username}` to `https://entrez.enphaseenergy.com/tokens`. That is the same token request this scraper sends.

- [evilzenscientist/envoyAPI](https://github.com/evilzenscientist/envoyAPI) walks through the same login, `POST https://entrez.enphaseenergy.com/tokens`, and local bearer use in `GetEnphaseToken.py`.

Firmware before 7 accepted HTTP digest. The `envoy` password was the last six digits of the serial, and the installer password came from the Enphase Installer Toolkit Android app. Firmware 7 and later require the owner token for `production.json`. This repository does not calculate those passwords. `secrets.env.sample` still lists the old names as unused notes.

The 15-minute cloud archive was read from the Enlighten website while logged in: `lifetime_energy.json` and `daily_energy.json` under `https://enlighten.enphaseenergy.com/pv/systems/<id>/`. A request for `energy_lifetime.json` returns the website HTML. Monitoring API v4 needs a separate developer key. This project uses the website session for that archive.

## License

MIT. See [LICENSE](LICENSE). Copyright (c) 2026 Don J. Rude.
