"""Device-flow OAuth against the Lovstudio web backend (RFC 8628).

~/.lovstudio/auth.yml holds the Supabase session:

    access_token:  <JWT, ~1h lifetime>
    refresh_token: <long-lived>
    expires_at:    <unix epoch of access_token expiry>
    user_id:       <auth.users uuid>
    email:         <display string>

We refresh on demand when a caller asks for a fresh bearer.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

import yaml

from . import config

AUTH_FILE = config.CONFIG_DIR / "auth.yml"
POLL_MAX_SECONDS = 600
REFRESH_SKEW_SEC = 60


class AuthError(RuntimeError):
    pass


def _post(url: str, body: dict, *, anon: str | None = None, bearer: str | None = None) -> dict:
    headers = {"content-type": "application/json"}
    if anon:
        headers["apikey"] = anon
        headers["authorization"] = f"Bearer {anon}"
    if bearer:
        headers["authorization"] = f"Bearer {bearer}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read())
        except Exception:
            payload = {"error": f"http {e.code}"}
        raise AuthError(f"{url}: {payload.get('error', 'unknown')}") from e


def load_auth() -> dict | None:
    if not AUTH_FILE.exists():
        return None
    return yaml.safe_load(AUTH_FILE.read_text()) or {}


def save_auth(data: dict) -> None:
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    AUTH_FILE.write_text(yaml.safe_dump(data, sort_keys=False))
    AUTH_FILE.chmod(0o600)


def wipe_auth() -> None:
    if AUTH_FILE.exists():
        AUTH_FILE.unlink()


def _save_session(session: dict[str, Any]) -> dict:
    expires_in = int(session.get("expires_in") or 3600)
    data = {
        "access_token": session["access_token"],
        "refresh_token": session["refresh_token"],
        "expires_at": int(time.time()) + expires_in,
        "user_id": session.get("user", {}).get("id"),
        "email": session.get("user", {}).get("email"),
    }
    save_auth(data)
    return data


def login(client_name: str, *, open_browser: bool = True) -> dict:
    """Run the device flow. Blocks up to POLL_MAX_SECONDS."""
    start = _post(
        f"{config.api_base()}/cli_device_start",
        {"client_name": client_name, "scope": "cli"},
        anon=config.anon_key(),
    )
    device_code = start["device_code"]
    user_code = start["user_code"]
    verify_url = start["verification_uri_complete"]
    interval = int(start.get("interval") or 5)

    print(f"→ open {verify_url}")
    print(f"  user code: {user_code}")
    if open_browser:
        try:
            webbrowser.open(verify_url)
        except Exception:
            pass
    print("  waiting for approval…", flush=True)

    deadline = time.time() + POLL_MAX_SECONDS
    while time.time() < deadline:
        time.sleep(interval)
        try:
            resp = _post(
                f"{config.api_base()}/cli_device_poll",
                {"device_code": device_code},
                anon=config.anon_key(),
            )
        except AuthError as e:
            # keep polling on transient server errors
            if "server_error" in str(e):
                continue
            raise

        err = resp.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 2
            continue
        if err == "expired_token":
            raise AuthError("user code expired — run login again")
        if err == "access_denied":
            raise AuthError("authorization denied")
        if err:
            raise AuthError(f"poll failed: {err}")

        return _save_session(resp)

    raise AuthError("login timed out after 10 minutes")


def logout() -> None:
    wipe_auth()


def whoami() -> dict | None:
    return load_auth()


def refresh_if_needed() -> dict:
    auth = load_auth()
    if not auth:
        raise AuthError("not logged in — run: lovstudio-skill-helper login")
    if int(auth.get("expires_at") or 0) - REFRESH_SKEW_SEC > int(time.time()):
        return auth
    # Supabase token endpoint: POST /auth/v1/token?grant_type=refresh_token
    url = f"{config.api_base().replace('/functions/v1', '/auth/v1')}/token?grant_type=refresh_token"
    try:
        new = _post(url, {"refresh_token": auth["refresh_token"]}, anon=config.anon_key())
    except AuthError as e:
        raise AuthError(f"refresh failed ({e}); run login again") from e
    return _save_session({
        "access_token": new["access_token"],
        "refresh_token": new["refresh_token"],
        "expires_in": new.get("expires_in", 3600),
        "user": new.get("user") or {
            "id": auth.get("user_id"),
            "email": auth.get("email"),
        },
    })


def bearer_headers() -> dict:
    """Return headers with a fresh access_token for authenticated CLI calls."""
    auth = refresh_if_needed()
    return {
        "apikey": config.anon_key(),
        "authorization": f"Bearer {auth['access_token']}",
    }
