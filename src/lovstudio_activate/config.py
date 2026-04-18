"""On-disk layout for activated state.

~/.lovstudio/
├── license.yml                    # license_key, device_id, activated_at, expires_at,
│                                  # last_heartbeat_at, entitled_skills
└── brand_skills/<skill-name>/     # ciphertext installed from lovstudio/skills repo
    ├── MANIFEST.enc.json
    ├── SKILL.md.enc
    └── scripts/*.enc

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
BRAND_SKILLS_DIR = CONFIG_DIR / "brand_skills"

# Default Edge Function endpoint. Overridable via env for dev/test.
DEFAULT_API_BASE = "https://cssuvwfoyevryibnipqf.supabase.co/functions/v1"
# Default anon key — Edge Functions require it for JWT gate, even though
# we enforce real auth via HMAC inside the function body.
DEFAULT_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImNzc3V2d2ZveWV2cnlpYm5pcHFmIiwicm9sZSI6"
    "ImFub24iLCJpYXQiOjE3NzY1MzA4MzgsImV4cCI6MjA5MjEwNjgzOH0."
    "RfzHDCYdrfrAAcdurAAiD_SaL_tkf28bdTWnD4T9zqg"
)


def api_base() -> str:
    return os.environ.get("LOVSTUDIO_API_BASE", DEFAULT_API_BASE)


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


def skill_dir(skill_name: str) -> Path:
    """Locate an encrypted skill bundle.

    Search order:
      1. ~/.lovstudio/brand_skills/<name>/       ← explicit install
      2. ~/.claude/skills/<name>/                ← installed via `npx skills add`
      3. ~/.claude/skills/lovstudio-<name>/      ← legacy prefixed name

    Returns the first directory that contains a MANIFEST.enc.json.
    Falls back to the primary path so the error message points somewhere sane.
    """
    candidates = [
        BRAND_SKILLS_DIR / skill_name,
        Path.home() / ".claude" / "skills" / skill_name,
        Path.home() / ".claude" / "skills" / f"lovstudio-{skill_name}",
    ]
    for c in candidates:
        if (c / "MANIFEST.enc.json").exists():
            return c
    return candidates[0]
