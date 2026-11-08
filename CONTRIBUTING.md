# Contributing

Read [docs/coding-standards.md](docs/coding-standards.md) before editing — it is
the canonical house standard (synced from `<redacted-profile-repo>`). Key points
for this repo: Conventional Commits; PRs required for `main` (no direct pushes);
Woodpecker CI only (`.woodpecker.yml`); functions over classes; models only in
`src/frogcache/data_models/`; files under ~200 lines; docstrings with
Args/Returns/Raises; runtime config is env-only; secrets never committed.

Architecture decisions live in `docs/decisions/ADR-*.md` (ADR-001 owns this
service's design). Never deploy from a feature branch; deploys need explicit
per-occurrence operator approval. And the first rule: **never SSH into servers —
use `docker --context`** (see `deploy/deploy.sh`).
