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

from . import config


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
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


def call(path: str, body: dict, timeout: int = 15) -> dict:
    req = urllib.request.Request(
        f"{config.api_base()}/{path}",
        data=json.dumps(body).encode(),
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {config.anon_key()}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read()).get("error", "unknown error")
        except Exception:
            err_body = "unknown error"
        raise ApiError(e.code, err_body) from None


def activate(license_key: str, device_id: str) -> dict:
    payload = signed_payload(
        license_key, "activate", device_id,
        extra_fields={"device_info": config.device_info()},
    )
    return call("activate", payload)


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
