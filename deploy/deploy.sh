#!/usr/bin/env bash
# deploy.sh — deploy frogcache WITHOUT ssh: every operation goes through the
# docker context named by $CONTEXT (rule #1: never ssh into servers).
#
# Steps:
#   1. create the data-server share dirs (+ the air-gapped sandbox volume)
#   2. migrate any legacy named volumes into the shares, then seed shares via
#      a throwaway container bind mount + `docker cp`
#      (seed/wheels -> files share, seed/artifacts.json -> artifacts share)
#   3. idempotent ntfy publisher setup (user frogcache, topic $NTFY_TOPIC,
#      token) and write the token into the secrets share (mode 600)
#   4. ensure the updater image exists on the host (pull; build+push locally if missing)
#   5. docker compose up -d
#   6. verify the index serves the seeded wheel
#
# Usage:   ./deploy.sh            (run from deploy/)
# Env (required — no internal hosts are hardcoded in this public repo):
#          CONTEXT        docker context name for the LAN host
#          CACHE_HOST     host/IP the cache index is served on
#          UPDATER_IMAGE  updater image, e.g. <registry>/frogcache/updater:latest
# Env (optional):
#          NTFY_TOPIC_URL default http://$CACHE_HOST:8090/frogcache-updates
#          NTFY_TOPIC     topic name, derived from NTFY_TOPIC_URL if unset
#          NTFY_CONTAINER ntfy container name on the host (default: ntfy)
set -euo pipefail

CONTEXT="${CONTEXT:?set CONTEXT to the docker context name for the LAN host}"
CACHE_HOST="${CACHE_HOST:?set CACHE_HOST to the LAN host/IP the cache is served on}"
UPDATER_IMAGE="${UPDATER_IMAGE:?set UPDATER_IMAGE, e.g. <registry>/frogcache/updater:latest}"
DATA_SHARE="${DATA_SHARE:?set DATA_SHARE to the mounted data-server share root on the docker host, e.g. /mnt/<data-share>}"
export DATA_SHARE
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
D=(docker --context "$CONTEXT")
CACHE_URL="http://${CACHE_HOST}:8091/"
NTFY_TOPIC_URL="${NTFY_TOPIC_URL:-http://${CACHE_HOST}:8090/frogcache-updates}"
# ACL topic must match the topic in the URL, otherwise the publisher token
# would be saved but denied access on a deny-by-default ntfy server. Strip
# any query/fragment (e.g. ?priority=high) before extracting the topic.
NTFY_TOPIC_URL_PATH="${NTFY_TOPIC_URL%%[?#]*}"
NTFY_TOPIC="${NTFY_TOPIC:-$(basename "${NTFY_TOPIC_URL_PATH%/}")}"
[[ -n "$NTFY_TOPIC" ]] || { echo "ERROR: cannot derive ntfy topic from NTFY_TOPIC_URL='$NTFY_TOPIC_URL'" >&2; exit 1; }
NTFY_CONTAINER="${NTFY_CONTAINER:-ntfy}"

create_shares() {
    # create share dirs ON THE DOCKER HOST via the context (DATA_SHARE is a
    # host path there, not necessarily visible/writable on this client)
    "${D[@]}" run --rm -v "$DATA_SHARE:/data" alpine mkdir -p /data/files /data/artifacts /data/data /data/secrets
    # sandbox stays a named volume (air-gapped staging, never on the share)
    "${D[@]}" volume create frogcache-sandbox >/dev/null
}

# seed_share <share-subdir> <local-path> <container-path> — copy local content
# into a real data-server share via a throwaway container bind mount (ADR-003 §C).
seed_share() {
    local sub="$1" src="$2" dest="$3" tmp
    tmp="frogcache-seed-$$"
    "${D[@]}" create --name "$tmp" -v "$DATA_SHARE/$sub:$dest" alpine true >/dev/null
    if [[ -d "$src" ]]; then
        "${D[@]}" cp "$src/." "$tmp:$dest"
    else
        "${D[@]}" cp "$src" "$tmp:$dest/"
    fi
    "${D[@]}" rm "$tmp" >/dev/null
}

setup_ntfy_publisher() {
    if ! "${D[@]}" exec "$NTFY_CONTAINER" ntfy user list 2>/dev/null | grep -q '^user frogcache '; then
        local pw
        pw="$(openssl rand -hex 24)"
        printf '%s\n%s\n' "$pw" "$pw" | "${D[@]}" exec -i "$NTFY_CONTAINER" ntfy user add frogcache >/dev/null
    fi
    "${D[@]}" exec "$NTFY_CONTAINER" ntfy access frogcache "$NTFY_TOPIC" write-only >/dev/null
    local token
    token="$("${D[@]}" exec "$NTFY_CONTAINER" ntfy token add frogcache | grep -o 'tk_[A-Za-z0-9]*' | head -1)"
    if [[ -z "$token" ]]; then
        echo "ERROR: could not obtain ntfy token for user frogcache" >&2
        exit 1
    fi
    # merge NTFY vars into the existing secrets file (keep DATABASE_URL/REGISTRY_*)
    "${D[@]}" run --rm -i -v "$DATA_SHARE/secrets:/secrets" -e NTFY_TOPIC_URL="$NTFY_TOPIC_URL" -e NTFY_TOKEN="$token" python:3.15-alpine \
        python3 -c 'import os
p = "/secrets/frogcache.env"
lines = []
try:
    lines = [l for l in open(p).read().splitlines() if l and not l.startswith(("NTFY_URL=", "NTFY_TOKEN="))]
except FileNotFoundError:
    pass
lines += [f"NTFY_URL={os.environ["NTFY_TOPIC_URL"]}", f"NTFY_TOKEN={os.environ["NTFY_TOKEN"]}"]
open(p, "w").write("\n".join(lines) + "\n")
import os as o; o.chmod(p, 0o600)'
}

