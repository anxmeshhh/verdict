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
#
# Git passes the remote name as $1 and its URL as $2 to a pre-push hook -
# never assume it's literally "origin" (upstream, a renamed origin, multiple
# remotes are all common). $1 is still valid inside the while loop below:
# a bare `while` doesn't fork a subshell the way `cmd | while` would.
remote_name=$1

zero=0000000000000000000000000000000000000000

while read local_ref local_sha remote_ref remote_sha; do
  [ "$local_sha" = "$zero" ] && continue   # branch deletion - nothing to verify

  if [ "$remote_sha" = "$zero" ]; then
    # New branch: no remote_sha to diff against - fall back to a merge-base
    # with the remote's default branch. Try the remote's tracked HEAD first,
    # then common default-branch names, since not every remote has HEAD
    # tracked locally (e.g. a bare remote added without `remote set-head`).
    base=$(git merge-base "$local_sha" "refs/remotes/$remote_name/HEAD" 2>/dev/null)
    if [ -z "$base" ]; then
      for candidate in main master; do
        if git rev-parse --verify -q "refs/remotes/$remote_name/$candidate" >/dev/null 2>&1; then
          base=$(git merge-base "$local_sha" "refs/remotes/$remote_name/$candidate" 2>/dev/null)
          [ -n "$base" ] && break
        fi
      done
    fi
    if [ -z "$base" ]; then
      # Never a silent no-op: an unverified push must never look identical
      # to a verified one. This is the first push of a genuinely new/orphan
      # branch with nothing to compare against - nothing CAN be checked yet.
      echo "verdict: could not determine a base commit for this new branch - skipping verification (nothing was checked)"
      continue
    fi
  else
    base=$remote_sha
  fi

  [ "$base" = "$local_sha" ] && continue   # nothing new to push

  echo "verdict: verifying $base..$local_sha before push"
  verdict run --base "$base" --ref "$local_sha"
  status=$?
  if [ "$status" -ne 0 ]; then
    echo ""
    if [ "$status" -eq 2 ]; then
      # Exit 2: the CHECKER couldn't do its job (bad ref, provider down) -
      # distinct from the code actually looking risky, so say so plainly
      # rather than reusing the same "did not come back LOW" wording.
      echo "verdict: could not verify this push (a checker problem, not necessarily your code) - push blocked."
      echo "verdict: inspect with 'verdict logs <run-id>', or push with --no-verify to bypass."
    else
      echo "verdict: verification did not come back LOW - push blocked."
      echo "verdict: inspect with 'verdict logs <run-id>', fix, or push with --no-verify to bypass."
    fi
    exit 1
  fi
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
            if existing == HOOK_SCRIPT:
                raise HookError("verdict pre-push hook is already installed and up to date")
            # A verdict-installed hook from an older version - safe to
            # replace in place rather than forcing an uninstall/reinstall
            # round-trip every time the script's own logic gets fixed.
            path.write_text(HOOK_SCRIPT, encoding="utf-8", newline="\n")
            path.chmod(0o755)
            return path
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
