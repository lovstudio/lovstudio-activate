"""Shell completion scripts + install helper.

Design choices:
  - Completion scripts are STATIC (hardcoded subcommands) so shell startup
    doesn't pay any Python-import cost.
  - Dynamic parts (skill names, script paths inside a skill) are fetched via
    hidden subcommands `_complete-skills` / `_complete-skill-files` which do
    cheap filesystem scans and print one candidate per line.
  - `lovstudio-skill-helper completion install` writes a single line to the
    user's shell rc file; no multi-step setup.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


BASH_SCRIPT = r"""# lovstudio-skill-helper bash completion
_lovstudio_skill_helper() {
    local cur prev words cword
    _init_completion -n : 2>/dev/null || {
        cur="${COMP_WORDS[COMP_CWORD]}"
        prev="${COMP_WORDS[COMP_CWORD-1]}"
        words=("${COMP_WORDS[@]}")
        cword=$COMP_CWORD
    }

    local subcommands="activate heartbeat status deactivate decrypt exec completion"

    # Top-level subcommand.
    if [[ $cword -eq 1 ]]; then
        COMPREPLY=($(compgen -W "$subcommands" -- "$cur"))
        return
    fi

    local sub="${words[1]}"
    case "$sub" in
        status)
            COMPREPLY=($(compgen -W "--json" -- "$cur"))
            ;;
        decrypt)
            if [[ $cword -eq 2 ]]; then
                local skills
                skills=$(lovstudio-skill-helper _complete-skills 2>/dev/null)
                COMPREPLY=($(compgen -W "$skills" -- "$cur"))
            elif [[ $cword -eq 3 ]]; then
                local files
                files=$(lovstudio-skill-helper _complete-skill-files "${words[2]}" 2>/dev/null)
                COMPREPLY=($(compgen -W "$files" -- "$cur"))
            fi
            ;;
        exec)
            if [[ $cword -eq 2 ]]; then
                local skills
                skills=$(lovstudio-skill-helper _complete-skills 2>/dev/null)
                COMPREPLY=($(compgen -W "$skills" -- "$cur"))
            elif [[ $cword -eq 3 ]]; then
                local files
                files=$(lovstudio-skill-helper _complete-skill-files "${words[2]}" 2>/dev/null)
                COMPREPLY=($(compgen -W "$files" -- "$cur"))
            fi
            ;;
        completion)
            if [[ $cword -eq 2 ]]; then
                COMPREPLY=($(compgen -W "install bash zsh" -- "$cur"))
            elif [[ $cword -eq 3 && "${words[2]}" == "install" ]]; then
                COMPREPLY=($(compgen -W "bash zsh" -- "$cur"))
            fi
            ;;
    esac
}
complete -F _lovstudio_skill_helper lovstudio-skill-helper
"""


ZSH_SCRIPT = r"""#compdef lovstudio-skill-helper
# lovstudio-skill-helper zsh completion

_lovstudio_skill_helper() {
    local context state line
    local -a subcommands
    subcommands=(
        'activate:activate a license key'
        'heartbeat:send heartbeat to refresh license'
        'status:show local license state'
        'deactivate:wipe local license file'
        'decrypt:print a decrypted skill file to stdout (defaults to SKILL.md)'
        'exec:run a decrypted script from a skill'
        'completion:install / print shell completion'
    )

    _arguments -C \
        '1: :->cmd' \
        '*::arg:->args'

    case $state in
        cmd)
            _describe -t commands 'lovstudio-skill-helper command' subcommands
            ;;
        args)
            case $line[1] in
                status)
                    _arguments '--json[raw JSON output]'
                    ;;
                decrypt)
                    if (( CURRENT == 2 )); then
                        local -a skills
                        skills=(${(f)"$(lovstudio-skill-helper _complete-skills 2>/dev/null)"})
                        _describe -t skills 'skill' skills
                    elif (( CURRENT == 3 )); then
                        local -a files
                        files=(${(f)"$(lovstudio-skill-helper _complete-skill-files $line[2] 2>/dev/null)"})
                        _describe -t files 'file' files
                    fi
                    ;;
                exec)
                    if (( CURRENT == 2 )); then
                        local -a skills
                        skills=(${(f)"$(lovstudio-skill-helper _complete-skills 2>/dev/null)"})
                        _describe -t skills 'skill' skills
                    elif (( CURRENT == 3 )); then
                        local -a files
                        files=(${(f)"$(lovstudio-skill-helper _complete-skill-files $line[2] 2>/dev/null)"})
                        _describe -t files 'script' files
                    fi
                    ;;
                completion)
                    _arguments \
                        '1:subcommand:(install bash zsh)' \
                        '2:shell:(bash zsh)'
                    ;;
            esac
            ;;
    esac
}

compdef _lovstudio_skill_helper lovstudio-skill-helper
"""


def detect_shell() -> str:
    """Best-effort detection of current shell from $SHELL env var."""
    shell = os.environ.get("SHELL", "")
    if "zsh" in shell:
        return "zsh"
    if "bash" in shell:
        return "bash"
    return ""


def script_for(shell: str) -> str:
    if shell == "bash":
        return BASH_SCRIPT
    if shell == "zsh":
        return ZSH_SCRIPT
    raise ValueError(f"unsupported shell: {shell!r} (try bash or zsh)")


def rc_file_for(shell: str) -> Path:
    home = Path.home()
    if shell == "bash":
        # macOS bash reads ~/.bash_profile; Linux typically ~/.bashrc. Prefer
        # whichever already exists; fall back to ~/.bashrc.
        for candidate in (home / ".bashrc", home / ".bash_profile"):
            if candidate.exists():
                return candidate
        return home / ".bashrc"
    if shell == "zsh":
        return home / ".zshrc"
    raise ValueError(f"unsupported shell: {shell!r}")


def install(shell: str) -> int:
    """Write the completion script to ~/.lovstudio/completion.<shell> and
    source it from the user's rc file. Idempotent.
    """
    from . import config

    script = script_for(shell)
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    target = config.CONFIG_DIR / f"completion.{shell}"
    target.write_text(script)

    rc = rc_file_for(shell)
    source_line = f"[ -f {target} ] && source {target}  # lovstudio-skill-helper"
    existing = rc.read_text() if rc.exists() else ""
    if "lovstudio-skill-helper" in existing:
        print(f"completion already installed in {rc} (edit manually if needed)")
        return 0

    with rc.open("a") as f:
        if not existing.endswith("\n"):
            f.write("\n")
        f.write(source_line + "\n")
    print(f"✓ installed {shell} completion to {target}")
    print(f"  added source line to {rc}")
    print(f"  run `source {rc}` or open a new terminal to activate.")
    return 0


def cmd_completion(args) -> int:
    sub = args.completion_cmd
    if sub == "install":
        shell = args.shell or detect_shell()
        if not shell:
            print(
                "error: could not detect shell. pass `bash` or `zsh` explicitly:\n"
                "  lovstudio-skill-helper completion install zsh",
                file=sys.stderr,
            )
            return 2
        return install(shell)
    if sub in ("bash", "zsh"):
        sys.stdout.write(script_for(sub))
        return 0
    print("error: expected `install`, `bash`, or `zsh`.", file=sys.stderr)
    return 2


def cmd_complete_skills(args) -> int:
    from . import config

    for name in config.installed_skills():
        print(name)
    return 0


def cmd_complete_skill_files(args) -> int:
    from . import config

    for path in config.list_skill_files(args.skill_name):
        print(path)
    return 0
