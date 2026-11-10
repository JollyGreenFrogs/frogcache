"""Docker image source (ADR-003 §B): pull from an upstream registry, push into the private registry.

Version = upstream manifest digest. Weekly check compares digests; on change
the image (manifest + blobs) is pulled through the sandbox (verified like any
download, ADR-003 §D), pushed to `<REGISTRY_ADDR>/frogcache/<image>:<tag>`, and
retention keeps the tag plus the newest `<tag>-old-*` history tag (latest + latest-1).

Registry credentials (REGISTRY_USER/REGISTRY_PASS) come from the secrets
file, never committed. Governing ADR: docs/decisions/ADR-003 (§B).
"""

import base64
import hashlib
import http.client
import json
import urllib.parse
from datetime import datetime, timezone
from frogcache import net
from pathlib import Path

from frogcache import audit, manifest, net, verify


def _extra_hosts(config) -> set[str]:
    """The configured private registry host is allowed over http on the LAN."""
    if not getattr(config, "registry_addr", ""):
        return set()
    return {urllib.parse.urlparse(config.registry_addr).hostname or ""}
from frogcache.data_models.artifact import ArtifactSpec, ManifestEntry, UpstreamRelease
from frogcache.data_models.config import Config

MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.oci.image.index.v1+json",
    ]
)
DOCKER_AUTH = "https://auth.docker.io/token?service=registry.docker.io&scope=repository:{image}:pull"


_request = net.request


def _upstream_token(registry: str, image: str) -> str:
    """Anonymous pull token for the upstream registry (docker.io only; others: none)."""
    if "docker.io" not in registry:
        return ""
    status, _, body = _request(DOCKER_AUTH.format(image=image))
    if status != 200:
        raise RuntimeError(f"token request failed: HTTP {status}")
    return json.loads(body)["token"]


