"""Shared config, Enlighten login, and Envoy token handling.

secrets.env is read from the repo root. It is gitignored. Nothing in this
module prints the password, the session id, or the token.
"""

from __future__ import annotations

import base64
import fcntl
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from http.cookiejar import CookieJar
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOGIN_URL = "https://enlighten.enphaseenergy.com/login/login.json"
TOKEN_URL = "https://entrez.enphaseenergy.com/tokens"
USER_AGENT = "enphase-solar-scraper/1.0"


class AuthError(RuntimeError):
    """Enlighten rejected a login or Entrez did not return a usable token."""


def log(message: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)


def brief_body(body: bytes, limit: int = 180) -> str:
    text = body[:limit].decode("utf-8", "replace").replace("\n", " ").strip()
    if text.startswith("eyJ") and text.count(".") >= 2:
        return "<jwt>"
    return text


def load_config(path: Path | None = None) -> dict[str, str]:
    """Read KEY=value lines from secrets.env. Values may be single-quoted."""
    secrets = path or (ROOT / "secrets.env")
    if not secrets.is_file():
        raise SystemExit(
            f"Missing {secrets.name}. Copy secrets.env.sample to secrets.env and fill it in. "
            "That file stays out of git."
        )
    values: dict[str, str] = {}
    for raw in secrets.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    required = (
        "ENPHASE_EMAIL",
        "ENPHASE_PASSWORD",
        "ENVOY_HOST",
        "ENVOY_SERIAL",
        "ENLIGHTEN_SYSTEM_ID",
    )
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise SystemExit(f"secrets.env is missing {', '.join(missing)}")
    return values


def data_root(cfg: dict[str, str]) -> Path:
    raw = Path(cfg.get("DATA_DIR") or "data")
    if not raw.is_absolute():
        raw = ROOT / raw
    return raw


@contextmanager
def solar_log_lock():
    """Hold secrets/solar-log.lock across one solar-log append or rewrite.

    The scraper keeps secrets/scrape.lock for its whole life so a second copy
    exits. This lock is released between samples, so the Enlighten merge can
    replace today's file without dropping a line the scraper just wrote.
    """
    folder = ROOT / "secrets"
    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder / "solar-log.lock"
    handle = path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def jwt_claims(token: str) -> dict:
    part = token.split(".")[1]
    part += "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(part))


def token_audience_matches(claims: dict, serial: str) -> bool:
    audience = claims.get("aud")
    if isinstance(audience, list):
        return serial in {str(item) for item in audience}
    return str(audience) == serial


class HttpClient:
    """Cloud calls keep an Enlighten cookie jar. Local calls skip certificate checks."""

    def __init__(self) -> None:
        self.jar = CookieJar()
        self._cloud = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
            urllib.request.HTTPCookieProcessor(self.jar),
        )
        self._local_ssl = ssl._create_unverified_context()

    def request(
        self,
        url: str,
        *,
        data: dict | None = None,
        json_body: dict | None = None,
        headers: dict | None = None,
        local: bool = False,
        timeout: float = 30,
        max_bytes: int = 8_000_000,
    ) -> tuple[int, bytes]:
        header_map = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain;q=0.8",
        }
        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode()
            header_map["Content-Type"] = "application/json"
        elif data is not None:
            body = urllib.parse.urlencode(data).encode()
            header_map["Content-Type"] = "application/x-www-form-urlencoded"
        if headers:
            header_map.update(headers)
        request = urllib.request.Request(url, data=body, headers=header_map)
        try:
            if local:
                response = urllib.request.urlopen(
                    request, context=self._local_ssl, timeout=timeout
                )
            else:
                response = self._cloud.open(request, timeout=timeout)
            with response:
                return response.status, response.read(max_bytes)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(max_bytes)
        except Exception as exc:
            return 0, f"{type(exc).__name__}: {exc}".encode()


