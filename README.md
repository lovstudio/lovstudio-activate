# lovstudio-skill-helper

CLI helper for Lovstudio paid skills — use a Lovstudio account entitlement or a legacy license to transparently decrypt/run protected skills locally. Decryption keys are fetched per-invocation and live only in process memory; they never touch disk.

## Install

The canonical way is via [`uv`](https://docs.astral.sh/uv/) — no install step needed, runs on first use:

```bash
uvx lovstudio-skill-helper login
```

The npm `lovstudio skills add <name>` command signs in and redeems Credits before
installing a paid encrypted bundle. The helper then uses the same account
entitlement when the Agent asks to decrypt the Skill.

Or install it persistently:

```bash
pipx install lovstudio-skill-helper
```

## Usage

```bash
# account login (the npm CLI can start this flow automatically)
lovstudio-skill-helper login

# legacy license path, one-time per device
lovstudio-skill-helper activate <license-key>

# then any paid skill placeholder SKILL.md will call:
lovstudio-skill-helper decrypt <skill-name>       # print plaintext SKILL.md to stdout
lovstudio-skill-helper exec <skill-name> <script> # run an encrypted script once

lovstudio-skill-helper status           # show current activation
lovstudio-skill-helper heartbeat        # refresh last-seen
lovstudio-skill-helper deactivate       # wipe local license
```

## How it works

Paid skills ship as AES-256-GCM ciphertext under the agent skill directory
created by `npx skills add ...`, normally `~/.agents/skills/lovstudio-<name>/`
with agent-specific copies or links such as `~/.claude/skills/...`. Each call
to `decrypt` / `exec`:

1. Uses the signed-in Lovstudio account entitlement created by a Credits redemption.
2. Falls back to the legacy license path for existing users.
3. Returns a per-skill-version AES key only after the server verifies entitlement.
4. Decrypts in memory, streams to stdout or a `tempfile.TemporaryDirectory` that is wiped on exit.

License keys are sold via the 手工川 (ShougongChuan) WeChat official account.

## License

MIT.
