"""
Module 7 - Reporter.

Input:  everything the pipeline produced (intent, scenarios, results, risk)
Output: formatted terminal text or JSON, plus a saved run record under
        .verdict/runs/<run_id>.json so every verdict stays auditable.
        (File store is Phase 1 scope; Postgres takes over in Phase 2.)
"""
import json
import secrets
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from verdict.generator import GenerationResult
from verdict.intent import IntentResult
from verdict.sandbox import SandboxResult
from verdict.scorer import RiskReport

_STATUS_TAGS = {
    "passed": "PASSED ",
    "failed": "FAILED ",
    "uncertain": "UNCLEAR",
    "error": "BADTEST",
    "timeout": "TIMEOUT",
}


def new_run_id() -> str:
    return f"run_{secrets.token_hex(3)}"


def build_record(
    run_id: str,
    intent_result: IntentResult,
    generation: GenerationResult,
    results: list[SandboxResult],
    risk: RiskReport,
    model: str,
    tokens: dict | None = None,
) -> dict:
    return {
        "run_id": run_id,
        "status": "completed",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "intent": intent_result.intent,
        "vague": intent_result.vague,
        "diff_lines": intent_result.diff.count("\n"),
        "diff": intent_result.diff,
        "scenario_source": generation.source,
        "scenario_from_cache": generation.from_cache,
        "generation_prompt": generation.prompt,
        "generation_raw_response": generation.raw_response,
        "results": [asdict(r) for r in results],
        "risk": asdict(risk),
        "tokens": tokens or {},
    }


def build_incomplete_record(
    run_id: str,
    status: str,  # "errored" | "skipped"
    stage: str,
    reason: str,
    model: str,
    intent_result: IntentResult | None = None,
    tokens: dict | None = None,
) -> dict:
    """A run that never reached a verdict still leaves evidence. An errored
    or skipped run that vanishes is a hole in the audit trail."""
    record = {
        "run_id": run_id,
        "status": status,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "failed_stage": stage,
        "reason": reason,
        "tokens": tokens or {},
    }
    if intent_result is not None:
        record["intent"] = intent_result.intent
        record["vague"] = intent_result.vague
        record["diff_lines"] = intent_result.diff.count("\n")
        record["diff"] = intent_result.diff
    return record


def save_run(record: dict, root: Path | None = None) -> Path:
    runs_dir = (root or Path.cwd()) / ".verdict" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{record['run_id']}.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")

    # Phase 2 dual-write: mirror into Postgres when configured. File write
    # already succeeded; a DB failure warns on stderr, never breaks the run.
    from verdict import store
    from verdict.config import load_config

    store.mirror_run(record, load_config(root))
    return path


def load_run(run_id: str, root: Path | None = None) -> dict | None:
    path = (root or Path.cwd()) / ".verdict" / "runs" / f"{run_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_runs(root: Path | None = None, limit: int | None = None) -> list[dict]:
    """All run records, newest first - the history a human actually browses."""
    runs_dir = (root or Path.cwd()) / ".verdict" / "runs"
    if not runs_dir.exists():
        return []
    records = []
    for path in runs_dir.glob("run_*.json"):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue  # a corrupt record must not hide the rest of the history
    records.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return records[:limit] if limit else records


def latest_run_id(root: Path | None = None) -> str | None:
    records = list_runs(root, limit=1)
    return records[0]["run_id"] if records else None


def format_terminal(record: dict) -> str:
    risk = record["risk"]
    lines = []
    coverage = risk["coverage"]
    coverage_txt = f" - coverage {coverage:.0%}" if coverage is not None else ""
    if risk.get("inconclusive"):
        coverage_txt += f" ({risk['inconclusive']} scenario(s) produced no evidence, excluded)"
    lines.append(f"{risk['level']} RISK - {risk['passed']}/{risk['passed'] + risk['failed']} conclusive passed{coverage_txt}")
    cap_dropped = record.get("scenario_cap_dropped") or []
    if cap_dropped:
        lines.append(
            f"  ! {len(cap_dropped)} validated scenario(s) NOT run at all (--max-scenarios cap): "
            f"{', '.join(cap_dropped)}"
        )
    lines.append("")
    for r in record["results"]:
        tag = _STATUS_TAGS.get(r["status"], r["status"].upper())
        first_line = (r["stdout"].strip().splitlines() or [""])[0]
        lines.append(f"  {tag} {r['scenario_name']} ({r['duration_s']}s)")
        if first_line:
            lines.append(f"          {first_line[:100]}")
    lines.append("")
    for reason in risk["reasons"]:
        lines.append(f"  {reason}")
    lines.append("")
    lines.append(f"Run ID: {record['run_id']}   [full evidence: verdict logs {record['run_id']}]")
    return "\n".join(lines)