validate_secrets() {
    # fresh DATA_SHARE installs have no secrets yet — fail early with a clear
    # message instead of containers crash-looping on a missing DATABASE_URL
    local missing
    missing="$("${D[@]}" run --rm -v "$DATA_SHARE/secrets:/secrets:ro" alpine \
        sh -c 'for k in DATABASE_URL REGISTRY_ADDR REGISTRY_USER REGISTRY_PASS; do grep -q "^$k=" /secrets/frogcache.env 2>/dev/null || echo "$k"; done')"
    if [[ -n "$missing" ]]; then
        echo "ERROR: $DATA_SHARE/secrets/frogcache.env is missing required keys:" >&2
        echo "$missing" | sed 's/^/  - /' >&2
        echo "Write them there (mode 600) and re-run deploy.sh" >&2
        exit 1
    fi
}

migrate_volumes() {
    # upgrade path: pre-ADR-003 deploys kept data in COMPOSE PROJECT-SCOPED
    # named volumes (e.g. deploy_frogcache-files), not bare names — discover
    # the actual volume names. Each migration is ONE-TIME (durable marker in
    # the share) and never overwrites existing share files (cp -n), so
    # redeploys can't erase auto-added specs, replace secrets, or restore
    # pruned files.
    local sub vol found
    for sub in files artifacts data secrets; do
        if "${D[@]}" run --rm -v "$DATA_SHARE/$sub:/to" alpine test -f /to/.migrated-from-volume; then
            continue  # already migrated on a previous deploy
        fi
        found=""
        while IFS= read -r vol; do
            if [[ "$vol" == "frogcache-$sub" || "$vol" == *_"frogcache-$sub" ]]; then
                found="$vol"; break
            fi
        done < <("${D[@]}" volume ls --format '{{.Name}}')
        [[ -n "$found" ]] || continue
        echo "== migrating named volume $found -> $DATA_SHARE/$sub (one-time)"
        "${D[@]}" run --rm -v "$found:/from:ro" -v "$DATA_SHARE/$sub:/to" alpine \
            sh -c 'cp -a -n /from/. /to/ && touch /to/.migrated-from-volume'
    done
}

ensure_image() {
    if ! "${D[@]}" image inspect "$UPDATER_IMAGE" >/dev/null 2>&1; then
        echo "== image $UPDATER_IMAGE not on context $CONTEXT; building locally and pushing"
        docker build -t "$UPDATER_IMAGE" "$ROOT"
        "${D[@]}" push "$UPDATER_IMAGE"
    fi
}

verify() {
    echo "== index at $CACHE_URL"
    curl -sf "$CACHE_URL" | grep -o 'en_core_web_sm[^"<]*' || {
        echo "ERROR: wheel not listed in $CACHE_URL" >&2
        exit 1
    }
    "${D[@]}" ps --format '{{.Names}}\t{{.Status}}' | grep -E '^frogcache(-updater)?\b'
}

# seed_artifacts_file — merge seed/artifacts.json into the share's artifacts.json.
# Seed entries override same-name non-auto entries; auto:true entries are kept.
# The seed file is docker cp'd into a created container that is then STARTED and
# exec'd (create alone leaves it stopped); flock on the share .lock serializes
# against the running fetcher.
seed_artifacts_file() {
    local tmp
    tmp="frogcache-seed-$$"
    "${D[@]}" create --name "$tmp" -v "$DATA_SHARE/artifacts:/artifacts" python:3.15-alpine sleep 300 >/dev/null
    "${D[@]}" cp "$ROOT/seed/artifacts.json" "$tmp:/seed-artifacts.json"
    "${D[@]}" start "$tmp" >/dev/null
    "${D[@]}" exec "$tmp" sh -c 'flock -x /artifacts/.lock python3 - /seed-artifacts.json <<'"'"'PYEOF'"'"'
import json, os, sys
path = "/artifacts/artifacts.json"
live = json.load(open(path)) if os.path.exists(path) else {"artifacts": []}
seed = json.load(open(sys.argv[1]))
by_name = {a.get("name"): a for a in live.get("artifacts", [])}
for a in seed.get("artifacts", []):
    existing = by_name.get(a.get("name"))
    if existing is None or not existing.get("auto"):
        by_name[a.get("name")] = a
live["artifacts"] = list(by_name.values())
# atomic publish: sibling temp + replace while still holding the flock
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    json.dump(live, fh, indent=2)
os.replace(tmp, path)
PYEOF'
    "${D[@]}" rm -f "$tmp" >/dev/null
}

main() {
    create_shares
    migrate_volumes      # named volumes -> data-server shares (upgrade path)
    seed_share files "$ROOT/seed/wheels" /cache
    seed_artifacts_file  # merge seed specs into the live artifacts.json; keep auto-added ones
    setup_ntfy_publisher
    validate_secrets     # DATABASE_URL/REGISTRY_* must exist before compose up
    ensure_image
    UPDATER_IMAGE="$UPDATER_IMAGE" "${D[@]}" compose -f "$HERE/docker-compose.yml" up -d
    verify
    echo "OK: frogcache deployed on $CONTEXT ($CACHE_URL)"
}

main "$@"
