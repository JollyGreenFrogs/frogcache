"""Raw HTTP helpers that need status/headers (fetch_json discards them).

Used by docker_source (registry v2 needs headers) and hf (Link pagination).
Governing ADR: ADR-003 §B.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect target through safe_url — an allowlisted host
    must not be able to redirect us to a non-allowlisted one — and strip the
    Authorization header when the redirect crosses to a different host, so
    bearer tokens are never leaked cross-host (urllib forwards them by default)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        # redirects must stay http(s) — safe_url alone would let file:// through
        # (it is allowed for local test fixtures in direct calls)
        if parsed.scheme not in ("https", "http"):
            raise RuntimeError(f"refusing redirect to non-http scheme: {parsed.scheme!r}")
        safe_url(newurl)  # raises RuntimeError on non-allowlisted host
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None:
            old = urllib.parse.urlparse(req.full_url)
            same_origin = (
                parsed.scheme == (old.scheme or "https")
                and (parsed.hostname or "").lower() == (old.hostname or "").lower()
                and (parsed.port or (443 if parsed.scheme == "https" else 80))
                == (old.port or (443 if (old.scheme or "https") == "https" else 80))
            )
            if not same_origin:
                new_req.remove_header("Authorization")
        return new_req


SAFE_OPENER = urllib.request.build_opener(_SafeRedirectHandler)


def request(url: str, method: str = "GET", headers: dict | None = None,
            data: bytes | None = None, timeout: int = 60):
    """One HTTP request; returns (status, headers, body). Never raises on HTTP errors.

    Raises:
        RuntimeError: on network failure or a redirect to a non-allowlisted host.
    """
    req = urllib.request.Request(safe_url(url), data=data, method=method, headers=headers or {})
    try:
        with SAFE_OPENER.open(req, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers or {}), err.read()
    except urllib.error.URLError as err:
        raise RuntimeError(f"{method} {redact_url(url)} failed: {err.reason}") from err


def stream_to(url: str, dest: Path, headers: dict, timeout: int) -> None:
    """Stream a URL body to dest. Raises RuntimeError on failure (dest removed)."""
    req = urllib.request.Request(safe_url(url), headers=headers)
    try:
        with SAFE_OPENER.open(req, timeout=timeout) as response, open(dest, "wb") as out:
            while chunk := response.read(1 << 20):
                out.write(chunk)
    except (urllib.error.URLError, OSError) as err:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"GET {redact_url(url)} failed: {err}") from err


USER_AGENT = "frogcache/1.0"

# Only these hosts are ever fetched (plus the configured private registry).
ALLOWED_HOSTS = {
    "pypi.org",
    "files.pythonhosted.org",
    "raw.githubusercontent.com",
    "api.github.com",
    "github.com",
    # GitHub release-asset CDN redirect targets
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
    "codeload.github.com",
    "huggingface.co",
    # HF LFS/resolve redirect targets for model weight downloads
    "cas-bridge.xethub.hf.co",
    "cdn-lfs.hf.co",
    "transfer.xethub.hf.co",
    "auth.docker.io",
    "index.docker.io",
}


_EXTRA_HOSTS: set[str] = set()


def register_extra_host(host: str | None) -> None:
    """Allow an extra host (the private registry), typically from Config."""
    if host:
        _EXTRA_HOSTS.add(host)


_REGISTRY_HOST: str | None = None
_HTTP_OK_HOSTS: set[str] = set()  # LAN hosts allowed plain http (registry, ntfy)


def set_registry_host(host: str | None) -> None:
    """Track the PRIVATE registry host separately from the general allowlist.

    The private registry is allowed plain http (LAN); upstream registries
    registered via register_extra_host stay https-only.
    """
    global _REGISTRY_HOST
    if host:
        _REGISTRY_HOST = host
        _HTTP_OK_HOSTS.add(host)


def register_http_host(host: str | None) -> None:
    """Allow a LAN service host (e.g. ntfy) over plain http AND in the allowlist."""
    if host:
        _HTTP_OK_HOSTS.add(host)
        _EXTRA_HOSTS.add(host)


def _configured_registry_host() -> str | None:
    """Private registry host: set via set_registry_host (Config), else REGISTRY_ADDR env."""
    if _REGISTRY_HOST:
        return _REGISTRY_HOST
    addr = os.environ.get("REGISTRY_ADDR", "")
    return urllib.parse.urlparse(addr).hostname if addr else None


def safe_url(url: str, extra_hosts: set[str] | None = None, allow_file: bool = False) -> str:
    """Validate and rebuild a URL: https only, host allowlist, re-encoded path.

    Args:
        url: candidate URL.
        extra_hosts: additional allowed hosts (e.g. the private registry host).
        allow_file: permit file:// URLs — LOCAL TEST FIXTURES ONLY; production
            callers must leave this False so a file:// URL can never be fetched.
    Returns:
        Rebuilt URL with scheme+host from the allowlist and an encoded path.
    Raises:
        RuntimeError: if the scheme or host is not allowed.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "file":
        if not allow_file:
            raise RuntimeError("file:// URLs are not allowed")
        return url  # local test fixtures only
    # allowlist membership is checked against the COMPLETE registered set —
    # several extra hosts (private registry + multiple upstream registries) may
    # be registered at once
    allowed = ALLOWED_HOSTS | _EXTRA_HOSTS | (extra_hosts or set())
    if parsed.hostname not in allowed or parsed.scheme not in ("https", "http"):
        raise RuntimeError(f"refusing non-allowlisted URL host: {parsed.hostname!r}")
    # plain http is allowed ONLY for registered LAN hosts (private registry,
    # ntfy) — tracked separately, not for arbitrary registered hosts
    is_lan = parsed.hostname in _HTTP_OK_HOSTS
    if parsed.scheme == "http" and not is_lan:
        raise RuntimeError(f"http only allowed for LAN services (registry/ntfy), not {parsed.hostname!r}")
    scheme = parsed.scheme if is_lan else "https"  # LAN services may be plain http
    path = urllib.parse.quote(parsed.path, safe="/%")
    return urllib.parse.urlunparse((scheme, parsed.netloc, path, "", parsed.query, ""))



