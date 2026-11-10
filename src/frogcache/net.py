"""Raw HTTP helpers that need status/headers (fetch_json discards them).

Used by docker_source (registry v2 needs headers) and hf (Link pagination).
Governing ADR: ADR-003 §B.
"""

import json
import urllib.error
import urllib.request
from pathlib import Path


def request(url: str, method: str = "GET", headers: dict | None = None,
            data: bytes | None = None, timeout: int = 60):
    """One HTTP request; returns (status, headers, body). Never raises on HTTP errors.

    Raises:
        RuntimeError: on network failure.
    """
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers or {}), err.read()
    except urllib.error.URLError as err:
        raise RuntimeError(f"{method} {url} failed: {err.reason}") from err


def stream_to(url: str, dest: Path, headers: dict, timeout: int) -> None:
    """Stream a URL body to dest. Raises RuntimeError on failure (dest removed)."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response, open(dest, "wb") as out:
            while chunk := response.read(1 << 20):
                out.write(chunk)
    except (urllib.error.URLError, OSError) as err:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"GET {url} failed: {err}") from err


USER_AGENT = "frogcache/1.0"


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
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as err:
        raise RuntimeError(f"GET {url} -> HTTP {err.code}") from err
    except urllib.error.URLError as err:
        raise RuntimeError(f"GET {url} failed: {err.reason}") from err
