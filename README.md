# Verdict

An independent, self-hosted pre-deployment verification system for AI-generated and human-written code.

Verdict reads a code diff plus its stated intent, and proves — through generated, executed tests — whether the change actually does what it claims, before a human reviews it. It is not another AI coding assistant; it is the neutral referee that sits downstream of tools like Cursor, Claude Code, and Antigravity, verifying their output rather than producing more of it.

Full direction, architecture, module breakdown, and phased roadmap: [`Verifier_Project_Direction.docx`](./Verifier_Project_Direction.docx).

## Status

Currently at **Phase 0** — validating the core assumption (LLM-generated scenario precision against real PRs) offline, before any infrastructure is built. See the direction doc, Section 12, for the full gated roadmap.

## Local model setup

Verdict's scenario generation step runs against a self-hosted [Ollama](https://ollama.com) instance — no cloud LLM API is used, by design (see the direction doc, Section 5).

- Model: `qwen2.5-coder:7b`
- Run `ollama serve` to start the local inference server before running Verdict.

## License

MIT — see [LICENSE](./LICENSE).
