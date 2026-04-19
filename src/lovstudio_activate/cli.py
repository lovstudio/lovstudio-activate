"""lovstudio-activate CLI — activate, heartbeat, decrypt, exec.

Trust model:
  - ~/.lovstudio/license.yml holds license_key (chmod 600). Anyone with this
    file can impersonate the user. Don't share.
  - Decryption keys are fetched from the server per invocation, used in
    memory, then die with the process. They are NEVER written to disk.
  - `exec` decrypts a script to a tmpdir, runs it, then deletes the tmpdir.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from . import api, config
from .crypto import SkillManifest, decrypt_file


_BUY_HINT = "  Buy a license key at https://lovstudio.ai (or follow the 手工川 / ShougongChuan WeChat OA)."


def _require_license() -> dict:
    lic = config.load_license()
    if not lic:
        print("error: not activated. run `lovstudio-activate activate <key>` first.", file=sys.stderr)
        print(_BUY_HINT, file=sys.stderr)
        sys.exit(2)
    return lic


def cmd_activate(args) -> int:
    license_key = args.key.strip().lower()
    if len(license_key) != 64 or not all(c in "0123456789abcdef" for c in license_key):
        print("error: license key must be 64 hex chars.", file=sys.stderr)
        return 2

    existing = config.load_license() or {}
    device_id = existing.get("device_id") or config.generate_device_id()

    try:
        resp = api.activate(license_key, device_id)
    except api.ApiError as e:
        print(f"error: activation failed — {e.message}", file=sys.stderr)
        print(_BUY_HINT, file=sys.stderr)
        return 1

    data = {
        "license_key": license_key,
        "device_id": device_id,
        "user_id": resp.get("user_id"),
        "expires_at": resp.get("expires_at"),
        "entitled_skills": resp.get("entitled_skills", []),
        "last_heartbeat_at": None,
    }
    config.save_license(data)
    skills = ", ".join(data["entitled_skills"]) or "(none)"
    print(f"✓ activated. user_id={data['user_id']} entitled={skills}")
    return 0


def cmd_heartbeat(args) -> int:
    lic = _require_license()
    try:
        resp = api.heartbeat(lic["license_key"], lic["device_id"])
    except api.ApiError as e:
        print(f"error: heartbeat failed — {e.message}", file=sys.stderr)
        return 1
    lic["expires_at"] = resp.get("expires_at")
    config.save_license(lic)
    print(f"✓ heartbeat ok. expires_at={resp.get('expires_at')}")
    return 0


def cmd_status(args) -> int:
    lic = config.load_license()
    if not lic:
        print("not activated.")
        return 0
    redacted = {**lic, "license_key": lic["license_key"][:8] + "…"}
    print(json.dumps(redacted, indent=2, ensure_ascii=False))
    return 0


def cmd_deactivate(args) -> int:
    config.wipe_license()
    print("✓ license wiped from local disk.")
    return 0


def _manifest_for(skill_name: str) -> SkillManifest:
    d = config.skill_dir(skill_name)
    if not (d / "MANIFEST.enc.json").exists():
        candidates = config.skill_dir_candidates(skill_name)
        print(f"error: skill '{skill_name}' not installed (no MANIFEST.enc.json found).", file=sys.stderr)
        print(f"  searched, in order:", file=sys.stderr)
        for c in candidates:
            mark = "✓" if (c / "MANIFEST.enc.json").exists() else "✗"
            print(f"    {mark} {c}", file=sys.stderr)
        print(f"  install via either:", file=sys.stderr)
        print(f"    npx skills add lovstudio/skills              # full marketplace", file=sys.stderr)
        print(f"    npx skills add lovstudio/{skill_name}-skill   # just this one", file=sys.stderr)
        sys.exit(2)
    return SkillManifest(d)


def _read_skill_version(manifest: SkillManifest) -> str:
    """Version is baked into MANIFEST.enc.json (format v2+)."""
    if manifest.skill_version:
        return manifest.skill_version
    raise RuntimeError(
        f"manifest at {manifest.skill_dir} has no skill_version field. "
        "Re-pack with pack-skill.py --skill-version <semver>."
    )


def _fetch_key(lic: dict, skill_name: str, version: str) -> bytes:
    try:
        resp = api.skill_keys(lic["license_key"], lic["device_id"], skill_name, version)
    except api.ApiError as e:
        print(f"error: skill_keys failed — {e.message}", file=sys.stderr)
        # 403 = entitlement missing for this skill — point at the storefront.
        if e.status in (401, 403):
            print(_BUY_HINT, file=sys.stderr)
        sys.exit(1)
    return bytes.fromhex(resp["decryption_key"])


def cmd_decrypt(args) -> int:
    """Print the decrypted SKILL.md to stdout. This is what Claude reads."""
    lic = _require_license()
    manifest = _manifest_for(args.skill_name)
    version = _read_skill_version(manifest)
    key = _fetch_key(lic, args.skill_name, version)
    plaintext = decrypt_file(manifest, "SKILL.md", key)
    sys.stdout.buffer.write(plaintext)
    return 0


def cmd_exec(args) -> int:
    """Decrypt a script file to a tmpdir, execute it, then clean up."""
    lic = _require_license()
    manifest = _manifest_for(args.skill_name)
    version = _read_skill_version(manifest)
    key = _fetch_key(lic, args.skill_name, version)

    if args.script_path not in manifest.files:
        print(f"error: '{args.script_path}' not in manifest.", file=sys.stderr)
        return 2
    plaintext = decrypt_file(manifest, args.script_path, key)

    with tempfile.TemporaryDirectory(prefix="lovstudio-") as tmp:
        tmp_path = Path(tmp) / Path(args.script_path).name
        tmp_path.write_bytes(plaintext)
        tmp_path.chmod(0o700)

        # Pick interpreter from extension. KISS — extend when needed.
        suffix = tmp_path.suffix
        if suffix == ".py":
            cmd = [sys.executable, str(tmp_path), *args.script_args]
        elif suffix == ".sh":
            cmd = ["bash", str(tmp_path), *args.script_args]
        else:
            cmd = [str(tmp_path), *args.script_args]

        result = subprocess.run(cmd)
        return result.returncode


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lovstudio-activate")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_activate = sub.add_parser("activate", help="activate a license key")
    p_activate.add_argument("key", help="license key (64 hex chars)")
    p_activate.set_defaults(func=cmd_activate)

    p_hb = sub.add_parser("heartbeat", help="send heartbeat to refresh license")
    p_hb.set_defaults(func=cmd_heartbeat)

    p_status = sub.add_parser("status", help="show local license state")
    p_status.set_defaults(func=cmd_status)

    p_deact = sub.add_parser("deactivate", help="wipe local license file")
    p_deact.set_defaults(func=cmd_deactivate)

    p_dec = sub.add_parser("decrypt", help="print decrypted SKILL.md to stdout")
    p_dec.add_argument("skill_name")
    p_dec.set_defaults(func=cmd_decrypt)

    p_exec = sub.add_parser("exec", help="run a decrypted script from a skill")
    p_exec.add_argument("skill_name")
    p_exec.add_argument("script_path", help="relative path inside the skill, e.g. scripts/foo.py")
    p_exec.add_argument("script_args", nargs=argparse.REMAINDER)
    p_exec.set_defaults(func=cmd_exec)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
