"""On-disk layout for activated state.

~/.lovstudio/
└── license.yml                    # license_key, device_id, activated_at, expires_at,
                                   # last_heartbeat_at, entitled_skills

Encrypted skill bundles live under ~/.claude/skills/<name>/ (or the
`lovstudio-<name>/` variant), placed there by `npx skills add ...`.

Decryption keys are NEVER persisted here. They live in the running CLI's
memory for the duration of one `decrypt` or `exec` invocation, then die.
"""
from __future__ import annotations

import os
import platform
import uuid
from pathlib import Path

import yaml

CONFIG_DIR = Path(os.environ.get("LOVSTUDIO_HOME", Path.home() / ".lovstudio"))
LICENSE_FILE = CONFIG_DIR / "license.yml"

# Default Edge Function endpoint. Overridable via env for dev/test.
# Points at the lovstudio.ai web project (merged license system).
DEFAULT_API_BASE = "https://nouchjcfeoobplxkwasg.supabase.co/functions/v1"
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


def load_license() -> dict | None:
    if not LICENSE_FILE.exists():
        return None
    return yaml.safe_load(LICENSE_FILE.read_text()) or {}


def save_license(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LICENSE_FILE.write_text(yaml.safe_dump(data, sort_keys=False))
    # Restrict to owner-read/write — the key_secret equivalent is stored here.
    LICENSE_FILE.chmod(0o600)


def wipe_license() -> None:
    if LICENSE_FILE.exists():
        LICENSE_FILE.unlink()


def generate_device_id() -> str:
    """Stable-ish device id. Not privacy-sensitive; mirrors OpenClacky approach."""
    return uuid.uuid4().hex


def device_info() -> dict:
    return {
        "os": platform.system().lower(),
        "os_version": platform.release(),
        "hostname": platform.node(),
        "python": platform.python_version(),
    }


def skill_dir_candidates(skill_name: str) -> list[Path]:
    """Search candidates for an encrypted skill bundle, in priority order.

    1. ~/.claude/skills/<name>/                ← `npx skills add` with bare name
    2. ~/.claude/skills/lovstudio-<name>/      ← `npx skills add` with namespaced name
                                                  (free skills + paid skills both land here)
    """
    return [
        Path.home() / ".claude" / "skills" / skill_name,
        Path.home() / ".claude" / "skills" / f"lovstudio-{skill_name}",
    ]


def skill_dir(skill_name: str) -> Path:
    """Locate an encrypted skill bundle, returning the first candidate that
    contains a MANIFEST.enc.json. Falls back to the primary path so callers
    can render a sane error message.
    """
    for c in skill_dir_candidates(skill_name):
        if (c / "MANIFEST.enc.json").exists():
            return c
    return skill_dir_candidates(skill_name)[0]


def installed_skills() -> list[str]:
    """List names of locally-installed encrypted skills (any dir under
    ~/.claude/skills containing MANIFEST.enc.json). Strips the `lovstudio-`
    prefix so the user sees the canonical name.
    """
    root = Path.home() / ".claude" / "skills"
    if not root.is_dir():
        return []
    names: set[str] = set()
    for child in root.iterdir():
        if not child.is_dir():
            continue
        if not (child / "MANIFEST.enc.json").exists():
            continue
        name = child.name
        if name.startswith("lovstudio-"):
            name = name[len("lovstudio-"):]
        names.add(name)
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
