# ADR-003: pull-through auto-add for files + docker images pushed into the private registry

**Date:** 2026-11-09
**Status:** Accepted (operator, 2026-11-09)
**Area:** frogcache / pull-through caching / image store

---

## Context

Today the cache index is **read-only**: a request for something not in the
volume returns 404, and only artifacts listed in `seed/artifacts.json` are
watched/updated weekly. The goal is that **frogcache is the default for
everything**: if a consumer asks for something frogcache doesn't have, frogcache
fetches it once, and from then on manages it (weekly upstream check,
auto-download, retention, ntfy alerts) like any seeded artifact.

For docker images, the org **already runs a private registry** (it currently
hosts the frogcache updater image). Rather than adding a second pull-through
proxy, that registry should become the LAN image store: frogcache pulls images
from upstream registries and pushes them into the private registry, keeping
them current.

## Design

### A. Pull-through auto-add (files: wheels, HF models, release assets)

- nginx serves the cache as today, plus `error_page 404 = @fetch;` where `@fetch`
  proxies to the updater container's new HTTP mode (stdlib `http.server`,
  loopback-only, e.g. `127.0.0.1:8092` on the host network).
- On a miss, the fetcher infers the source **from the path shape only**:
  - `<name>-<version>-<tags>.whl` → auto-create a `pypi` source (name/version
    parsed per PEP 427, reusing `cache.parse_wheel_filename`);
  - `hf/<org>/<model>/...` → auto-create a `huggingface` source (ADR-002);
  - anything else → 404 (no guessing).
- **Safety:** the request path is never fetched as a URL. Download URLs are
  constructed only against known hosts (pypi.org, huggingface.co, github.com).
- **Reviewable, nothing hidden:** auto-added specs are appended to
  `artifacts.json` with `"auto": true` and every fetch is recorded in
  `manifest.json` with its sha256 — the complete list of what frogcache manages
  is always visible in those two files (and in the index itself). No hidden
  state.
- **Auth (interim):** unauthenticated but **LAN-only** — the index binds to the
  LAN interface / host firewall, and **all LAN clients may read and write**
  (i.e. trigger auto-adds). Proper token-based auth via **jgf-auth** will be
  layered on once jgf-auth is ready (see Consequences); until then no tokens.
- On success: atomic `.part`+rename into the cache, sha256 into the manifest
  (`source: "auto:<type>"`), and the spec is **appended to `artifacts.json`**
  with `"auto": true` (the artifacts **share** becomes writable by the updater
  only; nginx stays read-only). The weekly loop then manages the artifact:
  upstream watch, auto-download of newer versions, RETAIN retention, ntfy.

### B. Docker images via the existing private registry

- New source type `docker`:

  ```json
  { "name": "nginx",
    "source": { "type": "docker", "image": "library/nginx", "tag": "latest",
                "upstream_registry": "https://index.docker.io" } }
  ```

- The updater pulls the manifest + blobs from the upstream registry (registry
  v2 API; anonymous token auth for docker.io) and pushes them to the **private
  registry** as `<private-registry>/frogcache/<image>:<tag>`, using credentials
  from the secrets file (e.g. `REGISTRY_USER`/`REGISTRY_PASS`) — never
  committed.
- "Version" = the upstream manifest **digest**. The weekly check compares the
  upstream digest; on change it pulls+pushes the new tag. Retention keeps
  **latest + latest-1** tags per image (old tag deleted in the private
  registry), mirroring file retention.
- Consumers pull from `<private-registry>/frogcache/<image>:<tag>` explicitly.
  NOTE: a `registry-mirrors` entry does NOT map namespaces (pulling `nginx:latest`
  requests `library/nginx`, not `frogcache/library/nginx`), so mirrors would miss
  the cache and fall back to Docker Hub — explicit references required.
  The private registry already hosts the updater image — one store, no new infra.

### C. Storage — real shares on the LAN data server, not volumes

- All frogcache state lives on **real shares on the LAN data server**
  (`<data-server>/<share>`, SMB or NFS), bind-mounted into the containers:
  cached files, `artifacts.json`, `manifest.json`, and the secrets file
  (mode 600). Named docker volumes are **not** used for frogcache data, so
  everything is covered by the data server's backups and redundancy.
