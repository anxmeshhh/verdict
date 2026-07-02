"""
Git pre-push hook - the literal pre-deployment gate.

`verdict install-hook` drops a pre-push hook into .git/hooks that verifies
exactly the commits about to leave this machine (remote SHA -> local SHA)
and blocks the push unless the verdict is LOW. This is the sharpest
meaning of "pre-deployment": nothing unverified gets out.
"""
from pathlib import Path

HOOK_MARKER = "# verdict-pre-push-hook"

HOOK_SCRIPT = f"""#!/bin/sh
{HOOK_MARKER}
# Installed by `verdict install-hook`. Remove with `verdict uninstall-hook`.
# Verifies the exact range being pushed; blocks unless the verdict is LOW.

zero=0000000000000000000000000000000000000000

while read local_ref local_sha remote_ref remote_sha; do
  [ "$local_sha" = "$zero" ] && continue   # branch deletion - nothing to verify

  if [ "$remote_sha" = "$zero" ]; then
    # New branch: compare against the merge base with the default branch, if any
    base=$(git merge-base "$local_sha" origin/HEAD 2>/dev/null)
    [ -z "$base" ] && continue
  else
    base=$remote_sha
  fi

  [ "$base" = "$local_sha" ] && continue   # nothing new to push

  echo "verdict: verifying $base..$local_sha before push"
  verdict run --base "$base" --ref "$local_sha" || {{
    echo ""
    echo "verdict: verification did not come back LOW - push blocked."
    echo "verdict: inspect with 'verdict logs <run-id>', fix, or push with --no-verify to bypass."
    exit 1
  }}
done

exit 0
"""


class HookError(Exception):
    pass


def _hook_path(repo: Path) -> Path:
    git_dir = repo / ".git"
    if not git_dir.is_dir():
        raise HookError(f"{repo} is not a git repository")
    return git_dir / "hooks" / "pre-push"


def install(repo: Path) -> Path:
    path = _hook_path(repo)
    if path.exists():
        existing = path.read_text(encoding="utf-8", errors="replace")
        if HOOK_MARKER in existing:
            raise HookError("verdict pre-push hook is already installed")
        raise HookError(
            f"a pre-push hook already exists at {path} - not overwriting it. "
            "Merge it manually or remove it first."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HOOK_SCRIPT, encoding="utf-8", newline="\n")
    path.chmod(0o755)
    return path


def uninstall(repo: Path) -> Path:
    path = _hook_path(repo)
    if not path.exists():
        raise HookError("no pre-push hook is installed")
    if HOOK_MARKER not in path.read_text(encoding="utf-8", errors="replace"):
        raise HookError(f"the pre-push hook at {path} was not installed by verdict - not touching it")
    path.unlink()
    return path


def is_installed(repo: Path) -> bool:
    try:
        path = _hook_path(repo)
    except HookError:
        return False
    return path.exists() and HOOK_MARKER in path.read_text(encoding="utf-8", errors="replace")
