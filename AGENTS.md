# Project instructions

## Scope and workflow
- Implement the employer's assignment, with `docs/specification.md` as the checked transcription
  and `docs/decisions.md` as the record of our implementation choices.
- Work in small completed stages. Explain each stage and its verification in Ukrainian.
- Stage 1 ends at a working foundation and `/health`. Stop for user review before business logic.
- Keep the assignment compact (employer estimate: 3–4 hours). Use typed Python 3.13.
- Current remaining deadline supplied by the user: 24 hours on 2026-11-05 around 19:15 Kyiv;
  planning target: 2026-11-06 around 19:15 Kyiv, with time reserved for submission.
- Recommend the user's preferred Codex mode before stages, but never switch it automatically:
  Sol Medium for foundation/HTML/docs, Sol High for workers/integrations/tests,
  Astra High for concurrency/idempotency/recovery and final review.
- No subagents unless the user explicitly requests them.

## Implementation
- FastAPI, PostgreSQL, SQLAlchemy async, Alembic, HTTPX, Jinja2. Use `uv.lock`.
- Share application code between separate API and worker processes. No Redis/Celery by default.
- Commit events and durable jobs atomically; enforce event uniqueness in PostgreSQL.
- Use short DB transactions and fenced leases for workers; never hold a DB lock during HTTP calls.
- Approval and delivery-job creation must be atomic. Rejection must prevent sending.
- Enforce the LLM limit globally in PostgreSQL, including retries; concurrency is a separate limit.
- Keep retries bounded, with backoff and visible failures. Test concurrency against PostgreSQL.
- Validate draft citations against actual retrieved articles.
- Use small external-service adapters with injected dependencies for controlled tests.
- Explain non-obvious locking, retry, recovery and idempotency decisions with short comments.

## Verification and delivery
- Run `.venv313/Scripts/python.exe -m pytest`, Ruff check and Ruff format check locally.
- Also run strict mypy for `app`; full PostgreSQL coverage must meet the configured 90% floor.
- Test PostgreSQL constraints and concurrent behavior on PostgreSQL, not SQLite.
- Use controlled LLMs in bulk tests; paid calls require explicit user approval.
- Preserve meaningful Git commits without squash; commit these instructions and project docs.
- Never log secrets or commit `.env`, `.venv*`, `.idea`, credentials or customer messages.
- Public deployment and repository publication require approval after preparing reviewable results.
- Before public deployment, protect operator actions and allowlist helpdesk reply destinations.
- Do not claim exactly-once delivery for a helpdesk without durable idempotency support.

## Agent configuration used
Codex desktop, repository rules in this file, task specification and decisions in `docs/`.
The `documents` skill was used to read the employer's DOCX; it is a host tool, not a dependency.
The `openai-docs` skill was used to verify the Responses API and structured-output format.
No provider key or paid external operation is needed for foundation verification.