def enlighten_login(client: HttpClient, cfg: dict[str, str]) -> dict:
    """Log in to Enlighten. Leaves session cookies on client for later cloud calls."""
    status, body = client.request(
        LOGIN_URL,
        data={
            "user[email]": cfg["ENPHASE_EMAIL"],
            "user[password]": cfg["ENPHASE_PASSWORD"],
        },
        timeout=45,
    )
    if status != 200:
        raise AuthError(f"Enlighten login HTTP {status}: {brief_body(body)}")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise AuthError(f"Enlighten login was not JSON: {brief_body(body)}") from exc
    if payload.get("message") != "success" or not payload.get("session_id"):
        raise AuthError(
            "Enlighten login did not return a session "
            f"(message={payload.get('message')!r}, keys={sorted(payload)}). "
            "A multi-factor prompt would block an unattended token refresh."
        )
    return payload


def obtain_token(client: HttpClient, cfg: dict[str, str]) -> dict:
    """Owner JWT for the local gateway. Valid for about a year on a homeowner account."""
    login = enlighten_login(client, cfg)
    status, body = client.request(
        TOKEN_URL,
        json_body={
            "session_id": login["session_id"],
            "serial_num": cfg["ENVOY_SERIAL"],
            "username": cfg["ENPHASE_EMAIL"],
        },
        timeout=45,
    )
    if status != 200:
        raise AuthError(f"Entrez token HTTP {status}: {brief_body(body)}")
    token = body.decode("utf-8", "replace").strip().strip('"')
    if token.count(".") != 2:
        raise AuthError(f"Entrez did not return a JWT: {brief_body(body)}")
    claims = jwt_claims(token)
    if not token_audience_matches(claims, cfg["ENVOY_SERIAL"]):
        raise AuthError(
            f"Token audience {claims.get('aud')!r} does not match envoy {cfg['ENVOY_SERIAL']}"
        )
    if "exp" not in claims:
        raise AuthError("Token has no exp claim")
    return {
        "token": token,
        "exp": int(claims["exp"]),
        "role": claims.get("enphaseUser"),
        "obtained_at": int(time.time()),
    }


class TokenStore:
    """Cache the Envoy JWT in secrets/ and refresh it before it expires or after a 401."""

    def __init__(self, client: HttpClient, cfg: dict[str, str]) -> None:
        self.client = client
        self.cfg = cfg
        self.path = ROOT / "secrets" / "envoy-token.json"
        self.token: str | None = None
        self.exp = 0
        self.last_obtain = 0.0
        # Set when the gateway rejects the current token so the next pass
        # logs in again instead of reusing a JWT that still has a year left.
        self.rejected = False

    def invalidate(self) -> None:
        self.token = None
        self.exp = 0
        self.rejected = True

    def ensure(self, force: bool = False) -> str:
        force = force or self.rejected
        margin = float(self.cfg.get("TOKEN_REFRESH_DAYS") or 14) * 86400
        now = time.time()
        if not force and self.token and self.exp - now > margin:
            return self.token
        cached = self._read()
        cached_ok = (
            cached is not None
            and cached.get("serial") == self.cfg["ENVOY_SERIAL"]
            and float(cached.get("exp") or 0) > now + 120
        )
        if not force and cached_ok and float(cached["exp"]) - now > margin:
            self.token = cached["token"]
            self.exp = int(cached["exp"])
            return self.token
        if force and now - self.last_obtain < 60:
            raise AuthError(
                "The gateway rejected a token that was just issued. "
                "Waiting before logging in to Enlighten again."
            )
        try:
            got = obtain_token(self.client, self.cfg)
        except AuthError:
            if cached_ok and not force:
                log("token refresh failed; using the cached token until it expires")
                self.token = cached["token"]
                self.exp = int(cached["exp"])
                return self.token
            raise
        self.last_obtain = time.time()
        self.token = got["token"]
        self.exp = int(got["exp"])
        self.rejected = False
        self._write(got)
        days_left = round((self.exp - now) / 86400, 1)
        log(f"envoy token refreshed role={got.get('role')} expires_in_days={days_left}")
        return self.token

    def _read(self) -> dict | None:
        if not self.path.is_file():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or not payload.get("token"):
            return None
        return payload

    def _write(self, got: dict) -> None:
        self.path.parent.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        payload = {
            "token": got["token"],
            "exp": got["exp"],
            "role": got.get("role"),
            "serial": self.cfg["ENVOY_SERIAL"],
            "obtained_at": got["obtained_at"],
        }
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)


