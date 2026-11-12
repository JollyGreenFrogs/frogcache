# frogcache

Self-hosted LAN **artifact cache** with **weekly auto-update**: serves cached
downloads (wheels, release assets, …) as a flat HTTP index for `--find-links`,
watches each artifact's upstream once a week, **downloads newer versions
automatically**, and retains **latest + latest-1** files per artifact
(rollback). Alerts/updates go to an ntfy topic (`frogcache-updates`).

Motivation: spaCy language models are only published as GitHub release wheels,
and github.com release downloads once 504'd and failed CI. Design:
[docs/decisions/ADR-001-frogcache.md](docs/decisions/ADR-001-frogcache.md).

> This repo is public — it contains **no internal hostnames, IPs, or project
> names**. Deploy it with your own values (see [Deploy](#deploy--no-ssh-docker-context-only)).

**License: [CC BY-NC-SA 4.0](LICENSE) — source-available, no commercial reuse.**
**Co-authored by humans and AI** — see [Authorship](#authorship--human--ai-co-creation).

## Layout

```
├── LICENSE                 # CC BY-NC-SA 4.0 (no commercial reuse)
├── CONTRIBUTING.md         # what counts as a contribution + AI co-authorship rules
├── src/frogcache/          # updater service (stdlib only)
│   ├── cli.py              #   --once / --loop (weekly), orchestration
│   ├── upstream.py         #   PyPI / GitHub-compat / GitHub-releases sources
│   ├── cache.py            #   download (atomic), retention, manifest
│   ├── versions.py         #   version parse/compare
│   ├── notify.py           #   ntfy
│   ├── config.py           #   env-only config
│   └── data_models/        #   dataclasses only (artifact specs, config, manifest)
├── tests/                  # pytest
├── deploy/
│   ├── docker-compose.yml  # cache (nginx :8091) + updater containers, named volumes
│   └── deploy.sh           # no-SSH deploy via docker context (env-configured)
├── seed/
│   ├── artifacts.json      # what to cache + where upstream lives
│   └── wheels/             # initial cache content
├── Dockerfile              # updater image (built by Woodpecker -> your registry)
└── .woodpecker.yml         # CI: compileall + pytest on push/PR; image build on main
```

## Deploy — no SSH, docker context only

Rule #1: **never SSH into servers**; everything goes through
`docker --context <name>`.

All host-specific values come from the environment (nothing internal is
hardcoded in this repo):

```bash
cd deploy
export CONTEXT=<docker-context> \
       CACHE_HOST=<lan-host-or-ip> \
       UPDATER_IMAGE=<registry>/frogcache/updater:latest
./deploy.sh        # seeds volumes, sets up ntfy publisher, ensures image, compose up, verifies
curl -s "http://$CACHE_HOST:8091/" | grep -o 'en_core_web_sm[^"<]*'
```

`deploy.sh` also accepts `NTFY_TOPIC_URL` (default
`http://$CACHE_HOST:8090/frogcache-updates`) and `NTFY_CONTAINER` (default
`ntfy`) if your ntfy setup differs.

Port **8091** because 8090 is commonly taken on the host (e.g. by ntfy).

## Consume

- pip (CI / Dockerfile): `--find-links http://<cache-host>:8091/`
- uv (in `pyproject.toml`): `[tool.uv] find-links = ["http://<cache-host>:8091/"]`
  + plain name `"en-core-web-sm==3.8.0"` (not a direct URL, so find-links is
  consulted)

## Add an artifact

Add an entry to `seed/artifacts.json` and re-run `deploy/deploy.sh`:

```json
{ "name": "en-core-web-sm",
  "source": { "type": "github-compat", "repo": "explosion/spacy-models",
              "branch": "master",
              "file_pattern": "en_core_web_sm-{version}-py3-none-any.whl" } }
```

Source types: `pypi` (PyPI JSON, wheel preferred), `github-compat`
(compatibility.json via raw.githubusercontent.com — not rate-limited),
`github-releases` (api.github.com; set `GITHUB_TOKEN` in the secrets file if
the 60/hr unauthenticated limit is exhausted).

## LLM models (HuggingFace)

frogcache can also host HF model files, so consumers never need
huggingface.co. Add a `huggingface` source — `repo` is `org/model`,
`revision` is a branch/tag/commit (default `main`), `files` is an optional
list of glob patterns (default: all files in the repo):

```json
{ "name": "qwen3-8b",
  "source": { "type": "huggingface", "repo": "Qwen/Qwen3-8B-GGUF",
              "revision": "main",
              "files": ["qwen3-8b-q4_k_m-*.gguf", "config.json"] } }
```

Files are cached under `hf/<org>/<model>/<revision-sha>/…` (design:
[docs/decisions/ADR-002-hf-model-hosting.md](docs/decisions/ADR-002-hf-model-hosting.md))
and served by the same index:

```bash
curl -OJ "http://<cache-host>:8091/hf/Qwen/Qwen3-8B-GGUF/<revision-sha>/qwen3-8b-q4_k_m.gguf"
```

The revision sha is listed in the index and recorded in Postgres (table `fetches`, via Alembic migrations) with per-file
sha256; every action is in table `events` (audit log, ADR-004). Retention keeps the newest `RETAIN=2` **revisions** per
model — rollback = point consumers at the previous sha's files. For offline
`huggingface_hub` usage, sync the `hf/` tree into `HF_HOME` and set
`HF_HUB_OFFLINE=1`.

## Retention & updates

Every `CHECK_INTERVAL_SECONDS` (default **604800 = weekly**) the updater:
resolves the latest upstream version per artifact → downloads if newer
(atomic `.part` + rename, sha256 recorded in Postgres (`fetches` table)) → prunes
all but the newest `RETAIN=2` files per artifact → posts to ntfy
(`frogcache-updates`: info on updates, high priority on failures).

Manual single run (docker context only):

```bash
docker --context <name> compose -f deploy/docker-compose.yml run --rm updater python -m frogcache --once
```

## Dev

```bash
uv run --with pytest pytest -q        # or: PYTHONPATH=src python3 -m pytest -q tests
PYTHONPATH=src python3 -m compileall -q src tests
```

## Current seed contents

| wheel | sha256 |
|---|---|
| en_core_web_sm-3.8.0-py3-none-any.whl | 9f6d3f8d23683ee19b9036f8e209d56e2a5f860d191b2dbc0729dc230cdd6051 |

(Rebuilt locally from the uv cache, so the hash differs from the GitHub release
wheel; content identical — reference by name.)

## License

frogcache code and documentation are licensed under
**CC BY-NC-SA 4.0** — free to use, share, and adapt for **non-commercial**
purposes with attribution and share-alike (ShareAlike applies only to shared
adaptations). Because of the NonCommercial clause this is **source-available,
not OSI "open source"**. **Commercial reuse is not permitted** without
separate written permission from the relevant copyright holders (contributors
retain their copyright).
See [LICENSE](LICENSE). Cached third-party artifacts (e.g. `seed/wheels/`)
are not covered — they keep their upstream licenses (spaCy models are
Apache-2.0).

## Authorship — human + AI co-creation

All of frogcache is **co-created by humans and AI assistants** (Mistral Vibe /
Copilot-style agents). The AI contribution is explicit in the history: every
commit ends with the trailers `Generated by Mistral Vibe.` and
`Co-Authored-By: Mistral Vibe <vibe@mistral.ai>`. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the contribution rules that apply.