- Bind paths come from the **data server** only — never from client machines.
- This supersedes ADR-001's "named volumes only, no host bind paths"
  constraint (marker added there); ADR-001's Decision text is otherwise
  unchanged (ADD lifecycle: supersede, don't edit).

### D. Staged download flow: sandbox → scan → promote (applies to ADR-001, ADR-002, ADR-003 alike)

Every artifact — wheel (ADR-001), HF model file (ADR-002), pull-through add or
docker image (ADR-003) — follows the same 4-step flow:

1. **Look for a new version** — existing weekly check (upstream resolve/digest
   compare) decides a download is needed.
2. **Download to the sandbox store** — a dedicated, **air-gapped staging
   area**: a named volume mounted only into the updater container
   (`/sandbox`), never into nginx, never on the data-server share, never
   backed up. Files land as `.part` and are invisible to consumers. The
   sandbox is the *only* place a named volume is used — real data lives on
   shares (§C), untrusted bytes never do.
3. **Scan/verify in the sandbox**:
   - integrity — sha256 vs the upstream digest when available (PyPI
     `digests.sha256`, GitHub asset `digest`, HF `lfs.sha256`); otherwise the
     computed sha256 becomes the recorded baseline;
   - malware — **ClamAV** `clamscan` (definitions refreshed via `freshclam`
     each run); runs in the updater image or a scanner sidecar that mounts
     only the sandbox;
   - format ("working") — `.whl` = valid zip with `*.dist-info/METADATA` +
     `WHEEL`; `.json` parses; `.gguf` has the `GGUF` magic; docker image =
     valid manifest digest after pull.
   Any failure → move to a **quarantine** dir inside the sandbox (never
   served, no manifest entry, revision stays incomplete/retryable) +
   high-priority ntfy alert.
4. **On pass, promote to the real share** — move (rename if same filesystem,
   else copy + re-verify digest) from sandbox into the cache share, then
   record the manifest entry with the verified sha256/digest (and, for
   auto-add, append the spec to `artifacts.json`). Only now is the file
   servable by nginx.

A weekly full sweep of the cache share with ClamAV (section E table) remains
as the backstop for anything fetched before this pipeline existed.

### E. Scanning everything (periodic)

| Layer | Scanner | Where |
| ----- | ------- | ----- |
| Repo code (SAST) | CodeQL `security-extended` | `.github/workflows/codeql.yml` (already), on push/PR + weekly |
| Secrets in repo | GitHub secret scanning + push protection | repo Settings → Security (enable; GHAS) |
| Dependencies (pip/docker/actions) | Dependabot | `.github/dependabot.yml` (already), weekly |
| Updater image | **Trivy** (vuln + secrets + misconfig) | CI step on image build + weekly schedule |
| Cached files (wheels, HF models, release assets — arbitrary binaries) | **ClamAV** over the cache share, on fetch + weekly | scan job against `<data-share>` |
| Images in the private registry | `trivy registry <private-registry>` | weekly job |
| Integrity of everything fetched | sha256 / manifest digest in `manifest.json` | built in (already) |

## Decision

Implement A (pull-through auto-add with path-shape source inference, persisted
auto specs) and B (a `docker` source type that pulls from upstream registries
and pushes into the existing private registry, with digest-based weekly
updates and tag retention). frogcache becomes the default fetch path for files,
and the manager of the LAN image store.

**Rationale:** one update loop (weekly check / download / retain / notify)
manages everything; auto-added specs are visible and reviewable in
`artifacts.json`; the private registry is reused instead of adding a proxy.

## Rejected Alternatives

- **`registry:2` pull-through proxy container** — rejected: the org already
  runs a private registry; a proxy cannot push into it, cannot apply
  latest+latest-1 tag retention, and would be a second update mechanism.
  frogcache pushing into the private registry keeps one loop.
- **Auto-add by fetching the raw request path/URL** — rejected: SSRF and
  cache-poisoning surface. Only path-shape inference against known hosts.
- **Separate `auto.json` for auto-added specs** — rejected: one
  `artifacts.json` with an `"auto": true` marker keeps everything visible in a
  single file and lets the existing loop read it unchanged.
- **nginx WebDAV uploads for adds** — rejected: no validation, no manifest, no
  source inference.
- **Digest-pinning consumers instead of `:latest`** — out of scope here;
  retention already keeps latest-1 for rollback.

## Consequences

- The artifacts share on the data server becomes writable (updater only);
  nginx remains read-only over the files share. All state is on real shares
  (§C), so the data server's backup/redundancy covers it.
- The updater container gains a loopback HTTP mode for 404 fetches; compose
  changes are small (no new services).
- Private-registry credentials live in the secrets volume (`frogcache.env`),
  never in git; the registry address stays env-configured (`<private-registry>`
  placeholder in this public repo).
- Storage growth: RETAIN applies to auto-added artifacts and image tags alike;
  a separate auto-retain knob can be added later if needed.
- Integrity: sha256 (files) / manifest digest (images) recorded per fetch.
- **Auth roadmap:** for now the service is unauthenticated and LAN-only (all
  LAN clients read+write). Once **jgf-auth** is finished, token-based auth
  (bearer tokens issued by jgf-auth, validated on both the read index and the
  write/fetch path) replaces the interim model — the nginx fetch endpoint and
  the updater HTTP mode both get a token check. Until then, LAN-only exposure
  is the accepted risk (operator decision, 2026-11-09).
- Diun-style alerts for image updates are already covered by the weekly loop +
  ntfy.

---