def apply_firmware_fix(eim: dict, pdm: dict | None) -> dict:
    """Keep the historical production.json meter fields, and repair D8.3.5433 daily energy.

    On this gateway (D8.3.5433, build 513fe4) the production meter's whToday and
    whLastSevenDays are copies of whLifetime. The microinverter counter in
    /ivp/pdm/energy (production.pcu) matches Enlighten's daily total. The meter's
    own watts in production.json are still the live reading; the pdm eim wattsNow
    value can stay stale after sunset, so it is recorded and not used as wNow.
    """
    out = dict(eim)
    production = (pdm or {}).get("production") or {}
    pcu = production.get("pcu") or {}
    eim_pdm = production.get("eim") or {}
    if pcu:
        out["pcu"] = {
            "wattsNow": pcu.get("wattsNow"),
            "wattHoursToday": pcu.get("wattHoursToday"),
            "wattHoursSevenDays": pcu.get("wattHoursSevenDays"),
            "wattHoursLifetime": pcu.get("wattHoursLifetime"),
        }
    if eim_pdm:
        out["pdmEim"] = {
            "wattsNow": eim_pdm.get("wattsNow"),
            "wattHoursToday": eim_pdm.get("wattHoursToday"),
            "wattHoursSevenDays": eim_pdm.get("wattHoursSevenDays"),
            "wattHoursLifetime": eim_pdm.get("wattHoursLifetime"),
        }
    lifetime = out.get("whLifetime")
    today = out.get("whToday")
    if not isinstance(lifetime, (int, float)) or not isinstance(today, (int, float)):
        return out
    if lifetime <= 1000:
        return out
    if abs(today - lifetime) > max(1.0, abs(lifetime) * 0.001):
        return out
    out["whTodayRaw"] = today
    seven = out.get("whLastSevenDays")
    if isinstance(seven, (int, float)) and abs(seven - lifetime) <= max(1.0, abs(lifetime) * 0.001):
        out["whLastSevenDaysRaw"] = seven
        if pcu.get("wattHoursSevenDays") is not None:
            out["whLastSevenDays"] = pcu["wattHoursSevenDays"]
    if pcu.get("wattHoursToday") is not None:
        out["whToday"] = pcu["wattHoursToday"]
        out["whTodaySource"] = "pdm.pcu"
    else:
        out["whTodaySource"] = "unusable-mirrors-lifetime"
    return out


def self_test() -> None:
    eim = {
        "type": "eim",
        "measurementType": "production",
        "whLifetime": 89375942.441,
        "whToday": 89375942.441,
        "whLastSevenDays": 89375942.441,
        "wNow": 0.0,
    }
    pdm = {
        "production": {
            "pcu": {
                "wattsNow": 0,
                "wattHoursToday": 35840,
                "wattHoursSevenDays": 201540,
                "wattHoursLifetime": 32466685,
            },
            "eim": {"wattsNow": 1382, "wattHoursToday": 0, "wattHoursLifetime": 78766679},
        }
    }
    fixed = apply_firmware_fix(eim, pdm)
    assert fixed["whToday"] == 35840, fixed["whToday"]
    assert fixed["whTodayRaw"] == 89375942.441
    assert fixed["whLastSevenDays"] == 201540
    assert fixed["wNow"] == 0.0
    assert fixed["whLifetime"] == 89375942.441
    assert fixed["whTodaySource"] == "pdm.pcu"
    assert fixed["pdmEim"]["wattsNow"] == 1382

    sane = apply_firmware_fix(
        {"whLifetime": 100000, "whToday": 40000, "whLastSevenDays": 200000, "wNow": 10},
        pdm,
    )
    assert sane["whToday"] == 40000
    assert "whTodayRaw" not in sane
    assert sane["wNow"] == 10
    print("enphase_common self-test ok")
