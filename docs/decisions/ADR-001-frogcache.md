# ADR-001: frogcache — self-hosted artifact cache with weekly auto-update and retention

**Date:** 2026-11-08
**Status:** Proposed
**Area:** frogcache / LAN deployment / CI dependency resilience

---

## Context

Some dependencies are **not on PyPI** — spaCy language models (e.g.
`en-core-web-sm==3.8.0`) exist only as GitHub release wheels on
`explosion/spacy-models`. Direct GitHub URLs made an internal project's CI
fail when github.com release downloads 504'd. We want those artifacts
cached on the LAN so builds never touch github.com — and kept **current
automatically**, retaining the latest and latest-1 files per artifact for
rollback, instead of only being told that an update exists.

Facts and constraints:

- **House rule #1 (operator, 2026-11-08): never SSH into servers.** Everything
  on the LAN host goes through `docker --context <name>` (exec/cp/volume/compose).
  No host bind paths from client machines; content lives in named docker
  volumes seeded via a throwaway container + `docker cp`.
- The LAN server (Unraid) already runs ntfy (host port **8090**) and a private
  docker registry (host port **5005**), plus monitoring and other services.
  (Hostnames/IPs are deliberately not recorded in this public repo — pass them
  via env, see `deploy/deploy.sh`.)
- Consumers use `--find-links` / `tool.uv.find-links`, which need a **flat HTML
  page of links** — nginx autoindex provides it.
- Secrets live only in docker volumes / env — never in git, never on host
  shares.
- api.github.com unauthenticated rate limit (60/hr/IP) is exhausted on our
  egress IP, so discovery must use raw.githubusercontent.com (compatibility.json),
  not the API.
- This is a real service with logic, so it lives in its own repo
  (`JollyGreenFrogs/frogcache`) with tests and Woodpecker CI — not as a bare
  compose file in the `server` repo.

## Design

Two containers from `deploy/docker-compose.yml`, deployed on the LAN host via
`deploy/deploy.sh` (docker context only):

```
cache    nginx:alpine, host network, port 8091 (8090 = ntfy), autoindex over
         the frogcache-files volume        -> http://<cache-host>:8091/
updater  image built by Woodpecker from this repo, pushed to the private
         registry on the host (:5005)
         (deploy.sh builds+pushes it locally if the registry copy is missing)
```

Volumes: `frogcache-files` (served + written), `frogcache-artifacts`
(`seed/artifacts.json`), `frogcache-data` (manifest.json with sha256 per fetch),
`frogcache-secrets` (`frogcache.env` with the ntfy token, mode 600).

Updater logic (`src/frogcache/`, stdlib only, weekly loop
`CHECK_INTERVAL_SECONDS=604800`):

1. load artifact specs from `artifacts.json` (name + source).
2. per artifact, resolve the latest upstream release:
   - `pypi` — PyPI JSON API, prefer the wheel file URL;
   - `github-compat` — repo `compatibility.json` via raw.githubusercontent.com
     (not rate-limited); download URL constructed from the release tag
     `<name>-<version>` + `file_pattern`;
   - `github-releases` — api.github.com (optional `GITHUB_TOKEN`).
3. if newer than the newest cached file: download to a `.part` temp file,
   sha256, atomic rename into `frogcache-files`;
4. **retention**: keep only the newest `RETAIN=2` files per artifact (latest +
   latest-1) — older files deleted, manifest entries pruned to match;
5. ntfy notification: info on updates, high-priority alert on failures.

The served index needs no restart after downloads (autoindex lists per request).

## Decision

We will build **frogcache** as its own repo: an nginx autoindex cache on the
LAN host at **http://<cache-host>:8091/** plus a weekly updater container that
**auto-downloads** newer upstream versions and **retains latest + latest-1**,
deployed and operated **only via `docker --context <name>`**.

**Rationale:** nothing off-the-shelf covers "watch upstream → auto-download →
retain latest N → serve flat find-links" for arbitrary files (see rejected
alternatives); a small stdlib-only Python service is testable (pytest + compileall
gate in Woodpecker) and runs anywhere.

## Rejected Alternatives

- **LanCache** — caches game/console CDN traffic (Steam, Epic, Battle.net,
  Windows Update…) via DNS interception of plain HTTP; knows nothing about pip
  wheels or GitHub release assets.
- **Sonatype Nexus / JFrog Artifactory / Pulp** — real artifact repositories
  with "retain last N versions" cleanup policies, but heavyweight (Java, GBs of
  RAM) for a NAS, and oriented to Maven/registry formats rather than a flat
  `--find-links` page. Harbor's retention is container-images only.
- **pypiserver / devpi** — PEP 503 indexes with
  upload/auth flows; not a flat links page, and no auto-update/retention of
  non-PyPI files.
- **git-pkgs/proxy** — pull-through caching proxy for package registries; caches
  on demand but has no upstream-watch/auto-refresh/retention.
- **Diun / Watchtower** — Docker image update notifier/auto-updater; images
  only, not files. (Diun is a good later addition for our own images.)
- **changedetection.io** — watches web pages (incl. GitHub release Atom feeds)
  and can notify via ntfy, but cannot download/retain files or compare versions.
- **Renovate `github-releases` datasource** — opens PRs in consumer repos; a
  good complement for flipping consumers to the cache, not a cache itself.
- **Cron daemon in the updater container (Ofelia/Supercronic/crond)** — a
  sleep-loop in the long-running container is simpler and cannot silently stop
  firing; weekly cadence is `CHECK_INTERVAL_SECONDS=604800`.

## Consequences

- Consumer CI/Dockerfiles point at `http://<cache-host>:8091/` with plain names
  + find-links and never touch github.com for these artifacts (consumer-side
  change, separate repo).
- Cache stays current automatically; rollback = point consumers at the
  latest-1 wheel still present in the index; manifest.json records sha256 of
  every fetch.
- Adding an artifact = new entry in `seed/artifacts.json` + `./deploy.sh`.
- ntfy topic `frogcache-updates` on the host's ntfy (port 8090) carries
  update/failure alerts.
- LAN-only, unauthenticated on 8091 — acceptable for a read-only artifact
  mirror on the trusted LAN; sha256s in the manifest are the integrity check.

---
