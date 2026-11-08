"""Tests for frogcache.upstream (artifact loading + source dispatch, network mocked)."""

import json

import pytest

from frogcache import upstream
from frogcache.data_models.artifact import ArtifactSpec, SourceSpec


def _write_artifacts(tmp_path, artifacts):
    path = tmp_path / "artifacts.json"
    path.write_text(json.dumps({"artifacts": artifacts}))
    return str(path)


def test_load_artifacts_valid(tmp_path):
    path = _write_artifacts(tmp_path, [{
        "name": "en-core-web-sm",
        "source": {"type": "github-compat", "repo": "explosion/spacy-models",
                   "branch": "master", "file_pattern": "en_core_web_sm-{version}-py3-none-any.whl"},
    }])
    specs = upstream.load_artifacts(path)
    assert specs[0].name == "en-core-web-sm"
    assert specs[0].underscore_name == "en_core_web_sm"
    assert specs[0].source.repo == "explosion/spacy-models"


def test_load_artifacts_rejects_bad_source(tmp_path):
    path = _write_artifacts(tmp_path, [{"name": "x", "source": {"type": "nope"}}])
    with pytest.raises(ValueError):
        upstream.load_artifacts(path)


def test_latest_compat_release_picks_max_version(tmp_path, monkeypatch):
    compat = {"spacy": {"3.7": {"en_core_web_sm": ["3.7.0", "3.7.1"]},
                        "3.8": {"en_core_web_sm": ["3.8.0"]}}}
    monkeypatch.setattr(upstream, "fetch_json", lambda url, token="", timeout=60: compat)
    spec = ArtifactSpec(
        name="en-core-web-sm",
        source=SourceSpec(type="github-compat", repo="explosion/spacy-models",
                          file_pattern="en_core_web_sm-{version}-py3-none-any.whl"),
    )
    release = upstream.latest_release(spec, timeout=30)
    assert release.version == "3.8.0"
    assert release.filename == "en_core_web_sm-3.8.0-py3-none-any.whl"
    assert release.url == (
        "https://github.com/explosion/spacy-models/releases/download/"
        "en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"
    )


def test_latest_pypi_release_prefers_wheel(monkeypatch):
    data = {
        "info": {"version": "1.2.3"},
        "releases": {"1.2.3": [
            {"packagetype": "sdist", "url": "https://x/pkg-1.2.3.tar.gz", "filename": "pkg-1.2.3.tar.gz"},
            {"packagetype": "bdist_wheel", "url": "https://x/pkg-1.2.3-py3-none-any.whl",
             "filename": "pkg-1.2.3-py3-none-any.whl"},
        ]},
    }
    monkeypatch.setattr(upstream, "fetch_json", lambda url, token="", timeout=60: data)
    spec = ArtifactSpec(name="pkg", source=SourceSpec(type="pypi"))
    release = upstream.latest_release(spec, timeout=30)
    assert release.filename == "pkg-1.2.3-py3-none-any.whl"


def test_latest_pypi_release_404_returns_none(monkeypatch):
    def fake_fetch(url, token="", timeout=60):
        raise RuntimeError(f"GET {url} -> HTTP 404")

    monkeypatch.setattr(upstream, "fetch_json", fake_fetch)
    spec = ArtifactSpec(name="en-core-web-sm", source=SourceSpec(type="pypi"))
    assert upstream.latest_release(spec, timeout=30) is None
