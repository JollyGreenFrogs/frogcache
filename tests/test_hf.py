"""Tests for frogcache.hf (HuggingFace model hosting, network mocked) and HF cache helpers."""

import os

from frogcache import cache, hf
from frogcache.data_models.artifact import ArtifactSpec, SourceSpec


def _hf_spec(files=()):
    return ArtifactSpec(
        name="qwen3-8b",
        source=SourceSpec(type="huggingface", repo="Qwen/Qwen3-8B-GGUF",
                          revision="main", files=tuple(files)),
    )


def test_latest_hf_release_filters_files(monkeypatch):
    def fake_fetch(url, token="", timeout=60):
        if url.endswith("/revision/main"):
            return {"sha": "abc123def456"}
        assert "recursive=true" in url
        return [
            {"type": "directory", "path": "refs"},
            {"type": "file", "path": "qwen3-8b-q4_k_m.gguf"},
            {"type": "file", "path": "config.json"},
            {"type": "file", "path": "README.md"},
        ]

    monkeypatch.setattr(hf, "fetch_json", fake_fetch)
    release = hf.latest_hf_release(_hf_spec(files=("*.gguf", "config.json")), timeout=30)
    assert release.version == "abc123def456"
    assert release.url == "" and release.filename == ""
    paths = [p for p, _ in release.files]
    assert paths == ["qwen3-8b-q4_k_m.gguf", "config.json"]
    # tree + resolve URLs must use the resolved sha, not the mutable revision
    assert release.files[0][1] == (
        "https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/abc123def456/qwen3-8b-q4_k_m.gguf"
    )


def test_latest_hf_release_all_files_when_no_patterns(monkeypatch):
    monkeypatch.setattr(
        hf, "fetch_json",
        lambda url, token="", timeout=60: {"sha": "abc123"}
        if url.endswith("/revision/main")
        else [{"type": "file", "path": "model.gguf"}],
    )
    release = hf.latest_hf_release(_hf_spec(), timeout=30)
    assert [p for p, _ in release.files] == ["model.gguf"]


def test_latest_hf_release_none_when_no_matching_files(monkeypatch):
    monkeypatch.setattr(
        hf, "fetch_json",
        lambda url, token="", timeout=60: {"sha": "abc123"}
        if url.endswith("/revision/main")
        else [{"type": "file", "path": "README.md"}],
    )
    assert hf.latest_hf_release(_hf_spec(files=("*.gguf",)), timeout=30) is None


def test_cached_hf_versions_newest_first_by_mtime(tmp_path):
    for i, version in enumerate(("sha_old", "sha_mid", "sha_new")):
        d = hf.hf_dir(str(tmp_path), "Qwen/Qwen3-8B-GGUF", version)
        d.mkdir(parents=True)
        os.utime(d, (1000 + i, 1000 + i))
    assert hf.cached_hf_versions(str(tmp_path), "Qwen/Qwen3-8B-GGUF") == [
        "sha_new", "sha_mid", "sha_old",
    ]
    assert hf.cached_hf_versions(str(tmp_path), "Qwen/Other") == []


def test_apply_hf_retention_keeps_newest_revisions(tmp_path):
    for i, version in enumerate(("sha_old", "sha_mid", "sha_new")):
        d = hf.hf_dir(str(tmp_path), "Qwen/Qwen3-8B-GGUF", version)
        (d / "model.gguf").parent.mkdir(parents=True, exist_ok=True)
        (d / "model.gguf").write_bytes(b"x")
        os.utime(d, (1000 + i, 1000 + i))
    removed = hf.apply_hf_retention(str(tmp_path), "Qwen/Qwen3-8B-GGUF", keep=2)
    assert removed == ["sha_old"]
    remaining = [p.name for p in (tmp_path / "hf" / "Qwen/Qwen3-8B-GGUF").iterdir()]
    assert sorted(remaining) == ["sha_mid", "sha_new"]


def test_download_atomic_supports_subdir_filename(tmp_path):
    src = tmp_path / "model.gguf"
    src.write_bytes(b"weights")
    dest_dir = tmp_path / "cache" / "hf" / "org" / "model" / "sha123"
    path, sha256 = cache.download_atomic(src.as_uri(), str(dest_dir), "sub/dir/model.gguf", 30)
    assert path == dest_dir / "sub" / "dir" / "model.gguf"
    assert path.read_bytes() == b"weights"
    assert len(sha256) == 64


def test_record_fetch_multi_prunes_old_versions(tmp_path):
    manifest = {"artifacts": {}}
    cache.record_fetch_multi(
        manifest, "qwen3-8b", "sha_old", [("hf/o/m/sha_old/a.gguf", "0" * 64)],
        "huggingface", 2, "2026-11-01T00:00:00+00:00",
    )
    cache.record_fetch_multi(
        manifest, "qwen3-8b", "sha_new", [("hf/o/m/sha_new/a.gguf", "1" * 64),
                                          ("hf/o/m/sha_new/config.json", "2" * 64)],
        "huggingface", 2, "2026-11-08T00:00:00+00:00",
    )
    entries = manifest["artifacts"]["qwen3-8b"]
    assert {e["version"] for e in entries} == {"sha_old", "sha_new"}
    cache.record_fetch_multi(
        manifest, "qwen3-8b", "sha_newer", [("hf/o/m/sha_newer/a.gguf", "3" * 64)],
        "huggingface", 2, "2026-11-09T00:00:00+00:00",
    )
    entries = manifest["artifacts"]["qwen3-8b"]
    assert {e["version"] for e in entries} == {"sha_new", "sha_newer"}
    assert len(entries) == 3


def test_revision_complete_requires_files_on_disk_and_manifest(tmp_path):
    repo = "Qwen/Qwen3-8B-GGUF"
    dest = hf.hf_dir(str(tmp_path), repo, "sha123")
    (dest / "model.gguf").parent.mkdir(parents=True, exist_ok=True)
    (dest / "model.gguf").write_bytes(b"x")
    entries = [{"version": "sha123", "filename": f"hf/{repo}/sha123/model.gguf"}]
    assert hf.revision_complete(str(tmp_path), repo, "sha123", ["model.gguf"], entries)
    # missing manifest entry -> incomplete (retry)
    assert not hf.revision_complete(str(tmp_path), repo, "sha123", ["model.gguf"], [])
    # missing file on disk -> incomplete (retry)
    assert not hf.revision_complete(
        str(tmp_path), repo, "sha123", ["model.gguf", "config.json"], entries
    )


def test_load_artifacts_huggingface(tmp_path):
    import json

    from frogcache import upstream

    path = tmp_path / "artifacts.json"
    path.write_text(json.dumps({"artifacts": [{
        "name": "qwen3-8b",
        "source": {"type": "huggingface", "repo": "Qwen/Qwen3-8B-GGUF",
                   "revision": "main", "files": ["*.gguf"]},
    }]}))
    (spec,) = upstream.load_artifacts(str(path))
    assert spec.source.type == "huggingface"
    assert spec.source.revision == "main"
    assert spec.source.files == ("*.gguf",)


def test_load_artifacts_huggingface_requires_repo(tmp_path):
    import json

    import pytest

    from frogcache import upstream

    path = tmp_path / "artifacts.json"
    path.write_text(json.dumps({"artifacts": [{"name": "x", "source": {"type": "huggingface"}}]}))
    with pytest.raises(ValueError):
        upstream.load_artifacts(str(path))
