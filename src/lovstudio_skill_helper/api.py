"""Signed requests to the Lovstudio licensing Edge Functions.

Protocol mirrors OpenClacky:
    proof = HMAC_SHA256(license_key, f"{action}:{key_hash}:{user_id}:{device_id}:{timestamp}:{nonce}{extra}")

The license_key itself is NEVER sent over the wire — only key_hash + proof.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import urllib.error
import urllib.request

from . import __version__, config


USER_AGENT = f"lovstudio-skill-helper/{__version__} (+https://lovstudio.ai)"


def hmac_hex(key_hex: str, message: str) -> str:
    return hmac.new(bytes.fromhex(key_hex), message.encode(), hashlib.sha256).hexdigest()


def key_hash(license_key_hex: str) -> str:
    return hashlib.sha256(bytes.fromhex(license_key_hex)).hexdigest()


def parse_user_id_from_key(license_key_hex: str) -> int:
    """First 8 hex chars of the license key = user_id. Server validates independently."""
    return int(license_key_hex[:8], 16)


def signed_payload(
    license_key: str,
    action: str,
    device_id: str,
    extra_suffix: str = "",
    extra_fields: dict | None = None,
) -> dict:
    kh = key_hash(license_key)
    uid = str(parse_user_id_from_key(license_key))
    ts = str(int(time.time()))
    nonce = secrets.token_hex(16)
    msg = f"{action}:{kh}:{uid}:{device_id}:{ts}:{nonce}{extra_suffix}"
    proof = hmac_hex(license_key, msg)
    payload = {
        "key_hash": kh,
        "user_id": uid,
        "device_id": device_id,
        "timestamp": ts,
        "nonce": nonce,
        "proof": proof,
    }
    if extra_fields:
        payload.update(extra_fields)
    return payload


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str, *, code: str | None = None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.code = code or message

    @classmethod
    def from_http(cls, error: urllib.error.HTTPError) -> ApiError:
        # Never print the response body: a proxy may return HTML or echo secrets.
        body = error.read(65536).decode("utf-8", errors="replace")
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        headers = error.headers or {}
        ray = headers.get("cf-ray") or payload.get("ray_id")
        code = payload.get("error") or payload.get("message")
        if payload.get("cloudflare_error") or (
            ray and ("cloudflare" in body.lower() or "1010" in body)
        ) or headers.get("cf-mitigated") == "challenge":
            name = payload.get("error_name") or "request_blocked"
            number = payload.get("error_code")
            message = f"Cloudflare HTTP {error.code}: {name}"
            if number:
                message += f" (Error {number})"
            message += "; website protection blocked the request; entitlement was not checked"
            code = "website_protection_blocked"
        elif isinstance(code, str) and code:
            message = code
        else:
            code = "unexpected_http_response"
            message = f"unexpected HTTP {error.code} response; entitlement could not be verified"
        if ray:
            message += f"; Ray ID: {str(ray)[:100]}"
        return cls(error.code, message, code=code)


def web_call(path: str, body: dict, bearer: str, timeout: int = 15) -> dict:
    """Call an authenticated Lovstudio web API route with the account JWT."""
    headers = {
        "user-agent": USER_AGENT,
        "content-type": "application/json",
        "accept": "application/json",
        "authorization": f"Bearer {bearer}",
    }
    req = urllib.request.Request(
        f"{config.web_base()}{path}",
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise ApiError.from_http(e) from None


def account_skill_key(bearer: str, skill_name: str, skill_version: str) -> dict:
    """Fetch a paid Skill key through the account entitlement bridge."""
    return web_call(
        "/api/skills/key",
        {"skill_name": skill_name, "skill_version": skill_version},
        bearer,
    )


def call(path: str, body: dict, timeout: int = 15, bearer: str | None = None) -> dict:
    # A user JWT (from device-flow login) is also a valid Supabase JWT, so it
    # clears the Functions gateway. If we don't have one, fall back to anon.
    headers = {
        "user-agent": USER_AGENT,
        "content-type": "application/json",
        "apikey": config.anon_key(),
        "authorization": f"Bearer {bearer or config.anon_key()}",
    }
    req = urllib.request.Request(
        f"{config.api_base()}/{path}",
        data=json.dumps(body).encode(),
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise ApiError.from_http(e) from None


def activate(license_key: str, device_id: str, bearer: str | None = None) -> dict:
    payload = signed_payload(
        license_key, "activate", device_id,
        extra_fields={"device_info": config.device_info()},
    )
    return call("activate", payload, bearer=bearer)


def heartbeat(license_key: str, device_id: str) -> dict:
    return call("heartbeat", signed_payload(license_key, "heartbeat", device_id))


def skill_keys(license_key: str, device_id: str, skill_name: str, skill_version: str) -> dict:
    suffix = f":{skill_name}:{skill_version}"
    payload = signed_payload(
        license_key, "skill_keys", device_id,
        extra_suffix=suffix,
        extra_fields={"skill_name": skill_name, "skill_version": skill_version},
    )
    return call("skill_keys", payload)


def skill_call(
    license_key: str,
    device_id: str,
    skill_name: str,
    skill_version: str,
    op: str,
    input_data: dict,
) -> dict:
    """Invoke a cloud-split skill's server-side handler.

    Returns the handler's `output` payload verbatim. Core logic runs on the
    server; the client only ever sees structured data, never the implementation.
    """
    suffix = f":{skill_name}:{skill_version}:{op}"
    payload = signed_payload(
        license_key, "skill_call", device_id,
        extra_suffix=suffix,
        extra_fields={
            "skill_name": skill_name,
            "skill_version": skill_version,
            "op": op,
            "input": input_data,
        },
    )
    return call("skill_call", payload)


def list_catalog(timeout: int = 15) -> list[dict]:
    """Public catalog of all skills (no auth). Returns [{name, category, paid}, ...]."""
    url = f"{config.rest_base()}/skills?select=name,category,paid"
    req = urllib.request.Request(
        url,
        headers={
            "user-agent": USER_AGENT,
            "apikey": config.anon_key(),
            "authorization": f"Bearer {config.anon_key()}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise ApiError.from_http(e) from None
