"""Pull-through fetch server (ADR-003 §A): nginx 404s proxy here; on a miss we infer
the source from the path shape only, fetch+verify+promote the EXACT requested file
via the staged flow, append the spec to artifacts.json (auto: true) only after a
successful promote, and serve the file.

Never fetches the raw request path as a URL — URLs are constructed only against
known hosts, and request paths are containment-checked against the cache root.
Binds loopback only; LAN exposure is the interim model (jgf-auth tokens later).

Governing ADR: docs/decisions/ADR-003 (§A).
"""

import fcntl
import json
import urllib.parse
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from frogcache import audit, cache, db, pipeline
from frogcache.data_models.artifact import ArtifactSpec, SourceSpec
from frogcache.data_models.config import Config
from frogcache.notify import notify_safe

FETCH_PORT = 8092

_SPEC_LOCK = threading.Lock()  # process-local; cross-container safety comes from the
# flock on <artifacts share>/.lock taken around the whole read/modify/write below
# (deploy.sh takes the same lock during redeploys).


def infer_spec_from_path(path: str) -> ArtifactSpec | None:
    """Infer the weekly-tracking artifact spec from a requested path shape.

    - '<name>-<version>-...whl' -> pypi source (PEP 427 parse)
    - 'hf/<org>/<model>/...'    -> huggingface source (repo=org/model)
    - anything else             -> None (404, no guessing)

    Args:
        path: URL path without leading slash.
    Returns:
        ArtifactSpec or None.
    """
    if path.startswith("hf/"):
        parts = path.split("/")
        if len(parts) >= 3 and parts[1] and parts[2]:
            return ArtifactSpec(
                name=f"{parts[1]}/{parts[2]}",
                source=SourceSpec(type="huggingface", repo=f"{parts[1]}/{parts[2]}"),
            )
        return None
    parsed = cache.parse_wheel_filename(path)
    if parsed:
        name, _version = parsed
        return ArtifactSpec(name=cache.normalize_name(name), source=SourceSpec(type="pypi"))
    return None


def append_auto_spec(config: Config, spec: ArtifactSpec, conn) -> bool:
    """Append the spec to artifacts.json with auto:true if not already managed.

    Caller must hold _SPEC_LOCK. Uses a unique temp file + atomic rename.

    Returns:
        True if appended, False if already present.
    """
    path = Path(config.artifacts_file)
    lock_path = path.parent / ".lock"
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)  # shared with deploy.sh redeploys
        raw = json.loads(path.read_text())
        artifacts = raw.setdefault("artifacts", [])
        if any(a.get("name") == spec.name for a in artifacts):
            return False
        artifacts.append({"name": spec.name, "auto": True, "source": spec.source.__dict__})
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(raw, indent=2))
        tmp.replace(path)
    audit.log_event(conn, "auto_add", spec.name, source=spec.source.type)
    return True


class _Handler(BaseHTTPRequestHandler):
    """GET handler: serve from cache (containment-checked), or fetch exact file on a miss."""

    config: Config

    def _safe_target(self, path: str) -> Path | None:
        """Resolve path inside the cache root via safe_join; None if it escapes."""
        try:
            return cache.safe_join(self.config.cache_dir, path)
        except RuntimeError:
            return None

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path).lstrip("/")
        target = self._safe_target(path)
        if target is None:
            self.send_error(404)
            return
        if not target.is_file():
            spec = infer_spec_from_path(path)
            if spec is None:
                self.send_error(404)
                return
            conn = db.connect(self.config.database_url)
            try:
                # fetch the EXACT requested file, not the latest release
                promoted = pipeline.fetch_exact_file(self.config, path, spec, conn)
            except (RuntimeError, ValueError) as err:
                audit.log_event(conn, "verify_fail", spec.name, error=str(err)[:200])
                notify_safe(self.config, "frogcache: pull-through FAILED",
                            f"{path}: {err}", priority="high")
                self.send_error(502, "fetch failed")
                return
            if promoted is None:
                self.send_error(404)
                return
            with _SPEC_LOCK:
                append_auto_spec(self.config, spec, conn)  # only after successful promote
            # serve the PROMOTED path (HF mutable revisions resolve to a sha dir,
            # which differs from the requested path) after a containment check
            cache_root = Path(self.config.cache_dir).resolve()
            target = promoted.resolve()
            if cache_root not in target.parents or not target.is_file():
                self.send_error(404)
                return
        self.send_response(200)
        self.send_header("Content-Length", str(target.stat().st_size))
        self.end_headers()
        with open(target, "rb") as fh:
            import shutil

            shutil.copyfileobj(fh, self.wfile)

    def log_message(self, fmt, *args) -> None:  # keep stdout clean; audit DB has the events
        return


def serve(config: Config, port: int = FETCH_PORT) -> None:
    """Run the pull-through fetch server on loopback (ADR-003 §A).

    Args:
        config: runtime config.
        port: loopback port (nginx proxies 404s here).
    """
    handler = type("Handler", (_Handler,), {"config": config})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"frogcache fetch-server on 127.0.0.1:{port}", flush=True)
    server.serve_forever()
