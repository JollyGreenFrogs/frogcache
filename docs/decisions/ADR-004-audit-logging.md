# ADR-004: audit logging + fetch state in Postgres (Alembic-versioned)

**Date:** 2026-11-09
**Status:** Proposed
**Area:** frogcache / auditability / logging

---

## Context

ADR-003 made the system reviewable ("nothing hidden"): `artifacts.json` says
what is managed, `manifest.json` records what was fetched with sha256. What is
missing is the **event history** — when each check ran, what was downloaded,
whether verification passed, what was quarantined or promoted, what retention
pruned, and every error. Operators need a persistent, queryable log without
adding infrastructure.

## Design

- One **Postgres database** on the LAN (DSN via `DATABASE_URL` in the secrets
  file — env-only, never committed) holds both the **fetch state** (table
  `fetches` — this replaces `manifest.json` as the store) and the **audit
  log** (table `events`). Schema is versioned by **Alembic**
  (`alembic/versions/`, pattern follows frogsense `backend/alembic/`); table
  definitions live in `src/frogcache/database_models/schema.py` (SQLAlchemy
  metadata, the single source of truth, imported by `alembic/env.py`).
- Schema (migration `0001_initial`): `fetches(id, artifact, version, filename,
  sha256, source, url, fetched_at, digest_verified)` + `events(id, ts, event,
  artifact, version, detail JSON)` + `meta(key, value)`. A legacy
  `manifest.json` (`STATE_FILE`) is imported into `fetches` once on first
  connect (`meta` flag), then left untouched.
- Event types: `check_start`, `update_available`, `download_start`,
  `verify_pass`, `verify_fail` (with `digest_verified`), `quarantine`,
  `promote`, `retention`, `auto_add`, `error`, `notify`.
- **Privacy rule (house standard):** log coarse categories only — artifact
  name, version, source type, sizes, digests, outcomes. Never response
  bodies, tokens, URLs with credentials, or file contents.
- The updater opens the DB once per run (`audit.connect`) and logs around each
  step in `cli.py`; a `--audit` CLI flag reads back recent events.
- Together the three stores make everything reviewable: `artifacts.json`
  (managed set) + `manifest.json` (fetches + sha256) + `audit.db` (event
  history).

## Decision

Log all frogcache actions to a SQLite audit DB on the data share via
`audit.py` (stdlib only), coarse-category fields only.

**Rationale:** persistent, queryable, backed up with the share, zero new
infra, stdlib-only constraint preserved.

## Rejected Alternatives

- **SQLite on the data share** — replaced: operator decision is Postgres + Alembic (matches the other JGF services); SQLite was dropped before merge.
- **syslog / remote log server** — new infrastructure; breaks the
  self-contained LAN design.
- **Append-only JSONL log file** — not queryable; Postgres + Alembic chosen instead.

## Consequences

- One more file on the data share (`audit.db`), covered by backups; WAL mode
  recommended for concurrent readers.
- `Config` gains `database_url` (env `DATABASE_URL`, from the secrets file) and `legacy_state_file` (env `STATE_FILE`, default `/data/manifest.json`).
- Log volume is small (events, not payloads); prune policy can be added
  later if needed (e.g. keep 90 days).

---
