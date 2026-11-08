# ADR-002: host LLM models from HuggingFace on the LAN cache

**Date:** 2026-11-08
**Status:** Proposed
**Area:** frogcache / HuggingFace model hosting

---

## Context

LLM models (GGUF / safetensors + configs / tokenizers) live on HuggingFace as
multi-file repos (`org/model`). Downloading them from huggingface.co is slow,
can be rate-limited or blocked, and breaks fully offline/LAN-only builds. We
want the same guarantee frogcache already gives wheels: **models are cached on
the LAN and served over plain HTTP, kept current automatically**.

HF clients (`huggingface_hub`) normally talk to the HF API
(`api.huggingface.co` + `/resolve/...` downloads). A flat nginx autoindex
cannot emulate that API, so consumers fetch model files from frogcache directly
over HTTP (or sync the `hf/` tree into a local `HF_HOME` and run
`HF_HUB_OFFLINE=1`).

## Design

New source type `huggingface` in `seed/artifacts.json`:

```json
{ "name": "qwen3-8b",
  "source": { "type": "huggingface", "repo": "Qwen/Qwen3-8B-GGUF",
              "revision": "main",
              "files": ["qwen3-8b-q4_k_m-*.gguf", "config.json"] } }
```

- `repo` = `org/model`; `revision` = branch/tag/commit (default `main`);
  `files` = optional fnmatch patterns (empty = all files in the repo tree).
- The updater resolves the revision's commit sha via
  `GET /api/models/{repo}/revision/{revision}` and the file list via
  `GET /api/models/{repo}/tree/{revision}?recursive=true`.
- Files download to `hf/<org>/<model>/<sha>/<path>` in the cache (atomic
  `.part` + rename, per-file sha256 into `/data/manifest.json`) and are served
  at `http://<cache-host>:8091/hf/<org>/<model>/<sha>/<path>` by the existing
  nginx autoindex — no extra service.
- **Versioning/retention:** the revision sha acts as the artifact version.
  Retention keeps the newest `RETAIN=2` revision directories per model
  (rollback = point consumers at the previous sha's files). Wheel artifacts
  keep their existing latest+latest-1 file retention; the two never mix
  because HF files live under `hf/` and wheel matching only globs `*.whl` at
  the top level.

## Decision

Extend frogcache with a `huggingface` source type as above. Consumers download
model files from the LAN index instead of huggingface.co.

**Rationale:** reuses the existing weekly updater, atomic downloads, manifest,
retention and ntfy alerts; stdlib-only (HF API is plain JSON); no new
services or HF-API emulation.

## Rejected Alternatives

- **HF endpoint mirror (`HF_ENDPOINT=http://cache/...`)** — `huggingface_hub`
  expects the HF REST API (`/api/models/...`, auth, ETags); nginx autoindex
  can't emulate it. Would need a real proxy service (e.g. a local
  `huggingface-hub` mirror) — much bigger than this cache.
- **git clone / git-lfs of the model repo** — pulls full history and every
  file; GBs of overhead, no retention, needs git-lfs on the client.
- **Plain pass-through proxy of huggingface.co** — no caching/retention;
  still depends on HF being reachable.
- **Reuse the wheel flow verbatim** — wheels are single files with parseable
  versions; HF models are multi-file with sha "versions", so they get their
  own layout (`hf/.../<sha>/`) and per-revision retention.

## Consequences

- Adding a model = one entry in `seed/artifacts.json` + `./deploy.sh`.
- Consumers fetch `http://<cache-host>:8091/hf/<org>/<model>/<sha>/<file>`
  (sha from the manifest or the index listing); for offline `huggingface_hub`
  usage, sync the `hf/` tree into `HF_HOME` and set `HF_HUB_OFFLINE=1`.
- HF tree API paginates (~1000 entries/request); model repos with more files
  need the `limit` raised in `hf.py`.
- LAN-only, unauthenticated serving on 8091 — same trust model as ADR-001;
  per-file sha256 in the manifest is the integrity check.

---
