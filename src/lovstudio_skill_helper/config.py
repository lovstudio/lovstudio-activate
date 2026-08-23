"""On-disk layout for activated state.

~/.lovstudio/
├── device.yml                     # device_id (shared by every license on this machine)
└── license.yml                    # stackable licenses — schema v2:
                                   #   licenses: [ { license_key, user_id, expires_at,
                                   #                 entitled_skills, last_heartbeat_at }, … ]
                                   # Legacy v1 (flat single license) is auto-migrated on read.

Encrypted skill bundles normally live under the installer-owned canonical
directory `~/.agents/skills/<runtime-name>/`, with optional Agent-specific
copies or links such as `~/.codex/skills/` and `~/.claude/skills/`.

Decryption keys are NEVER persisted here. They live in the running CLI's
memory for the duration of one `decrypt` or `exec` invocation, then die.
"""
from __future__ import annotations

import os
import platform
import uuid
from pathlib import Path
from typing import Optional

import yaml

CONFIG_DIR = Path(os.environ.get("LOVSTUDIO_HOME", Path.home() / ".lovstudio"))
LICENSE_FILE = CONFIG_DIR / "license.yml"
DEVICE_FILE = CONFIG_DIR / "device.yml"

# Default Edge Function endpoint. Overridable via env for dev/test.
# Points at the lovstudio.ai web project (merged license system).
DEFAULT_API_BASE = "https://nouchjcfeoobplxkwasg.supabase.co/functions/v1"
DEFAULT_WEB_BASE = "https://lovstudio.ai"
# Default anon key — Edge Functions require it for JWT gate, even though
# we enforce real auth via HMAC inside the function body.
DEFAULT_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im5vdWNoamNmZW9vYnBseGt3YXNnIiwicm9sZSI6"
    "ImFub24iLCJpYXQiOjE3NjYxNjI1OTMsImV4cCI6MjA4MTczODU5M30."
    "P3A_AoAjp0EXIafeBBeqp972h_lO7oXjbKgu0OdMsjA"
)


def api_base() -> str:
    return os.environ.get("LOVSTUDIO_API_BASE", DEFAULT_API_BASE)


def web_base() -> str:
    return os.environ.get("LOVSTUDIO_WEB_URL", DEFAULT_WEB_BASE).rstrip("/")


def rest_base() -> str:
    """PostgREST base URL — derived from api_base() by stripping the Edge
    Functions suffix. Overridable via env for dev/test.
    """
    override = os.environ.get("LOVSTUDIO_REST_BASE")
    if override:
        return override
    base = api_base()
    suffix = "/functions/v1"
    root = base[: -len(suffix)] if base.endswith(suffix) else base
    return f"{root}/rest/v1"


def anon_key() -> str:
    return os.environ.get("LOVSTUDIO_ANON_KEY", DEFAULT_ANON_KEY)


def _migrate_legacy(raw: dict) -> dict:
    """v1 single-license → v2 stackable shape.

    v1: {license_key, device_id, user_id, expires_at, entitled_skills, last_heartbeat_at}
    v2: {licenses: [ {license_key, user_id, expires_at, entitled_skills, last_heartbeat_at} ]}
        plus `device_id` moves to ~/.lovstudio/device.yml (machine-scoped).
    """
    if "licenses" in raw:
        return raw
    if not raw.get("license_key"):
        return {"licenses": []}
    legacy = {
        "license_key": raw["license_key"],
        "user_id": raw.get("user_id"),
        "expires_at": raw.get("expires_at"),
        "entitled_skills": raw.get("entitled_skills") or [],
        "last_heartbeat_at": raw.get("last_heartbeat_at"),
    }
    # Preserve the legacy device_id — it's already bound to the server row.
    if raw.get("device_id") and not DEVICE_FILE.exists():
        _write_device_id(raw["device_id"])
    return {"licenses": [legacy]}


def load_licenses() -> list[dict]:
    """Return all stacked licenses. Empty list when nothing is activated."""
    if not LICENSE_FILE.exists():
        return []
    raw = yaml.safe_load(LICENSE_FILE.read_text()) or {}
    return list(_migrate_legacy(raw).get("licenses") or [])


