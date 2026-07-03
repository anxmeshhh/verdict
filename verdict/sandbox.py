"""
Module 5 - Sandbox Runner.

Input:  generated tests (scenario + executable check) + repo path
Output: SandboxResult per scenario - pass/fail/uncertain, with full evidence
        (exit code, output, the exact code that ran, duration)

Each scenario runs in its own ephemeral Docker container: hard timeout,
memory/CPU caps, repo mounted read-only, torn down afterwards (--rm).

Phase 1 tradeoff, explicit: the container keeps network access because
dependency install (pip) needs it. Splitting install/run phases so the
test itself runs with --network=none is Phase 3 hardening (Section 11).
"""
import os
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from verdict.testgen import GeneratedTest

DEFAULT_IMAGE = "python:3.12-slim"
DEFAULT_TIMEOUT = 300
MEMORY_LIMIT = "512m"
CPU_LIMIT = "1"

# Docker-outside-of-Docker (compose worker): this process runs IN a container
# but talks to the HOST daemon, so every -v path must be a host path. Format:
# "container_prefix=host_prefix", e.g. "/data=C:\\verdict\\data". Unset (the
# plain CLI case) means no translation - behavior byte-identical to Phase 1.
HOST_PATH_MAP_ENV = "VERDICT_HOST_PATH_MAP"


def _host_path(p: Path) -> str:
    resolved = str(p.resolve())
    mapping = os.environ.get(HOST_PATH_MAP_ENV, "").strip()
    if not mapping or "=" not in mapping:
        return resolved
    container_prefix, _, host_prefix = mapping.partition("=")
    posix = resolved.replace("\\", "/")
    cp = container_prefix.rstrip("/")
    if posix == cp or posix.startswith(cp + "/"):
        return host_prefix.rstrip("/\\") + posix[len(cp):]
    return resolved


def _scratch_dir() -> str | None:
    """Where the per-test scratch dir lives. In DooD mode the system temp dir
    is container-local - the host daemon mounts a nonexistent path and the
    test file silently vanishes - so scratch must live under the shared,
    host-visible prefix instead."""
    mapping = os.environ.get(HOST_PATH_MAP_ENV, "").strip()
    if not mapping or "=" not in mapping:
        return None  # plain CLI: system temp, unchanged
    base = Path(mapping.partition("=")[0]) / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    return str(base)

# Exit code the generated test uses to say "cannot be checked by code"
UNCHECKABLE_EXIT = 2


@dataclass
class SandboxResult:
    scenario_name: str
    status: str  # "passed" | "failed" | "uncertain" | "timeout" | "error"
    exit_code: int | None
    stdout: str
    stderr: str
    duration_s: float
    test_code: str


class SandboxError(Exception):
    pass


# Exceptions that mean the generated TEST is broken, not the change under test.
# AssertionError / SystemExit are legitimate failure signals and stay "failed".
_BROKEN_TEST_MARKERS = (
    "AttributeError",
    "ImportError",
    "ModuleNotFoundError",
    "NameError",
    "IndentationError",
    "SyntaxError",
)


def _looks_like_broken_test(stderr: str) -> bool:
    if "AssertionError" in stderr:
        return False
    tail = stderr.strip().splitlines()[-1] if stderr.strip() else ""
    return any(tail.startswith(marker) for marker in _BROKEN_TEST_MARKERS)


def check_docker() -> bool:
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=20,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def run_test(
    test: GeneratedTest,
    repo: Path,
    image: str = DEFAULT_IMAGE,
    timeout: int = DEFAULT_TIMEOUT,
) -> SandboxResult:
    """Run one generated test in an ephemeral container against the repo."""
    container = f"verdict-{uuid.uuid4().hex[:12]}"

    with tempfile.TemporaryDirectory(dir=_scratch_dir()) as tmp:
        test_file = Path(tmp) / "scenario_test.py"
        test_file.write_text(test.code, encoding="utf-8")

        # Copy repo out of the read-only mount so pip/install steps can write;
        # the original working tree can never be mutated by a generated test.
        script = (
            "cp -r /src /app && cd /app "
            "&& (pip install -q . >/dev/null 2>&1 || pip install -q -r requirements.txt >/dev/null 2>&1 || true) "
            "&& python /verdict/scenario_test.py"
        )
        cmd = [
            "docker", "run", "--rm",
            "--name", container,
            "--memory", MEMORY_LIMIT,
            "--cpus", CPU_LIMIT,
            "-v", f"{_host_path(repo)}:/src:ro",
            "-v", f"{_host_path(Path(tmp))}:/verdict:ro",
            # Running python against an absolute script path (below) sets
            # sys.path[0] to the SCRIPT's directory (/verdict), not the repo
            # copy at /app, even after `cd /app` - so repo modules would
            # never be importable without this.
            "-e", "PYTHONPATH=/app",
            image,
            "sh", "-c", script,
        ]

        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout,
            )
            duration = time.monotonic() - start
            if proc.returncode == 0:
                status = "passed"
            elif proc.returncode == UNCHECKABLE_EXIT:
                status = "uncertain"
            elif proc.returncode in (125, 126, 127, 137):
                # 125-127: docker/exec-level failure. 137: SIGKILL - the memory
                # cap killed the container. Infrastructure death is not evidence
                # against the change - never let it masquerade as FAILED.
                status = "error"
            elif _looks_like_broken_test(proc.stderr):
                # The CHECK crashed (wrong API, bad import) - that is not
                # evidence against the change. Say so, don't fake a failure.
                status = "error"
            else:
                status = "failed"
            return SandboxResult(
                scenario_name=test.scenario.name,
                status=status,
                exit_code=proc.returncode,
                stdout=proc.stdout[-8000:],
                stderr=proc.stderr[-8000:],
                duration_s=round(duration, 2),
                test_code=test.code,
            )
        except subprocess.TimeoutExpired as e:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True)
            return SandboxResult(
                scenario_name=test.scenario.name,
                status="timeout",
                exit_code=None,
                stdout=(e.stdout or b"").decode("utf-8", "replace")[-8000:] if isinstance(e.stdout, bytes) else (e.stdout or "")[-8000:],
                stderr="",
                duration_s=round(time.monotonic() - start, 2),
                test_code=test.code,
            )
        except OSError as e:
            raise SandboxError(f"could not invoke docker: {e}") from e


def run_all(
    tests: list[GeneratedTest],
    repo: Path,
    image: str = DEFAULT_IMAGE,
    timeout: int = DEFAULT_TIMEOUT,
    on_result=None,
) -> list[SandboxResult]:
    if not check_docker():
        raise SandboxError("Docker daemon is not reachable - is Docker Desktop running?")
    results = []
    for test in tests:
        result = run_test(test, repo, image=image, timeout=timeout)
        results.append(result)
        if on_result:
            on_result(result)
    return results