_HTML_RISK_COLORS = {
    "LOW": "#22c55e",
    "MEDIUM": "#eab308",
    "HIGH": "#ef4444",
    "UNVERIFIED": "#6b7280",
}

_HTML_STATUS_COLORS = {
    "passed": "#22c55e",
    "failed": "#ef4444",
    "uncertain": "#eab308",
    "error": "#a3a3a3",
    "timeout": "#d946ef",
}


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_html(record: dict) -> str:
    """Self-contained HTML report - the shareable, human-readable face of a run.
    No external assets, opens anywhere, safe to attach to a PR or a message."""
    status = record.get("status", "completed")
    risk = record.get("risk") or {}
    level = risk.get("level", status.upper())
    color = _HTML_RISK_COLORS.get(level, "#6b7280")

    rows = []
    for r in record.get("results", []):
        s_color = _HTML_STATUS_COLORS.get(r["status"], "#a3a3a3")
        first_line = (r["stdout"].strip().splitlines() or [""])[0]
        rows.append(f"""
    <details class="scenario">
      <summary>
        <span class="tag" style="background:{s_color}22;color:{s_color};border-color:{s_color}66">{_esc(r['status'].upper())}</span>
        <strong>{_esc(r['scenario_name'])}</strong>
        <span class="dim">{r['duration_s']}s &middot; exit {r['exit_code']}</span>
      </summary>
      {f'<p class="dim finding">{_esc(first_line)}</p>' if first_line else ''}
      <h4>test code</h4>
      <pre>{_esc(r.get('test_code', ''))}</pre>
      {f"<h4>stdout</h4><pre>{_esc(r['stdout'].strip()[:4000])}</pre>" if r['stdout'].strip() else ''}
      {f"<h4>stderr</h4><pre class='err'>{_esc(r['stderr'].strip()[:4000])}</pre>" if r['stderr'].strip() else ''}
    </details>""")

    reasons = "".join(f"<li>{_esc(reason)}</li>" for reason in risk.get("reasons", []))
    if status != "completed":
        reasons += f"<li>run {_esc(status)} at stage '{_esc(record.get('failed_stage', '?'))}': {_esc(record.get('reason', ''))}</li>"
    cap_dropped = record.get("scenario_cap_dropped") or []
    if cap_dropped:
        reasons += (
            f"<li><strong>{len(cap_dropped)} validated scenario(s) NOT run at all (--max-scenarios cap):</strong> "
            f"{_esc(', '.join(cap_dropped))}</li>"
        )
    for ov in record.get("overrides", []):
        reasons += (
            f"<li><strong>OVERRIDDEN</strong> by {_esc(ov.get('actor', '?'))} "
            f"at {_esc(ov.get('created_at', '?'))}: {_esc(ov.get('reason', ''))}</li>"
        )

    coverage = risk.get("coverage")
    coverage_txt = f"{coverage:.0%} of validated scenarios reached a conclusive result" if coverage is not None else "no conclusive evidence - human review required"
    if risk.get("inconclusive"):
        # plain "·", not the &middot; HTML entity - this string goes through
        # _esc() below, which would escape the literal "&" into "&amp;middot;"
        coverage_txt += f" · {risk['inconclusive']} scenario(s) produced no evidence and were excluded from that figure"
    tokens = record.get("tokens") or {}
    tokens_txt = (
        f"{tokens.get('llm_calls', 0)} LLM call(s) &middot; {tokens.get('prompt_tokens', 0):,} tokens in / "
        f"{tokens.get('output_tokens', 0):,} out &middot; {tokens.get('llm_seconds', 0)}s LLM time"
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(record['run_id'])} &middot; {_esc(level)} &middot; Verdict</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin:0; padding:2rem 1rem; background:#0b0f14; color:#e5e7eb;
         font:15px/1.6 ui-monospace,'Cascadia Code',Consolas,monospace; }}
  main {{ max-width:860px; margin:0 auto; }}
  h1 {{ font-size:1.1rem; letter-spacing:.35em; color:#22d3ee; margin:0 0 .25rem; }}
  .sub {{ color:#6b7280; font-style:italic; margin:0 0 2rem; }}
  .verdict {{ border:1px solid {color}66; border-left:6px solid {color}; border-radius:8px;
              padding:1.2rem 1.5rem; margin-bottom:1.5rem; background:{color}0d; }}
  .level {{ display:inline-block; background:{color}; color:#0b0f14; font-weight:700;
            padding:.15rem .7rem; border-radius:4px; margin-right:.75rem; }}
  .dim {{ color:#6b7280; }}
  .meta {{ margin:1.5rem 0; }} .meta div {{ margin:.15rem 0; }}
  .meta b {{ color:#22d3ee; font-weight:600; display:inline-block; min-width:7rem; }}
  .scenario {{ border:1px solid #1f2937; border-radius:8px; margin:.6rem 0; background:#0f141b; }}
  .scenario summary {{ cursor:pointer; padding:.7rem 1rem; display:flex; gap:.8rem; align-items:center; }}
  .scenario[open] summary {{ border-bottom:1px solid #1f2937; }}
  .scenario h4 {{ margin:1rem 1rem .3rem; color:#6b7280; font-size:.75rem; text-transform:uppercase; letter-spacing:.1em; }}
  .finding {{ margin:.6rem 1rem 0; }}
  .tag {{ font-size:.72rem; font-weight:700; padding:.1rem .5rem; border-radius:4px; border:1px solid; }}
  pre {{ background:#0b0f14; border:1px solid #1f2937; border-radius:6px; padding:.8rem 1rem;
        margin:.3rem 1rem 1rem; overflow-x:auto; font-size:.82rem; }}
  pre.err {{ color:#fca5a5; }}
  ul {{ color:#9ca3af; }}
  footer {{ margin-top:2rem; color:#4b5563; font-size:.8rem; }}
</style></head><body><main>
  <h1>VERDICT</h1>
  <p class="sub">proof, not vibes</p>
  <div class="verdict">
    <span class="level">{_esc(level)}{' RISK' if status == 'completed' and level != 'UNVERIFIED' else ''}</span>
    <span>{_esc(coverage_txt)}</span>
    <ul>{reasons}</ul>
  </div>
  <div class="meta">
    <div><b>run</b> {_esc(record['run_id'])} <span class="dim">({_esc(status)})</span></div>
    <div><b>when</b> {_esc(record.get('created_at', ''))}</div>
    <div><b>model</b> {_esc(record.get('model', ''))}</div>
    <div><b>intent</b> {_esc(record.get('intent', '(never extracted)'))}</div>
    <div><b>diff size</b> {record.get('diff_lines', 0)} lines</div>
    <div><b>llm usage</b> {tokens_txt}</div>
  </div>
  {''.join(rows) if rows else '<p class="dim">no scenarios were executed for this run.</p>'}
  <footer>generated by verdict &middot; every claim above is backed by the recorded run data in .verdict/runs/{_esc(record['run_id'])}.json</footer>
</main></body></html>
"""


def save_html(record: dict, root: Path | None = None) -> Path:
    reports_dir = (root or Path.cwd()) / ".verdict" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{record['run_id']}.html"
    path.write_text(render_html(record), encoding="utf-8")
    return path


def format_json(record: dict) -> str:
    """Machine-readable output - everything except the bulky audit fields."""
    slim = {k: v for k, v in record.items() if k not in ("diff", "generation_prompt", "generation_raw_response")}
    for r in slim["results"]:
        r.pop("test_code", None)
    return json.dumps(slim, indent=2)
