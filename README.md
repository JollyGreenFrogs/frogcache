# frogcache

Self-hosted LAN **artifact cache** on <redacted-host> (<redacted-ip>) with **weekly
auto-update**: serves cached downloads (wheels, release assets, …) as a flat
HTTP index for `--find-links`, watches each artifact's upstream once a week,
**downloads newer versions automatically**, and retains **latest + latest-1**
files per artifact (rollback). Alerts/updates go to ntfy topic
`frogcache-updates`.

Motivation: spaCy language models are only published as GitHub release wheels
and github.com release downloads 504'd, failing <redacted-project> CI (PR #115, pipeline
444). Design: [docs/decisions/ADR-001-frogcache.md](docs/decisions/ADR-001-frogcache.md).

## Layout

```
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
│   └── deploy.sh           # no-ssh deploy via docker --context <redacted-host>
├── seed/
│   ├── artifacts.json      # what to cache + where upstream lives
│   └── wheels/             # initial cache content
├── Dockerfile              # updater image (built by Woodpecker -> <redacted-ip>:5005)
└── .woodpecker.yml         # CI: compileall + pytest on push/PR; image build on main
```

## Deploy (<redacted-host>) — no SSH, docker context only

House rule #1: **never SSH into servers**; everything goes through
`docker --context <redacted-host>`.

```bash
cd deploy
./deploy.sh        # seeds volumes, sets up ntfy publisher, ensures image, compose up, verifies
curl -s http://<redacted-ip>:8091/ | grep -o 'en_core_web_sm[^"<]*'
```

Port **8091** because <redacted-host>'s ntfy already owns host port 8090.

## Consume

- pip (CI / Dockerfile): `--find-links http://<redacted-ip>:8091/`
- uv (<redacted-project>): `[tool.uv] find-links = ["http://<redacted-ip>:8091/"]` + plain name
  `"en-core-web-sm==3.8.0"` (not a direct URL, so find-links is consulted)

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

## Retention & updates

Every `CHECK_INTERVAL_SECONDS` (default **604800 = weekly**) the updater:
resolves the latest upstream version per artifact → downloads if newer
(atomic `.part` + rename, sha256 recorded in `/data/manifest.json`) → prunes
all but the newest `RETAIN=2` files per artifact → posts to ntfy
(`frogcache-updates`: info on updates, high priority on failures).

Manual single run (docker context only):

```bash
docker --context <redacted-host> compose -f deploy/docker-compose.yml run --rm updater python -m frogcache --once
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
