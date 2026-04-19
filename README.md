# lovstudio

Lovstudio CLI — activate and run paid Lovstudio skills. Decryption keys are fetched per-invocation from the license server and live only in process memory; they never touch disk.

## Install

The canonical way is via [`uv`](https://docs.astral.sh/uv/) — no install step needed, runs on first use:

```bash
uvx lovstudio activate <license-key>
```

Or install it persistently:

```bash
pipx install lovstudio
```

## Usage

```bash
# one-time per device
lovstudio activate <license-key>

# then any paid skill placeholder SKILL.md will call:
lovstudio decrypt <skill-name>       # print plaintext SKILL.md to stdout
lovstudio exec <skill-name> <script> # run an encrypted script once

lovstudio status           # show current activation
lovstudio heartbeat        # refresh last-seen
lovstudio deactivate       # wipe local license
```

## How it works

Paid skills ship as AES-256-GCM ciphertext under `~/.claude/skills/<name>/` (or `~/.claude/skills/lovstudio-<name>/`), placed there by `npx skills add ...`. Each call to `decrypt` / `exec`:

1. Signs an HMAC proof with your license key (key itself never leaves the device).
2. Hits the Lovstudio license server, which verifies the proof, checks entitlement, and returns a per-skill-version AES key.
3. Decrypts in memory, streams to stdout or a `tempfile.TemporaryDirectory` that is wiped on exit.

License keys are sold via the 手工川 (ShougongChuan) WeChat official account.

## License

MIT.
