"""lovstudio-skill-helper CLI — activate, heartbeat, decrypt, exec.

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

from . import api, auth, completion, config
from .crypto import SkillManifest, decrypt_file


_BUY_HINT = "  前往 https://lovstudio.ai 购买 license key，或关注 #公众号：手工川 购买。"


def _require_licenses() -> list[dict]:
    licenses = config.load_licenses()
    if not licenses:
        print("error: not activated. run `lovstudio-skill-helper activate <key>` first.", file=sys.stderr)
        print(_BUY_HINT, file=sys.stderr)
        sys.exit(2)
    return licenses


def _all_entitled_skills(licenses: list[dict] | None = None) -> set[str]:
    """Union of entitled_skills across every stacked license."""
    if licenses is None:
        licenses = config.load_licenses()
    out: set[str] = set()
    for lic in licenses:
        out.update(lic.get("entitled_skills") or [])
    return out


def _pick_license_for(skill_name: str, licenses: list[dict] | None = None) -> dict | None:
    """Pick the first stacked license that's entitled to `skill_name`.
    Returns None when no key covers it — caller decides how to recover.
    """
    if licenses is None:
        licenses = config.load_licenses()
    for lic in licenses:
        if skill_name in (lic.get("entitled_skills") or []):
            return lic
    return None


def cmd_activate(args) -> int:
    from . import __version__

    raw = args.key.strip().lower()
    # Accept the human-friendly "lk-" prefix; the wire protocol uses raw hex.
    license_key = raw[3:] if raw.startswith("lk-") else raw
    if len(license_key) != 64 or not all(c in "0123456789abcdef" for c in license_key):
        print("error: license key must be 64 hex chars (with optional 'lk-' prefix).", file=sys.stderr)
        return 2

    device_id = config.device_id()

    # Require a Lovstudio session so the license row is bound to an auth user.
    # If none exists or it's expired past refresh, kick off the device flow
    # inline — users shouldn't have to know `login` is a separate command.
    try:
        bearer = auth.refresh_if_needed()["access_token"]
    except auth.AuthError:
        if args.no_login:
            print("error: not logged in (and --no-login set). run `lovstudio-skill-helper login` first.",
                  file=sys.stderr)
            return 1
        print("→ no Lovstudio session — signing in first")
        try:
            session = auth.login(f"lovstudio-skill-helper {__version__}")
        except auth.AuthError as e:
            print(f"error: login failed — {e}", file=sys.stderr)
            return 1
        print(f"✓ signed in as {session.get('email') or session.get('user_id')}")
        bearer = session["access_token"]

    try:
        resp = api.activate(license_key, device_id, bearer=bearer)
    except api.ApiError as e:
        print(f"error: activation failed — {e.message}", file=sys.stderr)
        print(_BUY_HINT, file=sys.stderr)
        return 1

    entry = {
        "license_key": license_key,
        "user_id": resp.get("user_id"),
        "expires_at": resp.get("expires_at"),
        "entitled_skills": resp.get("entitled_skills", []),
        "last_heartbeat_at": None,
    }
    config.upsert_license(entry)
    # Render the diff against the user's existing stack so they can see what
    # this key actually bought them on top of whatever was already active.
    new_skills = set(entry["entitled_skills"])
    total = _all_entitled_skills()
    added_by_this_key = sorted(new_skills)
    print(f"✓ activated key lk-{license_key[:6]}… — this key grants: "
          f"{', '.join(added_by_this_key) or '(none)'}")
    print(f"  total entitled across all keys: {len(total)} skill(s): "
          f"{', '.join(sorted(total)) or '(none)'}")
    return 0


def cmd_heartbeat(args) -> int:
    licenses = _require_licenses()
    did = config.device_id()
    updated: list[dict] = []
    any_error = False
    for lic in licenses:
        key_short = lic["license_key"][:6]
        try:
            resp = api.heartbeat(lic["license_key"], did)
        except api.ApiError as e:
            print(f"  ! lk-{key_short}… heartbeat failed — {e.message}", file=sys.stderr)
            # Keep the stale entry around — don't silently drop it; user may
            # fix it (e.g. server offline, transient auth). A hard-expired
            # key will surface as 401 on the next skill_keys call.
            updated.append(lic)
            any_error = True
            continue
        new_skills = resp.get("entitled_skills")
        if isinstance(new_skills, list):
            old_skills = set(lic.get("entitled_skills") or [])
            new_set = set(new_skills)
            added = sorted(new_set - old_skills)
            removed = sorted(old_skills - new_set)
            lic["entitled_skills"] = sorted(new_set)
            if added:
                print(f"  + lk-{key_short}… gained: {', '.join(added)}")
            if removed:
                print(f"  - lk-{key_short}… lost:   {', '.join(removed)}")
        lic["expires_at"] = resp.get("expires_at")
        lic["last_heartbeat_at"] = _utcnow_iso()
        updated.append(lic)
        print(f"  ✓ lk-{key_short}… expires_at={resp.get('expires_at')}")
    config.save_licenses(updated)
    total = _all_entitled_skills(updated)
    print(f"✓ heartbeat done for {len(updated)} key(s). "
          f"total entitled: {len(total)} skill(s).")
    return 1 if any_error else 0


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def cmd_status(args) -> int:
    licenses = config.load_licenses()
    did = config.device_id()

    if args.json:
        redacted = [
            {**lic, "license_key": lic["license_key"][:8] + "…"}
            for lic in licenses
        ]
        print(json.dumps({"activated": bool(licenses), "device_id": did, "licenses": redacted},
                         indent=2, ensure_ascii=False))
        return 0

    # License header. Free skills work without activation, so we still render
    # the catalog; we just mark paid skills as "not yet entitled".
    entitled_names: set[str] = _all_entitled_skills(licenses)
    if licenses:
        print(f"device_id         {did}")
        print(f"active keys       {len(licenses)}")
        for lic in licenses:
            key_short = lic["license_key"][:8] + "…"
            skills = lic.get("entitled_skills") or []
            print(f"  lk-{key_short}")
            print(f"    user_id         {lic.get('user_id') or '—'}")
            print(f"    expires_at      {lic.get('expires_at') or '— (no expiry)'}")
            print(f"    last_heartbeat  {lic.get('last_heartbeat_at') or '—'}")
            print(f"    grants          {', '.join(skills) if skills else '(none)'}")
    else:
        print(f"device_id         {did}")
        print("license           — (not activated; free skills still available)")
        print("                    activate a paid license with:")
        print("                    lovstudio-skill-helper activate lk-<your-key>")

    # Fetch catalog; fall back to flat list if offline.
    try:
        catalog = api.list_catalog()
    except api.ApiError as e:
        print(f"entitled          {len(entitled_names)} skills")
        print()
        print(f"(catalog fetch failed — {e.message}; showing flat entitled list)", file=sys.stderr)
        for name in sorted(entitled_names):
            print(f"  [x] {name} (entitled)")
        return 0

    # A skill counts as "entitled" if it's free OR explicitly in the license.
    def is_entitled(row: dict) -> bool:
        return (not row.get("paid")) or row["name"] in entitled_names

    total = len(catalog)
    granted = sum(1 for row in catalog if is_entitled(row))
    print(f"entitled          {granted}/{total} skills")
    print()

    # Group by category and build rows for the table.
    by_cat: dict[str, list[dict]] = {}
    for row in catalog:
        cat = row.get("category") or "(uncategorized)"
        by_cat.setdefault(cat, []).append(row)

    known_names = {row["name"] for row in catalog}
    orphans = sorted(entitled_names - known_names)
    if orphans:
        by_cat.setdefault("(other)", []).extend(
            {"name": n, "paid": True} for n in orphans
        )

    # Colors: auto-disable when stdout isn't a tty or NO_COLOR is set.
    use_color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    def tint(text: str, code: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if use_color else text

    def status_label(row: dict) -> tuple[str, str]:
        name = row["name"]
        if not row.get("paid"):
            return "free", "2"            # dim
        if name in entitled_names:
            return "entitled", "32"       # green
        return "not yet entitled", "31"   # red

    # Flatten to rows for rendering. Category appears only on its first row.
    table_rows: list[tuple[str, str, str, str]] = []  # (cat, skill, status, color)
    for cat in sorted(by_cat):
        first = True
        for r in sorted(by_cat[cat], key=lambda r: r["name"]):
            status, color = status_label(r)
            table_rows.append((cat if first else "", r["name"], status, color))
            first = False

    headers = ("CATEGORY", "SKILL", "STATUS")
    widths = [
        max(len(headers[0]), max((len(r[0]) for r in table_rows), default=0)),
        max(len(headers[1]), max((len(r[1]) for r in table_rows), default=0)),
        max(len(headers[2]), max((len(r[2]) for r in table_rows), default=0)),
    ]
    gap = "  "
    header_line = gap.join(h.ljust(w) for h, w in zip(headers, widths))
    rule = "─" * (sum(widths) + len(gap) * (len(widths) - 1))
    print(tint(header_line, "1"))  # bold
    print(rule)

    prev_cat = None
    for cat, skill, status, color in table_rows:
        # Blank line between categories to separate visual blocks.
        if prev_cat is not None and cat and cat != prev_cat:
            print()
        cat_cell = cat.ljust(widths[0])
        skill_cell = skill.ljust(widths[1])
        status_cell = tint(status.ljust(widths[2]), color)
        print(f"{cat_cell}{gap}{skill_cell}{gap}{status_cell}")
        if cat:
            prev_cat = cat

    return 0


def cmd_deactivate(args) -> int:
    if args.all:
        config.wipe_license()
        print("✓ all license keys wiped from local disk.")
        return 0
    if not args.key:
        print("error: specify which key to remove, or pass --all.\n"
              "  lovstudio-skill-helper deactivate lk-<key>\n"
              "  lovstudio-skill-helper deactivate --all", file=sys.stderr)
        return 2
    raw = args.key.strip().lower()
    key = raw[3:] if raw.startswith("lk-") else raw
    if config.remove_license(key):
        print(f"✓ removed lk-{key[:6]}… from local stack.")
        remaining = config.load_licenses()
        print(f"  {len(remaining)} key(s) remain.")
        return 0
    print(f"error: no local license matches lk-{key[:6]}…", file=sys.stderr)
    return 1


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
        print(f"    npx skills add lovstudio/skills                              # full marketplace", file=sys.stderr)
        print(f"    npx skills add lovstudio/skills --skill lovstudio:{skill_name}   # just this one", file=sys.stderr)
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


def _fetch_key(skill_name: str, version: str) -> bytes:
    """Fetch the AES key for `skill_name`, trying every stacked license.

    Strategy:
      1. Pick the license that advertises entitlement locally (fast path).
      2. If none advertise it, still try each license — the server is authoritative
         and the local cache may be stale after an admin top-up without heartbeat.
      3. If all keys come back 401/403, prompt the user to add another key.
    """
    did = config.device_id()
    licenses = _require_licenses()

    tried: set[str] = set()
    # Fast path: any key that already advertises this skill locally.
    preferred = _pick_license_for(skill_name, licenses)
    candidates: list[dict] = []
    if preferred is not None:
        candidates.append(preferred)
    # Then every other key — catches the "admin just topped up, no heartbeat yet" case.
    for lic in licenses:
        if lic["license_key"] not in {c["license_key"] for c in candidates}:
            candidates.append(lic)

    last_err: api.ApiError | None = None
    for lic in candidates:
        if lic["license_key"] in tried:
            continue
        tried.add(lic["license_key"])
        try:
            resp = api.skill_keys(lic["license_key"], did, skill_name, version)
        except api.ApiError as e:
            last_err = e
            # 401/403 → this key doesn't cover this skill. Try the next one.
            # Anything else → hard-fail now (network, server error, bad nonce).
            if e.status in (401, 403):
                continue
            print(f"error: skill_keys failed — {e.message}", file=sys.stderr)
            sys.exit(1)
        return bytes.fromhex(resp["decryption_key"])

    # Every stacked key returned 401/403. Offer a recovery path.
    if last_err and last_err.status in (401, 403) and sys.stdin.isatty():
        new_key = _prompt_not_entitled(skill_name)
        if new_key is None:
            sys.exit(1)
        if _activate_and_stack(new_key) != 0:
            sys.exit(1)
        # Retry once with the freshly-stacked license.
        return _fetch_key(skill_name, version)

    print(f"error: no activated license covers '{skill_name}'.", file=sys.stderr)
    print(_BUY_HINT, file=sys.stderr)
    sys.exit(1)


def _prompt_not_entitled(skill_name: str) -> str | None:
    """Ask the user how to resolve a missing entitlement. Returns a new license key or None."""
    import webbrowser

    buy_url = f"https://lovstudio.ai/skills/{skill_name}"
    print(f"", file=sys.stderr)
    print(f"You don't have access to '{skill_name}' yet.", file=sys.stderr)
    print(f"  [1] enter a different license key", file=sys.stderr)
    print(f"  [2] open purchase page ({buy_url})", file=sys.stderr)
    print(f"  [3] cancel", file=sys.stderr)
    try:
        choice = input("choose [1/2/3]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("", file=sys.stderr)
        return None
    if choice == "1":
        try:
            return input("license key (lk-...): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("", file=sys.stderr)
            return None
    if choice == "2":
        try:
            webbrowser.open(buy_url)
        except Exception:
            pass
        print(f"→ opened {buy_url} — complete purchase, then re-run your command.", file=sys.stderr)
    return None


def _activate_and_stack(license_key: str) -> int:
    """Activate `license_key` and stack it alongside existing keys. Returns exit code."""
    ns = argparse.Namespace(key=license_key, no_login=False)
    return cmd_activate(ns)


def cmd_decrypt(args) -> int:
    """Print the decrypted SKILL.md to stdout. This is what Claude reads."""
    _require_licenses()
    manifest = _manifest_for(args.skill_name)
    version = _read_skill_version(manifest)
    key = _fetch_key(args.skill_name, version)
    plaintext = decrypt_file(manifest, "SKILL.md", key)
    sys.stdout.buffer.write(plaintext)
    return 0


def cmd_exec(args) -> int:
    """Decrypt a script file to a tmpdir, execute it, then clean up."""
    _require_licenses()
    manifest = _manifest_for(args.skill_name)
    version = _read_skill_version(manifest)
    key = _fetch_key(args.skill_name, version)

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


def cmd_call(args) -> int:
    """Invoke a cloud-split skill's server-side handler.

    Emits the handler's `output` payload as JSON on stdout. Errors go to stderr.
    """
    licenses = _require_licenses()
    try:
        input_data = json.loads(args.input) if args.input else {}
    except json.JSONDecodeError as e:
        print(f"error: --input is not valid JSON — {e}", file=sys.stderr)
        return 2
    if not isinstance(input_data, dict):
        print("error: --input must be a JSON object", file=sys.stderr)
        return 2

    # Try each stacked license. Same fallthrough rule as `_fetch_key`:
    # 401/403 means "this key doesn't cover this skill"; try the next key.
    did = config.device_id()
    preferred = _pick_license_for(args.skill_name, licenses)
    candidates: list[dict] = [preferred] if preferred else []
    for lic in licenses:
        if lic["license_key"] not in {c["license_key"] for c in candidates}:
            candidates.append(lic)

    last_err: api.ApiError | None = None
    for lic in candidates:
        try:
            resp = api.skill_call(
                lic["license_key"], did,
                args.skill_name, args.skill_version, args.op, input_data,
            )
        except api.ApiError as e:
            last_err = e
            if e.status in (401, 403):
                continue
            print(f"error: {e.message}", file=sys.stderr)
            return 1
        output = resp.get("output", resp)
        print(json.dumps(output, ensure_ascii=False))
        return 0

    if last_err and last_err.status in (401, 403):
        print(f"error: no activated license covers '{args.skill_name}'.", file=sys.stderr)
        print(_BUY_HINT, file=sys.stderr)
    elif last_err:
        print(f"error: {last_err.message}", file=sys.stderr)
    return 1


def cmd_admin_issue_license(args) -> int:
    """Admin-only: mint a license key via the /issue_license edge function.

    Hidden subcommand — wrapped by `npx lovstudio license issue`. Requires
    the caller's auth.user.id to be in the ADMIN_USER_IDS server env.
    """
    bearer = _require_bearer()

    if not args.skills and not args.scope:
        print(
            "error: must specify which skills to grant.\n"
            "  --skills <name1,name2,...>   grant specific skills\n"
            "  --scope global               grant ALL skills in the catalog\n"
            "  --scope category --scope-value \"Image & Design\"   grant all skills in a category\n"
            "\n"
            "examples:\n"
            "  npx lovstudio license issue --scope global --notes \"测试 all\"\n"
            "  npx lovstudio license issue --skills paid-add,event-poster --notes \"朋友测试\"",
            file=sys.stderr,
        )
        return 2

    body: dict = {}
    if args.skills:
        body["skills"] = [s.strip() for s in args.skills.split(",") if s.strip()]
    if args.scope:
        body["scope"] = args.scope
    if args.scope_value:
        body["scope_value"] = args.scope_value
    if args.user:
        body["user_id"] = args.user
    if args.max_devices is not None:
        body["max_devices"] = args.max_devices
    if args.expires_days is not None:
        # edge function expects expires_at (ISO); convert days → ISO
        from datetime import datetime, timedelta, timezone
        if args.expires_days == 0:
            body["expires_at"] = None
        else:
            body["expires_at"] = (
                datetime.now(timezone.utc) + timedelta(days=args.expires_days)
            ).isoformat()
    if args.source:
        body["source"] = args.source
    if args.notes:
        body["notes"] = args.notes
    if args.nickname:
        body["nickname"] = args.nickname
    if args.force_new:
        body["force_new"] = True

    try:
        resp = api.call("issue_license", body, bearer=bearer)
    except api.ApiError as e:
        print(f"error: issue_license failed — {e.message}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(resp, ensure_ascii=False, indent=2))
        return 0

    granted = resp.get("granted_skills") or []
    expires_at = resp.get("expires_at")
    nickname = resp.get("nickname")
    is_global = bool(args.scope == "global")

    if resp.get("reused"):
        print(f"✓ topped up license #{resp['license_id']}")
        if nickname:
            print(f"  nickname:       {nickname}")
        print(f"  granted_skills: {', '.join(granted)}")
        newly = resp.get("newly_granted") or []
        if newly:
            print(f"  newly_granted:  {', '.join(newly)}")
        print(f"  expires_at:     {expires_at or '— (no expiry)'}")
        # Top-up path: the plaintext key was issued earlier, we can't
        # reconstruct it. Skip the forwardable message.
        return 0

    print(f"✓ minted license #{resp['license_id']}")
    print(f"  license_key:    {resp['license_key']}")
    if nickname:
        print(f"  nickname:       {nickname}")
    print(f"  proof_user_id:  {resp['proof_user_id']}")
    print(f"  granted_skills: {', '.join(granted)}")
    print(f"  expires_at:     {expires_at or '— (no expiry)'}")
    print()
    print("  ⚠ the plaintext key is shown ONCE. Copy it now.")
    print()
    _print_forwardable_message(
        license_key=resp["license_key"],
        granted_skills=granted,
        expires_at=expires_at,
        is_global=is_global,
        nickname=nickname,
    )
    return 0


def _print_forwardable_message(
    *,
    license_key: str,
    granted_skills: list,
    expires_at: str | None,
    is_global: bool,
    nickname: str | None = None,
) -> None:
    """Print a ready-to-paste Chinese message for the end user.

    Copy everything between the --- markers and send via WeChat / email.
    Activate is step 1 (it's a local state write, no skill required);
    installing comes after so every skill the user adds later is already
    entitled. When a nickname is supplied, greet the recipient by name —
    turns a generic hand-off into a personal one.
    """
    if is_global:
        scope_line = "**授权范围**：Lovstudio 全套 skill"
        install_step = "lovstudio skills add lovstudio/skills"
    elif len(granted_skills) == 1:
        scope_line = f"**授权范围**：{granted_skills[0]}"
        install_step = f"lovstudio skills add {granted_skills[0]}"
    else:
        scope_line = "**授权范围**：\n  - " + "\n  - ".join(granted_skills)
        install_step = "\n".join(
            f"lovstudio skills add {s}" for s in granted_skills
        )

    expiry_line = f"**有效期至**：{expires_at[:10]}" if expires_at else ""

    greeting = (
        f"🎉 {nickname}，您的 Lovstudio license 已开通～"
        if nickname
        else "🎉 您的 Lovstudio license 已开通～"
    )
    lines = [
        "── 复制以下内容发给用户 ──",
        "",
        greeting,
        "",
        scope_line,
    ]
    if expiry_line:
        lines.append(expiry_line)
    lines.extend([
        "",
        "**激活步骤**（建议在 Claude Code / 龙虾 等 agent runtime 里执行）：",
        "",
        "```bash",
        "# 1. 安装 lovstudio CLI（一次性）",
        "npm i -g lovstudio",
        "",
        "# 2. 激活 license（本地绑定，只需一次）",
        f"lovstudio license activate {license_key}",
        "",
        "# 3. 安装 skill",
        install_step,
        "```",
        "",
        "完成后在 agent 里直接调用对应 skill 即可 ✨",
        "",
        "有问题可以群里反馈或直接找我，更多内容见 https://lovstudio.ai。",
        "感谢您的支持，也欢迎推荐给身边的朋友（推荐返佣 30%）！",
        "",
        "也可在网页上把 license 绑定到账号：https://lovstudio.ai/license/redeem",
        "── 复制结束 ──",
    ])
    print("\n".join(lines))


def _require_bearer() -> str:
    """Return a valid auth bearer, prompting the device-flow login if needed."""
    from . import __version__

    try:
        return auth.refresh_if_needed()["access_token"]
    except auth.AuthError:
        print("→ no Lovstudio session — signing in first")
        try:
            session = auth.login(f"lovstudio-skill-helper {__version__}")
        except auth.AuthError as e:
            print(f"error: login failed — {e}", file=sys.stderr)
            sys.exit(1)
        print(f"✓ signed in as {session.get('email') or session.get('user_id')}")
        return session["access_token"]


def cmd_list_licenses(args) -> int:
    """List licenses. No flags → caller's own licenses. `--all`/`--user` → admin."""
    bearer = _require_bearer()
    body: dict = {}
    if args.all:
        body["all"] = True
    if args.user:
        body["user_id"] = args.user
    try:
        resp = api.call("list_licenses", body, bearer=bearer)
    except api.ApiError as e:
        print(f"error: list_licenses failed — {e.message}", file=sys.stderr)
        return 1

    rows = resp.get("licenses") or []
    if args.json:
        print(json.dumps(resp, ensure_ascii=False, indent=2))
        return 0

    if not rows:
        print("(no licenses)")
        return 0

    # Render as a compact table. Hide key_hash; show a key-short the admin can
    # cross-reference with what the recipient has.
    headers = ("ID", "KEY", "NICKNAME", "STATUS", "EXPIRES", "SKILLS", "NOTES")
    data_rows: list[tuple[str, ...]] = []
    for r in rows:
        data_rows.append((
            str(r.get("id", "—")),
            f"lk-{(r.get('key_hash') or '')[:8]}…",  # server returns key_hash, not plaintext
            (r.get("nickname") or "—"),
            (r.get("status") or "—"),
            (r.get("expires_at") or "— (no expiry)")[:19],
            str(r.get("skills_count", len(r.get("skills") or []))),
            (r.get("notes") or "—")[:30],
        ))
    widths = [max(len(h), max((len(row[i]) for row in data_rows), default=0))
              for i, h in enumerate(headers)]
    gap = "  "
    print(gap.join(h.ljust(w) for h, w in zip(headers, widths)))
    print("─" * (sum(widths) + len(gap) * (len(widths) - 1)))
    for row in data_rows:
        print(gap.join(cell.ljust(w) for cell, w in zip(row, widths)))
    return 0


def cmd_admin_revoke_license(args) -> int:
    """Admin-only: revoke a license server-side (licenses.status='revoked')."""
    bearer = _require_bearer()
    raw = args.key.strip().lower()
    key = raw[3:] if raw.startswith("lk-") else raw
    if len(key) != 64 or not all(c in "0123456789abcdef" for c in key):
        print("error: license key must be 64 hex chars (with optional 'lk-' prefix).", file=sys.stderr)
        return 2
    # Pass the plaintext key; the server hashes it to find the row.
    try:
        resp = api.call("revoke_license", {"license_key": key}, bearer=bearer)
    except api.ApiError as e:
        print(f"error: revoke_license failed — {e.message}", file=sys.stderr)
        return 1
    print(f"✓ revoked license #{resp.get('license_id', '?')} "
          f"(nickname: {resp.get('nickname') or '—'})")
    return 0


def cmd_login(args) -> int:
    from . import __version__

    client = f"lovstudio-skill-helper {__version__}"
    try:
        session = auth.login(client, open_browser=not args.no_browser)
    except auth.AuthError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"✓ logged in as {session.get('email') or session.get('user_id')}")
    return 0


