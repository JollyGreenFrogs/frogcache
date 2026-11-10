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


def request(url: str, method: str = "GET", headers: dict | None = None,
            data: bytes | None = None, timeout: int = 60):
    """One HTTP request; returns (status, headers, body). Never raises on HTTP errors.

    Raises:
        RuntimeError: on network failure.
    """
    req = urllib.request.Request(safe_url(url), data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers or {}), err.read()
    except urllib.error.URLError as err:
        raise RuntimeError(f"{method} {url} failed: {err.reason}") from err


def stream_to(url: str, dest: Path, headers: dict, timeout: int) -> None:
    """Stream a URL body to dest. Raises RuntimeError on failure (dest removed)."""
    req = urllib.request.Request(safe_url(url), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response, open(dest, "wb") as out:
            while chunk := response.read(1 << 20):
                out.write(chunk)
    except (urllib.error.URLError, OSError) as err:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"GET {url} failed: {err}") from err


USER_AGENT = "frogcache/1.0"

# Only these hosts are ever fetched (plus the configured private registry).
ALLOWED_HOSTS = {
    "pypi.org",
    "raw.githubusercontent.com",
    "api.github.com",
    "github.com",
    "huggingface.co",
    "auth.docker.io",
    "index.docker.io",
}


def _configured_registry_host() -> str | None:
    """Host of the private registry from REGISTRY_ADDR (secrets file), if set."""
    addr = os.environ.get("REGISTRY_ADDR", "")
    return urllib.parse.urlparse(addr).hostname if addr else None


def safe_url(url: str, extra_hosts: set[str] | None = None) -> str:
    """Validate and rebuild a URL: https only, host allowlist, re-encoded path.

    Args:
        url: candidate URL.
        extra_hosts: additional allowed hosts (e.g. the private registry host).
    Returns:
        Rebuilt URL with scheme+host from the allowlist and an encoded path.
    Raises:
        RuntimeError: if the scheme or host is not allowed.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "file":
        return url  # local test fixtures only
    allowed = ALLOWED_HOSTS | (extra_hosts or set())
    reg_host = _configured_registry_host()
    if reg_host:
        allowed.add(reg_host)
    if parsed.hostname not in allowed or parsed.scheme not in ("https", "http"):
        raise RuntimeError(f"refusing non-allowlisted URL host: {parsed.hostname!r}")
    if parsed.scheme == "http" and parsed.hostname not in (extra_hosts or set()):
        raise RuntimeError(f"http only allowed for the private registry, not {parsed.hostname!r}")
    path = urllib.parse.quote(parsed.path, safe="/%")
    return urllib.parse.urlunparse(("https", parsed.netloc, path, "", parsed.query, ""))



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
        with urllib.request.urlopen(urllib.request.Request(safe_url(url), headers=headers), timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as err:
        raise RuntimeError(f"GET {url} -> HTTP {err.code}") from err
    except urllib.error.URLError as err:
        raise RuntimeError(f"GET {url} failed: {err.reason}") from err