def save_licenses(licenses: list[dict]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LICENSE_FILE.write_text(yaml.safe_dump({"licenses": licenses}, sort_keys=False, allow_unicode=True))
    # Restrict to owner-read/write — the key_secret equivalent is stored here.
    LICENSE_FILE.chmod(0o600)


def upsert_license(entry: dict) -> list[dict]:
    """Insert or replace a license entry by license_key. Returns updated list."""
    key = entry["license_key"]
    licenses = load_licenses()
    out = [lic for lic in licenses if lic.get("license_key") != key]
    out.append(entry)
    save_licenses(out)
    return out


def remove_license(license_key: str) -> bool:
    licenses = load_licenses()
    kept = [lic for lic in licenses if lic.get("license_key") != license_key]
    if len(kept) == len(licenses):
        return False
    save_licenses(kept)
    return True


# ── Back-compat shim ──────────────────────────────────────────────────────
# Some external callers / tests may still import load_license/save_license.
# Keep them working, but have them read/write the first element of the list.

def load_license() -> dict | None:
    licenses = load_licenses()
    if not licenses:
        return None
    first = dict(licenses[0])
    first["device_id"] = device_id()
    return first


def save_license(data: dict) -> None:
    entry = {k: v for k, v in data.items() if k != "device_id"}
    upsert_license(entry)


def wipe_license() -> None:
    if LICENSE_FILE.exists():
        LICENSE_FILE.unlink()


# ── Device identity (machine-scoped, shared by all stacked licenses) ───────

def _write_device_id(did: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DEVICE_FILE.write_text(yaml.safe_dump({"device_id": did}, sort_keys=False))
    DEVICE_FILE.chmod(0o600)


def device_id() -> str:
    """Stable device id. One per machine, shared across every activated license,
    so `max_devices=1` isn't eaten by a single user stacking multiple keys.
    """
    if DEVICE_FILE.exists():
        raw = yaml.safe_load(DEVICE_FILE.read_text()) or {}
        did = raw.get("device_id")
        if did:
            return did
    did = uuid.uuid4().hex
    _write_device_id(did)
    return did


def generate_device_id() -> str:
    """Deprecated. Kept for back-compat; delegates to `device_id()`."""
    return device_id()


def device_info() -> dict:
    return {
        "os": platform.system().lower(),
        "os_version": platform.release(),
        "hostname": platform.node(),
        "python": platform.python_version(),
    }


def _skill_name_candidates(skill_name: str) -> list[str]:
    """Return current runtime, product, and legacy directory-name aliases."""
    raw = skill_name.strip()
    if raw.startswith("lov-"):
        product = raw[len("lov-"):]
    elif raw.startswith("lovstudio-"):
        product = raw[len("lovstudio-"):]
    elif raw.startswith("lovstudio:"):
        product = raw[len("lovstudio:"):]
    else:
        product = raw

    ordered = [f"lov-{product}", product, f"lovstudio-{product}", f"lovstudio:{product}"]
    if raw not in ordered:
        ordered.insert(0, raw)
    return list(dict.fromkeys(name for name in ordered if name))


def skill_roots(home: Optional[Path] = None) -> list[Path]:
    """Installer canonical root first, followed by known Agent-specific roots."""
    root = home or Path.home()
    return [
        root / ".agents" / "skills",
        root / ".codex" / "skills",
        root / ".claude" / "skills",
        root / ".config" / "opencode" / "skills",
        root / ".gemini" / "skills",
        root / ".cursor" / "skills",
        root / ".windsurf" / "skills",
    ]


def skill_dir_candidates(skill_name: str, home: Optional[Path] = None) -> list[Path]:
    """Search current installer and legacy Agent locations in priority order."""
    names = _skill_name_candidates(skill_name)
    return [root / name for root in skill_roots(home) for name in names]


def skill_dir(skill_name: str, home: Optional[Path] = None) -> Path:
    """Locate an encrypted skill bundle, returning the first candidate that
    contains a MANIFEST.enc.json. Falls back to the primary path so callers
    can render a sane error message.
    """
    for c in skill_dir_candidates(skill_name, home):
        if (c / "MANIFEST.enc.json").exists():
            return c
    return skill_dir_candidates(skill_name, home)[0]


def installed_skills(home: Optional[Path] = None) -> list[str]:
    """List canonical product names from encrypted manifests across Agent roots."""
    import json

    names: set[str] = set()
    for root in skill_roots(home):
        if not root.is_dir():
            continue
        for child in root.iterdir():
            manifest = child / "MANIFEST.enc.json"
            if not child.is_dir() or not manifest.exists():
                continue
            try:
                data = json.loads(manifest.read_text())
            except Exception:
                data = {}
            canonical = str(data.get("skill_name") or "").strip()
            if not canonical:
                canonical = _skill_name_candidates(child.name)[1]
            names.add(canonical)
    return sorted(names)


def list_skill_files(skill_name: str) -> list[str]:
    """List relative paths inside an installed skill's MANIFEST. Empty list on
    any error — this is only used for shell completion, never hard-fails.
    """
    import json

    d = skill_dir(skill_name)
    manifest_path = d / "MANIFEST.enc.json"
    if not manifest_path.exists():
        return []
    try:
        data = json.loads(manifest_path.read_text())
    except Exception:
        return []
    files = data.get("files")
    if not isinstance(files, dict):
        return []
    return sorted(files.keys())
