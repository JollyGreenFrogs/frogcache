#!/usr/bin/env bash
# deploy.sh — deploy frogcache WITHOUT ssh: every operation goes through the
# docker context named by $CONTEXT (rule #1: never ssh into servers).
#
# Steps:
#   1. create the named volumes
#   2. seed volumes via a throwaway container + `docker cp`
#      (seed/wheels -> frogcache-files, seed/artifacts.json -> frogcache-artifacts)
#   3. idempotent ntfy publisher setup (user frogcache, topic frogcache-updates,
#      token) and write the token into the frogcache-secrets volume (mode 600)
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
#          NTFY_CONTAINER ntfy container name on the host (default: ntfy)
set -euo pipefail

CONTEXT="${CONTEXT:?set CONTEXT to the docker context name for the LAN host}"
CACHE_HOST="${CACHE_HOST:?set CACHE_HOST to the LAN host/IP the cache is served on}"
UPDATER_IMAGE="${UPDATER_IMAGE:?set UPDATER_IMAGE, e.g. <registry>/frogcache/updater:latest}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
D=(docker --context "$CONTEXT")
CACHE_URL="http://${CACHE_HOST}:8091/"
NTFY_TOPIC_URL="${NTFY_TOPIC_URL:-http://${CACHE_HOST}:8090/frogcache-updates}"
NTFY_CONTAINER="${NTFY_CONTAINER:-ntfy}"

VOLUMES=(frogcache-files frogcache-artifacts frogcache-data frogcache-secrets)

create_volumes() {
    for vol in "${VOLUMES[@]}"; do
        "${D[@]}" volume create "$vol" >/dev/null
    done
}

# seed_volume <volume> <local-dir> <container-path> — copy a local dir into a
# named volume using a throwaway container (no ssh, no host paths).
seed_volume() {
    local vol="$1" src="$2" dest="$3" tmp
    tmp="frogcache-seed-$$"
    "${D[@]}" create --name "$tmp" -v "$vol:$dest" alpine true >/dev/null
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
    "${D[@]}" exec "$NTFY_CONTAINER" ntfy access frogcache frogcache-updates write-only >/dev/null
    local token
    token="$("${D[@]}" exec "$NTFY_CONTAINER" ntfy token add frogcache | grep -o 'tk_[A-Za-z0-9]*' | head -1)"
    if [[ -z "$token" ]]; then
        echo "ERROR: could not obtain ntfy token for user frogcache" >&2
        exit 1
    fi
    printf 'NTFY_URL=%s\nNTFY_TOKEN=%s\n' "$NTFY_TOPIC_URL" "$token" \
        | "${D[@]}" run --rm -i -v frogcache-secrets:/secrets alpine \
            sh -c 'cat > /secrets/frogcache.env && chmod 600 /secrets/frogcache.env'
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

main() {
    create_volumes
    seed_volume frogcache-files "$ROOT/seed/wheels" /cache
    seed_volume frogcache-artifacts "$ROOT/seed/artifacts.json" /artifacts
    setup_ntfy_publisher
    ensure_image
    UPDATER_IMAGE="$UPDATER_IMAGE" "${D[@]}" compose -f "$HERE/docker-compose.yml" up -d
    verify
    echo "OK: frogcache deployed on $CONTEXT ($CACHE_URL)"
}

main "$@"
