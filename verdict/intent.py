"""
Module 2 - Intent Extractor.

Input:  repo path + git ref (or explicit intent text)
Output: IntentResult {diff, intent, vague, vague_reason}

Phase 0 finding: nearly all bad scenarios came from vague intent
("fix in the rapidapi or ytdlp", "unnmaes changes"), not from the model.
So vagueness detection is a first-class output here, not an afterthought.
Deterministic logic only - the LLM's job stays narrow (Section 13).
"""
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

MIN_INTENT_LENGTH = 15
MIN_CONTENT_WORDS = 3

# Messages that are pure placeholder noise regardless of length
PLACEHOLDER_PATTERNS = [
    r"^(wip|tmp|temp|test|asdf|foo|misc)\b",
    r"^(final|the final)\b.*\b(fix|version|one)\b",
    r"^(update|fix|change|edit)s?\.?$",
    r"^minor (fix|change|update)e?s?\.?$",
    r"^(bug ?fix|hotfix|quickfix)e?s?\.?$",
    r"^save\b",
]

# Words that carry no information about WHAT the change does
STOPWORDS = {
    "a", "an", "the", "in", "on", "of", "to", "and", "or", "for", "with",
    "fix", "fixed", "fixes", "update", "updated", "updates", "change",
    "changed", "changes", "final", "some", "few", "minor", "small", "misc",
    "stuff", "things", "code", "chore", "feat", "refactor", "done", "safe",
}


@dataclass
class IntentResult:
    diff: str
    intent: str
    vague: bool
    vague_reason: str | None = None


class GitError(Exception):
    pass


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise GitError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def check_vagueness(intent: str) -> str | None:
    """Return a human-readable reason if the intent is too vague to verify against, else None."""
    text = intent.strip()
    lowered = text.lower()

    if len(text) < MIN_INTENT_LENGTH:
        return f"intent is too short ({len(text)} chars) to state what the change should do"

    for pattern in PLACEHOLDER_PATTERNS:
        if re.search(pattern, lowered):
            return f"intent matches placeholder pattern ('{text}') - it names no verifiable behavior"

    # Strip conventional-commit prefix (feat:/fix:/chore(scope):) before counting content words
    body = re.sub(r"^\w+(\([^)]*\))?[:!]\s*", "", lowered)
    words = re.findall(r"[a-z][a-z0-9_'-]*", body)
    content_words = [w for w in words if w not in STOPWORDS]
    if len(content_words) < MIN_CONTENT_WORDS:
        return "intent contains no concrete detail about what behavior changed"

    return None


def extract_from_commit(repo: Path, ref: str = "HEAD") -> IntentResult:
    """Diff + commit message of a single commit."""
    intent = _git(repo, "log", "-1", "--format=%B", ref).strip()
    diff = _git(repo, "show", ref, "--format=", "--no-color")
    return _build(diff, intent)


def extract_from_range(repo: Path, base: str, head: str = "HEAD", intent: str | None = None) -> IntentResult:
    """Diff of base..head; intent from arg or the combined commit messages in the range."""
    diff = _git(repo, "diff", f"{base}...{head}", "--no-color")
    if intent is None:
        intent = _git(repo, "log", "--format=%s", f"{base}..{head}").strip()
    return _build(diff, intent)


def extract_from_working_tree(repo: Path, intent: str) -> IntentResult:
    """Uncommitted changes + explicitly stated intent (live/watch mode will use this)."""
    diff = _git(repo, "diff", "HEAD", "--no-color")
    return _build(diff, intent)


def _build(diff: str, intent: str) -> IntentResult:
    reason = check_vagueness(intent)
    return IntentResult(diff=diff, intent=intent, vague=reason is not None, vague_reason=reason)
