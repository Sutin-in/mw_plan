# ADR-001 — Technology stack

- **Status:** Accepted (2026-09-28)
- **Context:** Spec §47 left the database engine and hosting TBD. The assessment (§D.1) proposed a stack; it was approved with corrections.

## Decision

| Concern | Choice |
|---|---|
| Language | Python (3.12+) |
| API | FastAPI (introduced Wave 1B) |
| Contracts / validation | Pydantic v2 |
| ORM / migrations | SQLAlchemy 2.x + Alembic (introduced Wave 1B) |
| Database | **PostgreSQL** — Phase 1 target |
| Tests | pytest + Hypothesis |
| Static checks | ruff (lint + format), mypy, import-linter |
| Frontend | Node.js + Express, server-rendered, calling the API — decided by D-24 (2026-09-29), see ADR-009. |

### Constraints

- **Docker is not an architectural dependency.** It may be used for development if available; a **native PostgreSQL** installation must always remain a supported way to run and test.
- Concurrency tests (spec §19) must run against real PostgreSQL, not SQLite.
- Only dependencies that are actually used are declared. Wave 0/1A declares `pydantic` at runtime; FastAPI, SQLAlchemy, Alembic and the PostgreSQL driver are added when Wave 1B starts using them.

## Consequences

- Row locking / transaction isolation, CHECK constraints and triggers (append-only ledger and audit) are available for Waves 1B–4.
- The `check` command (`tools/check.py`) is cross-platform Python so it runs identically on the Windows host and in CI.