def cmd_logout(args) -> int:
    auth.wipe_auth()
    print("✓ logged out")
    return 0


def cmd_whoami(args) -> int:
    session = auth.whoami()
    if not session:
        print("not logged in", file=sys.stderr)
        return 1
    print(session.get("email") or session.get("user_id") or "(unknown)")
    return 0


def main(argv: list[str] | None = None) -> int:
    from . import __version__

    p = argparse.ArgumentParser(prog="lovstudio-skill-helper")
    p.add_argument(
        "-V", "--version",
        action="version",
        version=f"lovstudio-skill-helper {__version__}",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_login = sub.add_parser("login", help="sign in to Lovstudio (device flow)")
    p_login.add_argument(
        "--no-browser",
        action="store_true",
        help="print the URL instead of auto-opening it",
    )
    p_login.set_defaults(func=cmd_login)

    p_logout = sub.add_parser("logout", help="forget the local Lovstudio session")
    p_logout.set_defaults(func=cmd_logout)

    p_whoami = sub.add_parser("whoami", help="show the email of the logged-in account")
    p_whoami.set_defaults(func=cmd_whoami)

    p_activate = sub.add_parser("activate", help="activate a license key (triggers login if needed)")
    p_activate.add_argument("key", help="license key (e.g. lk-<64 hex chars>)")
    p_activate.add_argument("--no-login", action="store_true",
                            help="fail instead of launching the device flow when no session exists")
    p_activate.set_defaults(func=cmd_activate)

    p_hb = sub.add_parser("heartbeat", help="send heartbeat to refresh license")
    p_hb.set_defaults(func=cmd_heartbeat)

    p_status = sub.add_parser("status", help="show local license state (by category, with entitlement marks)")
    p_status.add_argument("--json", action="store_true", help="raw JSON output (old behavior)")
    p_status.set_defaults(func=cmd_status)

    p_deact = sub.add_parser("deactivate",
                             help="remove a stacked license key, or all of them")
    p_deact.add_argument("key", nargs="?", default=None,
                         help="license key to remove (e.g. lk-<64 hex>). omit with --all")
    p_deact.add_argument("--all", action="store_true",
                         help="wipe every stacked license from this machine")
    p_deact.set_defaults(func=cmd_deactivate)

    p_dec = sub.add_parser("decrypt", help="print decrypted SKILL.md to stdout")
    p_dec.add_argument("skill_name")
    p_dec.set_defaults(func=cmd_decrypt)

    p_exec = sub.add_parser("exec", help="run a decrypted script from a skill")
    p_exec.add_argument("skill_name")
    p_exec.add_argument("script_path", help="relative path inside the skill, e.g. scripts/foo.py")
    p_exec.add_argument("script_args", nargs=argparse.REMAINDER)
    p_exec.set_defaults(func=cmd_exec)

    p_call = sub.add_parser("call", help="invoke a cloud-split skill's server-side handler")
    p_call.add_argument("skill_name")
    p_call.add_argument("--op", required=True, help="handler operation, e.g. `evaluate`")
    p_call.add_argument("--input", default="{}", help="JSON object forwarded to the handler as `input`")
    p_call.add_argument("--skill-version", default="0.1.0", help="skill version (must match server-side skills row)")
    p_call.set_defaults(func=cmd_call)

    p_comp = sub.add_parser(
        "completion",
        help="install or print shell completion (bash, zsh)",
    )
    p_comp.add_argument(
        "completion_cmd",
        choices=["install", "bash", "zsh"],
        help="`install` to write to rc file; `bash`/`zsh` to print the script to stdout",
    )
    p_comp.add_argument(
        "shell",
        nargs="?",
        choices=["bash", "zsh"],
        help="required with `install` if $SHELL can't be auto-detected",
    )
    p_comp.set_defaults(func=completion.cmd_completion)

    # Hidden admin command — used by `npx lovstudio license issue`.
    p_ail = sub.add_parser("admin-issue-license", help=argparse.SUPPRESS)
    p_ail.add_argument("--skills", help="comma-separated skill names (preferred)")
    p_ail.add_argument("--scope", choices=["skill", "category", "global"],
                       help="legacy scope (used only if --skills omitted)")
    p_ail.add_argument("--scope-value", help="legacy scope_value")
    p_ail.add_argument("--user", help="auth.users uuid to bind (omit = anonymous)")
    p_ail.add_argument("--max-devices", type=int, default=None)
    p_ail.add_argument("--expires-days", type=int, default=None,
                       help="days until expiry (0 = no expiry)")
    p_ail.add_argument("--source", default=None)
    p_ail.add_argument("--notes", default=None)
    p_ail.add_argument("--nickname", default=None,
                       help="recipient label, e.g. '李柯江'. Used to greet them "
                            "in the forwardable message and bind later.")
    p_ail.add_argument("--force-new", action="store_true",
                       help="mint a new key even if the user already has one")
    p_ail.add_argument("--json", action="store_true", help="raw JSON output")
    p_ail.set_defaults(func=cmd_admin_issue_license)

    # `list-licenses` — auth-gated, but not admin-only. No args = own licenses.
    p_ll = sub.add_parser("list-licenses", help=argparse.SUPPRESS)
    p_ll.add_argument("--all", action="store_true",
                      help="(admin) show every license in the system")
    p_ll.add_argument("--user", default=None,
                      help="(admin) filter by auth.users uuid")
    p_ll.add_argument("--json", action="store_true", help="raw JSON output")
    p_ll.set_defaults(func=cmd_list_licenses)

    # `admin-revoke-license` — admin only. Wrapped by `npx lovstudio license revoke`.
    p_arl = sub.add_parser("admin-revoke-license", help=argparse.SUPPRESS)
    p_arl.add_argument("key", help="license key (lk-<64 hex> or plain 64 hex)")
    p_arl.set_defaults(func=cmd_admin_revoke_license)

    # Hidden helpers used by the completion scripts themselves.
    p_cs = sub.add_parser("_complete-skills", help=argparse.SUPPRESS)
    p_cs.set_defaults(func=completion.cmd_complete_skills)

    p_csf = sub.add_parser("_complete-skill-files", help=argparse.SUPPRESS)
    p_csf.add_argument("skill_name")
    p_csf.set_defaults(func=completion.cmd_complete_skill_files)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
