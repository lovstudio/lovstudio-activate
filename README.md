# lovstudio-skill-helper

CLI helper for Lovstudio paid skills — activate your license and transparently decrypt/run protected skills locally. Decryption keys are fetched per-invocation from the license server and live only in process memory; they never touch disk.

## Install

The canonical way is via [`uv`](https://docs.astral.sh/uv/) — no install step needed, runs on first use:

```bash
uvx lovstudio-skill-helper activate <license-key>
```

The npm `lovstudio license activate` command calls this helper through `uvx`.
If you do not want to run the local helper for activation, bind the license on
the web instead: https://lovstudio.ai/license/redeem

Or install it persistently:

```bash
pipx install lovstudio-skill-helper
```

## Usage

```bash
# one-time per device
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

1. Signs an HMAC proof with your license key (key itself never leaves the device).
2. Hits the Lovstudio license server, which verifies the proof, checks entitlement, and returns a per-skill-version AES key.
3. Decrypts in memory, streams to stdout or a `tempfile.TemporaryDirectory` that is wiped on exit.

License keys are sold via the 手工川 (ShougongChuan) WeChat official account.

## License

MIT.
