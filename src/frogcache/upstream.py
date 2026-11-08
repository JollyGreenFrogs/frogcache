"""Upstream sources: resolve the latest release (version + download URL) per artifact.

Sources (SourceSpec.type):
  - pypi:            PyPI JSON API; wheel file URL taken from the release files.
  - github-compat:   repo's compatibility.json on raw.githubusercontent.com (not
                     rate-limited like api.github.com); download URL constructed
                     from the release tag '<underscore_name>-<version>'.
  - github-releases: api.github.com release tags/assets (optional GITHUB_TOKEN).
  - huggingface:     HF model repo files at a revision (see hf.py, ADR-002).

Governing ADR: docs/decisions/ADR-001.
"""

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

from frogcache.data_models.artifact import ArtifactSpec, SourceSpec, UpstreamRelease
from frogcache.versions import is_newer, parse_version

PYPI_JSON_URL = "https://pypi.org/pypi/{name}/json"
GITHUB_COMPAT_URL = "https://raw.githubusercontent.com/{repo}/{branch}/compatibility.json"
GITHUB_RELEASES_URL = "https://api.github.com/repos/{repo}/releases?per_page=100&page={page}"
GITHUB_RELEASE_BY_TAG_URL = "https://api.github.com/repos/{repo}/releases/tags/{tag}"
GITHUB_DOWNLOAD_URL = "https://github.com/{repo}/releases/download/{tag}/{filename}"
GITHUB_MAX_PAGES = 5
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
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as err:
        raise RuntimeError(f"GET {url} -> HTTP {err.code}") from err
    except urllib.error.URLError as err:
        raise RuntimeError(f"GET {url} failed: {err.reason}") from err


def load_artifacts(path: str) -> list[ArtifactSpec]:
    """Load and validate artifact specs from a JSON file.

    Args:
        path: artifacts.json with {"artifacts": [{name, source: {...}}]}.
    Returns:
        List of validated ArtifactSpec.
    Raises:
        ValueError: on malformed JSON or invalid specs.
    """
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as err:
        raise ValueError(f"cannot read artifacts file {path}: {err}") from err
    specs = []
    for entry in raw.get("artifacts", []):
        source = SourceSpec(
            type=entry["source"]["type"],
            repo=entry["source"].get("repo", ""),
            branch=entry["source"].get("branch", "master"),
            file_pattern=entry["source"].get("file_pattern", ""),
            revision=entry["source"].get("revision", "main"),
            files=tuple(entry["source"].get("files", [])),
        )
        source.validate()
        specs.append(ArtifactSpec(name=entry["name"], source=source))
    return specs


def latest_pypi_release(spec: ArtifactSpec, timeout: int) -> UpstreamRelease | None:
    """Resolve the latest release of a PyPI project, preferring a wheel file.

    Args:
        spec: artifact spec (source.type == 'pypi').
        timeout: request timeout in seconds.
    Returns:
        UpstreamRelease, or None if the project is not on PyPI (HTTP 404).
    Raises:
        RuntimeError: on other HTTP/network errors.
    """
    try:
        data = fetch_json(PYPI_JSON_URL.format(name=spec.name), timeout=timeout)
    except RuntimeError as err:
        if "HTTP 404" in str(err):
            return None
        raise
    version = data["info"]["version"]
    files = data.get("releases", {}).get(version, [])
    wheels = [f for f in files if f.get("packagetype") == "bdist_wheel"] or files
    if not wheels:
        return None
    best = wheels[0]
    return UpstreamRelease(version=version, url=best["url"], filename=best["filename"])


def latest_compat_release(spec: ArtifactSpec, timeout: int) -> UpstreamRelease | None:
    """Resolve the latest release via a repo's compatibility.json (spacy-models style).

    compatibility.json shape: {"spacy": {"<spacy ver>": {"<model>": ["<ver>"]}}}.
    The download URL is constructed from the release tag
    '<underscore_name>-<version>' and source.file_pattern.

    Args:
        spec: artifact spec (source.type == 'github-compat').
        timeout: request timeout in seconds.
    Returns:
        UpstreamRelease, or None if the model is not listed.
    Raises:
        RuntimeError: if compatibility.json cannot be fetched.
    """
    source = spec.source
    data = fetch_json(
        GITHUB_COMPAT_URL.format(repo=source.repo, branch=source.branch), timeout=timeout
    )
    versions = [
        version
        for model_versions in data.get("spacy", {}).values()
        for version in model_versions.get(spec.underscore_name, [])
    ]
    if not versions:
        return None
    version = max(versions, key=parse_version)
    filename = source.file_pattern.format(version=version)
    tag = f"{spec.underscore_name}-{version}"
    url = GITHUB_DOWNLOAD_URL.format(repo=source.repo, tag=tag, filename=filename)
    return UpstreamRelease(version=version, url=url, filename=filename)


def latest_github_release(spec: ArtifactSpec, token: str, timeout: int) -> UpstreamRelease | None:
    """Resolve the latest release by scanning api.github.com release tags/assets.

    Args:
        spec: artifact spec (source.type == 'github-releases').
        token: optional GitHub token (unauthenticated limit is 60/hr/IP).
        timeout: request timeout in seconds.
    Returns:
        UpstreamRelease, or None if no matching release tag exists.
    Raises:
        RuntimeError: if the GitHub API cannot be reached.
    """
    source = spec.source
    tag_re = re.compile(rf"^{re.escape(spec.underscore_name)}-v?(?P<version>\d[\w.]*)$")
    best: str | None = None
    for page in range(1, GITHUB_MAX_PAGES + 1):
        releases = fetch_json(
            GITHUB_RELEASES_URL.format(repo=source.repo, page=page), token, timeout
        )
        if not isinstance(releases, list) or not releases:
            break
        for release in releases:
            match = tag_re.match(release.get("tag_name", ""))
            if match and (best is None or is_newer(match.group("version"), best)):
                best = match.group("version")
        if len(releases) < 100:
            break
    if best is None:
        return None
    tag = f"{spec.underscore_name}-{best}"
    release = fetch_json(
        GITHUB_RELEASE_BY_TAG_URL.format(repo=source.repo, tag=tag), token, timeout
    )
    filename = source.file_pattern.format(version=best)
    asset = next(
        (a for a in release.get("assets", []) if a.get("name") == filename),
        None,
    )
    if asset is None:
        return None
    return UpstreamRelease(version=best, url=asset["browser_download_url"], filename=filename)


def latest_release(spec: ArtifactSpec, github_token: str = "", timeout: int = 60) -> UpstreamRelease | None:
    """Dispatch to the right upstream source for an artifact spec.

    Args:
        spec: artifact spec.
        github_token: optional GitHub token for api.github.com sources.
        timeout: request timeout in seconds.
    Returns:
        UpstreamRelease or None when the artifact has no upstream release.
    Raises:
        RuntimeError: if the upstream cannot be reached.
    """
    if spec.source.type == "pypi":
        return latest_pypi_release(spec, timeout)
    if spec.source.type == "github-compat":
        return latest_compat_release(spec, timeout)
    if spec.source.type == "huggingface":
        raise ValueError("huggingface sources are multi-file: use frogcache.hf (ADR-002)")
    return latest_github_release(spec, github_token, timeout)
