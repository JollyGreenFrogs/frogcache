"""Data models for artifact specs and upstream releases (fields + validation only).

Governing ADR: docs/decisions/ADR-001.
"""

from dataclasses import dataclass, field

VALID_SOURCE_TYPES = ("pypi", "github-compat", "github-releases", "huggingface", "docker")


@dataclass(frozen=True)
class SourceSpec:
    """Where to find an artifact's upstream releases.

    Attributes:
        type: 'pypi' (PyPI JSON API), 'github-compat' (repo compatibility.json,
            download URL constructed from the release tag), 'github-releases'
            (api.github.com release tags/assets; needs GITHUB_TOKEN when
            unauthenticated rate limits are exhausted), or 'huggingface'
            (HF model repo files; see ADR-002).
        repo: 'owner/repo' for the github-* types; 'org/model' for huggingface.
        branch: branch for compatibility.json (github-compat only).
        file_pattern: filename template with '{version}', e.g.
            'en_core_web_sm-{version}-py3-none-any.whl'. Required for github-*
            types; ignored for pypi and huggingface.
        revision: HF revision to track (branch/tag/commit, default 'main');
            huggingface only.
        files: fnmatch patterns for files to cache, e.g. ('*.gguf', 'config.json');
            empty = all files in the HF repo tree. huggingface only.
        image: docker image path without registry, e.g. 'library/nginx'. docker only.
        tag: tag to track (default 'latest'). docker only.
        upstream_registry: registry v2 base URL, default docker.io. docker only.
    """

    type: str
    repo: str = ""
    branch: str = "master"
    file_pattern: str = ""
    revision: str = "main"
    files: tuple[str, ...] = ()
    image: str = ""
    tag: str = "latest"
    upstream_registry: str = "https://index.docker.io"

    def validate(self) -> None:
        """Raise ValueError if the spec is incomplete for its type."""
        if self.type not in VALID_SOURCE_TYPES:
            raise ValueError(f"unknown source type: {self.type!r}")
        if self.type.startswith("github") and (not self.repo or not self.file_pattern):
            raise ValueError(f"source {self.type!r} needs repo and file_pattern")
        if self.type == "huggingface" and not self.repo:
            raise ValueError("source 'huggingface' needs repo ('org/model')")
        if self.type == "docker" and not self.image:
            raise ValueError("source 'docker' needs image (e.g. 'library/nginx')")


@dataclass(frozen=True)
class ArtifactSpec:
    """One cached artifact and its upstream source.

    Attributes:
        name: normalized project name, e.g. 'en-core-web-sm'.
        source: SourceSpec describing the upstream.
    """

    name: str
    source: SourceSpec

    @property
    def underscore_name(self) -> str:
        """Name with dashes/underscores collapsed to underscores (wheel/tag form)."""
        return self.name.replace("-", "_")


@dataclass(frozen=True)
class UpstreamRelease:
    """A concrete downloadable upstream release.

    Single-file sources (pypi, github-*) set url + filename. Multi-file sources
    (huggingface, ADR-002) set files = ((relative_path, url), ...) and leave
    url/filename empty.

    Attributes:
        version: upstream version string (HF: revision commit sha).
        url: absolute download URL (single-file mode).
        filename: basename to store the file as (single-file mode).
        files: (relative_path, absolute url, upstream sha256 or '') triples
            (multi-file mode).
        sha256: upstream-provided digest for single-file mode ('' = none).
    """

    version: str
    url: str
    filename: str
    files: tuple[tuple[str, str, str], ...] = ()
    sha256: str = ""


@dataclass(frozen=True)
class ManifestEntry:
    """One recorded fetch in the manifest (state file).

    Attributes:
        version: version that was fetched.
        filename: file in the cache dir.
        sha256: hex digest of the downloaded file.
        source: human-readable upstream label.
        url: download URL.
        fetched_at: ISO-8601 UTC timestamp.
        digest_verified: True only when sha256 matched an upstream digest
            (ADR-003 §D; False = no upstream digest existed, e.g. github-compat).
    """

    version: str
    filename: str
    sha256: str
    source: str
    url: str
    fetched_at: str = field(default="")
    digest_verified: bool = False
