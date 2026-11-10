"""Upstream source resolvers: turn an ArtifactSpec into an UpstreamRelease.

Split from upstream.py per the one-concern / <200-line house rules.
Governing ADR: docs/decisions/ADR-001 (sources), ADR-002 (HF via hf.py).
"""

import re

from frogcache.data_models.artifact import ArtifactSpec, UpstreamRelease
from frogcache.net import fetch_json
from frogcache.versions import is_newer, parse_version

PYPI_JSON_URL = "https://pypi.org/pypi/{name}/json"
GITHUB_COMPAT_URL = "https://raw.githubusercontent.com/{repo}/{branch}/compatibility.json"
GITHUB_RELEASES_URL = "https://api.github.com/repos/{repo}/releases?per_page=100&page={page}"
GITHUB_RELEASE_BY_TAG_URL = "https://api.github.com/repos/{repo}/releases/tags/{tag}"
GITHUB_DOWNLOAD_URL = "https://github.com/{repo}/releases/download/{tag}/{filename}"
GITHUB_MAX_PAGES = 5
USER_AGENT = "frogcache/1.0"


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
    return UpstreamRelease(
        version=version,
        url=best["url"],
        filename=best["filename"],
        sha256=best.get("digests", {}).get("sha256", ""),
    )


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
    digest = asset.get("digest", "")  # 'sha256:...' when the API provides it
    if digest.startswith("sha256:"):
        digest = digest[len("sha256:"):]
    return UpstreamRelease(
        version=best, url=asset["browser_download_url"], filename=filename, sha256=digest
    )


