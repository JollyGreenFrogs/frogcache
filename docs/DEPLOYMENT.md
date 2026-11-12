# Deployment & branch flow (Git Flow)

Branches:

- `main` — **prod**. Only merge PRs into `main` after UAT passed on `develop`.
- `develop` — integration / **UAT**. All feature PRs target `develop`.

CI (`.woodpecker.yml`) builds and pushes the updater image on every push to
`main` or `develop`:

- every build is tagged with the commit sha (`updater:<sha8>`)
- `main` additionally pushes `updater:latest` (prod)
- `develop` additionally pushes `updater:uat` (UAT)

## Deploy procedure (NO-SSH rule: docker contexts only)

All deploys run `deploy/deploy.sh` from a machine that has the docker contexts
for the target servers. No hostnames/IPs are committed to this public repo —
pass them via env. Server identities (UAT vs prod) live only in your local
docker context names and env, never in git.

```bash
# UAT first (from develop):
git checkout develop
CONTEXT=<uat-docker-context> \
CACHE_HOST=<uat-host> \
DATA_SHARE=<uat-data-share-mount-on-docker-host> \
UPDATER_IMAGE=<registry>/frogcache/updater:uat \
  ./deploy/deploy.sh

# verify UAT: index serves seeded artifacts, pull-through works, weekly check runs

# then prod (from main):
git checkout main
CONTEXT=<prod-docker-context> \
CACHE_HOST=<prod-host> \
DATA_SHARE=<prod-data-share-mount-on-docker-host> \
UPDATER_IMAGE=<registry>/frogcache/updater:latest \
  ./deploy/deploy.sh
```

`deploy.sh` is idempotent: it creates the share dirs, migrates legacy named
volumes into the shares once (marker file + `cp -n`), seeds `artifacts.json`,
sets up the ntfy publisher, validates that the secrets share contains
`DATABASE_URL`/`REGISTRY_*` **before** `compose up`, then brings the stack up
and verifies the index.

Required per-server secrets at `<DATA_SHARE>/secrets/frogcache.env` (mode 600):
`DATABASE_URL`, `REGISTRY_ADDR`, `REGISTRY_USER`, `REGISTRY_PASS`
(deploy adds `NTFY_URL`/`NTFY_TOKEN` itself).

## Promotion rule

prod (`main`) is only updated by merging `develop` after UAT verification:
`git checkout main && git merge --no-ff develop && git push`. Never push
feature branches to `main` directly.