def checked_url(url: str, allow_file: bool = False) -> str:
    """safe_url plus an INLINE hostname allowlist check at the point of use.

    safe_url already enforces the allowlist; the inline re-check makes the
    sanitizer visible to static analysis at the request sink. file:// is only
    permitted when allow_file=True (local test fixtures).
    """
    safe = safe_url(url, allow_file=allow_file)
    parsed = urllib.parse.urlparse(safe)
    if parsed.scheme == "file":
        return safe  # reachable only with allow_file=True (local test fixtures)
    if parsed.hostname not in (ALLOWED_HOSTS | _EXTRA_HOSTS):
        raise RuntimeError(f"refusing non-allowlisted URL host: {safe!r}")
    return safe


def redact_url(url: str) -> str:
    """Strip query/fragment from a URL for logs, events, notifications.

    Signed CDN URLs carry credentials in the query string — they must never
    land in the audit DB, ntfy messages, or error text.
    """
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def fetch_json(url: str, token: str = "", timeout: int = 60) -> object:
    """GET a URL and return the decoded JSON body.

    Args:
        url: absolute http(s) URL.
        token: optional bearer token (raises api.github.com rate limits).
        timeout: request timeout in seconds.
    Returns:
        Decoded JSON value (dict or list).
    Raises:
        RuntimeError: on HTTP error (including 404) or network failure.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with SAFE_OPENER.open(urllib.request.Request(checked_url(url), headers=headers), timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as err:
        raise RuntimeError(f"GET {redact_url(url)} -> HTTP {err.code}") from err
    except urllib.error.URLError as err:
        raise RuntimeError(f"GET {url} failed: {err.reason}") from err


def header(headers: dict, name: str) -> str:
    """Case-insensitive header lookup (registry responses vary in case)."""
    lname = name.lower()
    for k, v in headers.items():
        if k.lower() == lname:
            return v
    return ""