def latest_docker_release(spec: ArtifactSpec, timeout: int = 60) -> UpstreamRelease | None:
    """Resolve the current manifest digest of the image:tag upstream.

    Returns:
        UpstreamRelease with version = manifest digest, sha256 = digest, or None on 404.
    """
    source = spec.source
    token = _upstream_token(source.upstream_registry, source.image)
    headers = {"Accept": MANIFEST_ACCEPT, "User-Agent": "frogcache/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{source.upstream_registry.rstrip('/')}/v2/{source.image}/manifests/{source.tag}"
    status, resp_headers, body = _request(url, headers=headers, timeout=timeout)
    if status == 404:
        return None
    if status != 200:
        raise RuntimeError(f"GET manifest {source.image}:{source.tag} -> HTTP {status}")
    digest = resp_headers.get("Docker-Content-Digest", "")
    return UpstreamRelease(version=digest or source.tag, url=url, filename="manifest.json",
                           sha256=digest)


def _target_auth(config: Config) -> str:
    """Basic auth header for the private registry, from Config (secrets file)."""
    if not config.registry_user:
        raise RuntimeError("REGISTRY_USER/REGISTRY_PASS not set in secrets; cannot push")
    return "Basic " + base64.b64encode(
        f"{config.registry_user}:{config.registry_pass}".encode()
    ).decode()


def _target_repo(config: Config, spec: ArtifactSpec) -> tuple[str, str]:
    """(target base URL, repo path) for the private registry push."""
    if not config.registry_addr:
        raise RuntimeError("REGISTRY_ADDR not set in secrets; where is the private registry?")
    return config.registry_addr.rstrip("/"), f"frogcache/{spec.source.image}"


def check_docker_artifact(config: Config, spec: ArtifactSpec, conn) -> str:
    """Check one docker image: if the upstream digest changed, pull via sandbox + push to the private registry.

    Returns:
        Report line. Raises RuntimeError on failure.
    """
    release = latest_docker_release(spec, config.http_timeout)
    if release is None:
        return f"{spec.name}: no upstream image found"
    versions = manifest.versions(conn, spec.name)
    if versions and versions[0] == release.version:
        return f"{spec.name}: up to date ({release.version[:19]})"
    audit.log_event(conn, "download_start", spec.name, release.version, image=spec.source.image)
    _pull_push_image(config, spec, release, conn)
    manifest.record_fetch(
        conn, spec.name,
        ManifestEntry(version=release.version, filename=f"docker/{spec.source.image}:{spec.source.tag}",
                      sha256=release.sha256, source="docker", url=release.url,
                      fetched_at=datetime.now(timezone.utc).isoformat(),
                      digest_verified=True),
        config.retain,
    )
    _prune_image_tags(config, spec, keep_latest=config.retain)
    audit.log_event(conn, "promote", spec.name, release.version, image=spec.source.image)
    before = versions[0][:19] if versions else "none"
    return f"{spec.name}: updated {before} -> {release.version[:19]} (pushed to private registry)"


def _pull_push_image(config: Config, spec: ArtifactSpec, release: UpstreamRelease, conn) -> None:
    """Pull manifest+blobs via the sandbox (verified), push to the private registry."""
    source = spec.source
    token = _upstream_token(source.upstream_registry, source.image)
    headers = {"Accept": MANIFEST_ACCEPT, "User-Agent": "frogcache/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    digest_url = f"{source.upstream_registry.rstrip('/')}/v2/{source.image}/manifests/{release.version}"
    status, _, body = _request(digest_url, headers=headers, timeout=config.http_timeout)
    if status != 200:
        raise RuntimeError(f"pull manifest by digest -> HTTP {status}")
    if hashlib.sha256(body).hexdigest() != release.version.split(":")[-1]:
        raise RuntimeError("manifest bytes do not match the resolved digest")
    doc = json.loads(body)
    base, repo = _target_repo(config, spec)
    auth = _target_auth(config)
    _copy_descriptors(config, spec, release, doc, headers, base, repo, auth, conn)
    # push manifest under the tag; keep the previous digest under <tag>-old-<short>
    status, resp, _ = _request(f"{base}/v2/{repo}/manifests/{source.tag}", headers={"Accept": MANIFEST_ACCEPT, "Authorization": auth}, timeout=config.http_timeout)
    if status == 200:
        old_digest = resp.get("Docker-Content-Digest", "")
        if old_digest and old_digest != release.version:
            st, _, old_body = _request(f"{base}/v2/{repo}/manifests/{old_digest}", headers={"Accept": MANIFEST_ACCEPT, "Authorization": auth}, timeout=config.http_timeout)
            if st != 200:
                raise RuntimeError(f"read rollback manifest -> HTTP {st}")
            hist_tag = f"{source.tag}-old-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{old_digest.split(':')[-1][:12]}"
            st, _, _ = _request(f"{base}/v2/{repo}/manifests/{hist_tag}", method="PUT",
                               headers={"Authorization": auth, "Content-Type": resp.get("Content-Type", MANIFEST_ACCEPT.split(",")[0])},
                               data=old_body, timeout=config.http_timeout)
            if st not in (200, 201):
                raise RuntimeError(f"write rollback manifest -> HTTP {st}")
    status, _, _ = _request(f"{base}/v2/{repo}/manifests/{source.tag}", method="PUT",
                           headers={"Authorization": auth, "Content-Type": doc.get("mediaType", "application/vnd.docker.distribution.manifest.v2+json")},
                           data=body, timeout=config.http_timeout)
    if status not in (200, 201):
        raise RuntimeError(f"push manifest -> HTTP {status}")


_stream_to = net.stream_to


def _push_blob(base: str, repo: str, digest: str, file: Path, auth: str, timeout: int) -> None:
    """Upload one verified blob to the private registry (PUT, then monolithic upload)."""
    status, headers, _ = _request(f"{base}/v2/{repo}/blobs/uploads/", method="POST", headers={"Authorization": auth}, timeout=timeout)
    if status != 202:
        raise RuntimeError(f"blob upload init -> HTTP {status}")
    location = headers.get("Location", "")  # carries the upload URL
    sep = "&" if "?" in location else "?"
    status, _, _ = _request(f"{location}{sep}digest={digest}", method="PUT",
                           headers={"Authorization": auth, "Content-Type": "application/octet-stream"},
                           data=file.read_bytes(), timeout=timeout)
    if status not in (200, 201, 202):
        raise RuntimeError(f"blob upload -> HTTP {status}")


def _prune_image_tags(config: Config, spec: ArtifactSpec, keep_latest: int) -> None:
    """Prune this spec's managed history tags, keeping the newest `keep_latest`.

    Only tags named f"{tag}-old-*" (this spec's own history markers) are
    considered — other specs' tags sharing the repo (nginx:latest vs
    nginx:stable) are never touched, and a digest still referenced by a kept
    tag is never deleted.
    """
    base, repo = _target_repo(config, spec)
    auth = _target_auth(config)
    prefix = f"{spec.source.tag}-old-"
    status, _, body = _request(f"{base}/v2/{repo}/tags/list", headers={"Authorization": auth}, timeout=config.http_timeout)
    if status != 200:
        return
    tags = json.loads(body).get("tags", [])
    # history tag names embed a UTC timestamp -> lexical sort IS chronological
    history = sorted(t for t in tags if t.startswith(prefix))
    keep_tags = {spec.source.tag, *history[-(keep_latest - 1):]} if keep_latest > 1 else {spec.source.tag}
    # digests referenced by ANY tag that will remain (not just kept ones)
    referenced = set()
    for tag in tags:
        if tag in history and tag not in keep_tags:
            continue
        _, headers, _ = _request(f"{base}/v2/{repo}/manifests/{tag}", headers={"Authorization": auth, "Accept": MANIFEST_ACCEPT}, timeout=config.http_timeout)
        referenced.add(headers.get("Docker-Content-Digest", tag))
    for tag in history:
        if tag in keep_tags:
            continue
        _, headers, _ = _request(f"{base}/v2/{repo}/manifests/{tag}", headers={"Authorization": auth, "Accept": MANIFEST_ACCEPT}, timeout=config.http_timeout)
        digest = headers.get("Docker-Content-Digest", tag)
        if digest in referenced:
            continue
        _request(f"{base}/v2/{repo}/manifests/{digest}", method="DELETE", headers={"Authorization": auth}, timeout=config.http_timeout)


def _copy_descriptors(config, spec, release, doc, headers, base, repo, auth, conn, depth=0) -> None:
    """Recursively copy config + layer blobs and child manifests (indexes), verifying each."""
    if depth > 4:
        raise RuntimeError("manifest descriptor graph too deep")
    media = doc.get("mediaType", "")
    if "manifest.list" in media or "image.index" in media:
        for child in doc.get("manifests", []):
            child_digest = child["digest"]
            url = f"{spec.source.upstream_registry.rstrip('/')}/v2/{spec.source.image}/manifests/{child_digest}"
            status, _, cbody = _request(url, headers=headers, timeout=config.http_timeout)
            if status != 200 or hashlib.sha256(cbody).hexdigest() != child_digest.split(":")[-1]:
                raise RuntimeError(f"child manifest {child_digest[:19]} failed verification")
            _copy_descriptors(config, spec, release, json.loads(cbody), headers, base, repo, auth, conn, depth + 1)
            _push_manifest_bytes(base, repo, child_digest, cbody, auth, config.http_timeout, content_type=child.get("mediaType", "application/vnd.docker.distribution.manifest.v2+json"))
        return
    descriptors = [doc.get("config", {})] + doc.get("layers", [])
    for desc in descriptors:
        digest = desc.get("digest", "")
        if not digest:
            continue
        blob_url = f"{spec.source.upstream_registry.rstrip('/')}/v2/{spec.source.image}/blobs/{digest}"
        staged = Path(config.sandbox_dir) / digest.replace(":", "_")
        part = staged.with_suffix(".part")
        net.stream_to(blob_url, part, headers, config.http_timeout)
        ok, _, verified = verify.verify_and_promote(
            part, staged, expected_sha256=digest.split(":")[-1],
            quarantine=Path(config.quarantine_dir), digest_required=True,
            root=config.sandbox_dir,
        )
        if not ok:
            raise RuntimeError(f"blob {digest[:19]} failed verification; quarantined")
        _push_blob_stream(base, repo, digest, staged, auth, config.http_timeout)
        staged.unlink(missing_ok=True)  # sandbox stays temporary — no blob accumulation
        audit.log_event(conn, "verify_pass", spec.name, release.version, file=digest[:19], digest_verified=verified)


def _push_blob_stream(base: str, repo: str, digest: str, file: Path, auth: str, timeout: int) -> None:
    """Upload one verified blob to the private registry, STREAMING the file (no full RAM read)."""
    parsed = urllib.parse.urlparse(base)
    conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        conn = conn_cls(host, port, timeout=timeout)
        conn.request("POST", f"{parsed.path}/v2/{repo}/blobs/uploads/", headers={"Authorization": auth})
        resp = conn.getresponse()
        resp.read()
    except (OSError, http.client.HTTPException) as err:
        raise RuntimeError(f"registry connection failed: {err}") from err
    if resp.status != 202:
        raise RuntimeError(f"blob upload init -> HTTP {resp.status}")
    location = resp.getheader("Location", "")
    upload_url = urllib.parse.urlparse(location if location.startswith("http") else f"{base}{location}")
    # preserve the upload session query state (_state=...) and add the digest
    query = urllib.parse.parse_qsl(upload_url.query, keep_blank_values=True)
    query.append(("digest", digest))
    upload_url = upload_url._replace(query=urllib.parse.urlencode(query))
    conn.close()
    conn = conn_cls(upload_url.hostname, upload_url.port or port, timeout=timeout)
    size = file.stat().st_size
    with open(file, "rb") as fh:
        conn.request(
            "PUT",
            f"{upload_url.path}?{upload_url.query}",
            body=fh,
            headers={"Authorization": auth, "Content-Type": "application/octet-stream",
                     "Content-Length": str(size)},
        )
        resp = conn.getresponse()
        resp.read()
    conn.close()
    if resp.status not in (200, 201, 202):
        raise RuntimeError(f"blob upload -> HTTP {resp.status}")


def _push_manifest_bytes(base: str, repo: str, ref: str, body: bytes, auth: str, timeout: int,
                         content_type: str = "application/vnd.docker.distribution.manifest.v2+json") -> None:
    """PUT a manifest (by digest or tag) to the private registry, preserving its media type."""
    status, _, _ = _request(
        f"{base}/v2/{repo}/manifests/{ref}", method="PUT",
        headers={"Authorization": auth, "Content-Type": content_type},
        data=body, timeout=timeout,
    )
    if status not in (200, 201):
        raise RuntimeError(f"push manifest {ref[:19]} -> HTTP {status}")
