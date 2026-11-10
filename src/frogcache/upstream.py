"""Artifact spec loading + source dispatch.

Sources live in sources.py (single-file) and hf.py (HuggingFace, ADR-002) /
docker_source.py (images, ADR-003 §B). Governing ADR: docs/decisions/ADR-001.
"""

import json
from pathlib import Path

from frogcache.data_models.artifact import ArtifactSpec, SourceSpec, UpstreamRelease
from frogcache.sources import (
    latest_compat_release,
    latest_github_release,
    latest_pypi_release,
)


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
            image=entry["source"].get("image", ""),
            tag=entry["source"].get("tag", "latest"),
            upstream_registry=entry["source"].get("upstream_registry", "https://index.docker.io"),
        )
        source.validate()
        specs.append(ArtifactSpec(name=entry["name"], source=source))
    return specs


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
        ValueError: for multi-file source types handled elsewhere (hf, docker).
    """
    if spec.source.type == "pypi":
        return latest_pypi_release(spec, timeout)
    if spec.source.type == "github-compat":
        return latest_compat_release(spec, timeout)
    if spec.source.type == "huggingface":
        raise ValueError("huggingface sources are multi-file: use frogcache.hf (ADR-002)")
    if spec.source.type == "docker":
        raise ValueError("docker sources use frogcache.docker_source (ADR-003 §B)")
    return latest_github_release(spec, github_token, timeout)
