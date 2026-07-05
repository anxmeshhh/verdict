"""
Module: deterministic dependency-CVE checker (Phase 6).

No LLM anywhere in this file. This is the one vuln_class deliberately never
left to the model to guess at: whether a pinned dependency has a known
advisory against it is a fact to look up in a real, public vulnerability
database (OSV.dev - no API key needed), not a proposal to generate and then
validate like the other four vuln_classes in generator.py.

Only exact-pinned versions are checked (==X.Y.Z, never a range like >=1.0 or
^1.0) - a range means the resolved version isn't actually known from the
manifest alone, and a lookup against a guessed version would be exactly the
kind of unchecked claim the rest of this project refuses to make.
"""
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

OSV_API_URL = "https://api.osv.dev/v1/query"
REQUEST_TIMEOUT = 15


@dataclass
class Dependency:
    name: str
    version: str
    ecosystem: str  # OSV ecosystem name: "PyPI", "npm"


@dataclass
class DependencyFinding:
    dependency: Dependency
    vuln_id: str
    summary: str
    severity: str  # best-effort from OSV data - "" if the advisory doesn't provide one


class DepCheckError(Exception):
    """Raised only for genuine transport/provider failures - never for 'no
    vulnerabilities found', which is just an empty result, not an error."""


def _parse_requirements_txt(text: str) -> list[Dependency]:
    deps = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http")):
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*==\s*([A-Za-z0-9_.\-]+)\s*$", line)
        if m:
            deps.append(Dependency(name=m.group(1), version=m.group(2), ecosystem="PyPI"))
    return deps


def _parse_pyproject_toml(text: str) -> list[Dependency]:
    try:
        import tomllib
    except ModuleNotFoundError:
        try:
            import tomli as tomllib  # Python 3.10 fallback
        except ModuleNotFoundError:
            return []  # no TOML parser available - skip rather than hand-parse TOML with regex

    try:
        data = tomllib.loads(text)
    except Exception:
        return []  # malformed pyproject.toml is not this module's problem to report

    deps: list[Dependency] = []

    # PEP 621: [project] dependencies = ["name==1.2.3", ...]
    for raw in data.get("project", {}).get("dependencies", []):
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*==\s*([A-Za-z0-9_.\-]+)\s*$", str(raw).strip())
        if m:
            deps.append(Dependency(name=m.group(1), version=m.group(2), ecosystem="PyPI"))

    # Poetry: [tool.poetry.dependencies] name = "1.2.3" (exact string only -
    # "^1.2.3"/"~1.2.3"/">=1.2.3" are ranges, deliberately skipped)
    poetry_deps = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
    for name, spec in poetry_deps.items():
        if name.lower() == "python" or not isinstance(spec, str):
            continue
        if re.match(r"^\d[\w.\-]*$", spec.strip()):
            deps.append(Dependency(name=name, version=spec.strip(), ecosystem="PyPI"))

    return deps


def _parse_package_json(text: str) -> list[Dependency]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []

    deps: list[Dependency] = []
    for section in ("dependencies", "devDependencies"):
        for name, spec in data.get(section, {}).items():
            spec = str(spec).strip()
            # exact pin only - "^1.2.3", "~1.2.3", ">=1.2.3", "*" are ranges,
            # the actually-resolved version isn't knowable from this file alone
            if re.match(r"^\d[\w.\-]*$", spec):
                deps.append(Dependency(name=name, version=spec, ecosystem="npm"))
    return deps


_MANIFEST_PARSERS = {
    "requirements.txt": _parse_requirements_txt,
    "pyproject.toml": _parse_pyproject_toml,
    "package.json": _parse_package_json,
}


def find_dependencies(repo: Path) -> list[Dependency]:
    """Scan the repo root for known manifest files and return every
    exact-pinned dependency found. Best-effort and root-only by design -
    this is a fact-lookup helper, not a full dependency-tree resolver."""
    deps: list[Dependency] = []
    for filename, parser in _MANIFEST_PARSERS.items():
        path = repo / filename
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        deps.extend(parser(text))
    return deps


def _query_osv(dep: Dependency) -> list[dict]:
    payload = json.dumps(
        {"package": {"name": dep.name, "ecosystem": dep.ecosystem}, "version": dep.version}
    ).encode("utf-8")
    req = urllib.request.Request(
        OSV_API_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise DepCheckError(f"OSV.dev lookup failed for {dep.name}=={dep.version}: {e}") from e
    return body.get("vulns", [])


def _severity_of(vuln: dict) -> str:
    for sev in vuln.get("severity", []):
        if sev.get("type") == "CVSS_V3":
            return sev.get("score", "")
    db_severity = vuln.get("database_specific", {}).get("severity")
    return db_severity or ""


def check_dependencies(repo: Path) -> list[DependencyFinding]:
    """The deterministic Phase 6 check: every exact-pinned dependency in the
    repo, looked up against real OSV.dev advisories. Raises DepCheckError only
    on a genuine transport failure for a given lookup - a dependency with no
    match just contributes zero findings, same as any other clean result."""
    findings: list[DependencyFinding] = []
    for dep in find_dependencies(repo):
        for vuln in _query_osv(dep):
            findings.append(
                DependencyFinding(
                    dependency=dep,
                    vuln_id=vuln.get("id", "UNKNOWN"),
                    summary=(vuln.get("summary") or vuln.get("details") or "")[:300],
                    severity=_severity_of(vuln),
                )
            )
    return findings
