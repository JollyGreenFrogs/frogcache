"""Tests for frogcache.hf (HuggingFace model hosting, network mocked) and HF cache helpers."""

import os

from frogcache import cache, hf, manifest
from frogcache.data_models.artifact import ArtifactSpec, SourceSpec


def _hf_spec(files=()):
    return ArtifactSpec(
        name="qwen3-8b",
        source=SourceSpec(type="huggingface", repo="Qwen/Qwen3-8B-GGUF",
                          revision="main", files=tuple(files)),
    )


def _tree():
    return [
        {"type": "directory", "path": "refs"},
        {"type": "file", "path": "qwen3-8b-q4_k_m.gguf"},
        {"type": "file", "path": "config.json"},
        {"type": "file", "path": "README.md"},
    ]


def test_latest_hf_release_filters_files(monkeypatch):
    monkeypatch.setattr(
        hf, "fetch_json",
        lambda url, token="", timeout=60: {"sha": "abc123def456"}
        if url.endswith("/revision/main")
        else (_ for _ in ()).throw(AssertionError("tree must use fetch_tree_pages")),
    )
    monkeypatch.setattr(hf, "fetch_tree_pages", lambda url, timeout=60: _tree())
    release = hf.latest_hf_release(_hf_spec(files=("*.gguf", "config.json")), timeout=30)
    assert release.version == "abc123def456"
    assert release.url == "" and release.filename == ""
    paths = [p for p, _, _ in release.files]
    assert paths == ["qwen3-8b-q4_k_m.gguf", "config.json"]
    # tree + resolve URLs must use the resolved sha, not the mutable revision
    assert release.files[0][1] == (
        "https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/abc123def456/qwen3-8b-q4_k_m.gguf"
    )


def test_latest_hf_release_all_files_when_no_patterns(monkeypatch):
    monkeypatch.setattr(hf, "fetch_json", lambda url, token="", timeout=60: {"sha": "abc123"})
    monkeypatch.setattr(
        hf, "fetch_tree_pages", lambda url, timeout=60: [{"type": "file", "path": "model.gguf"}]
    )
    release = hf.latest_hf_release(_hf_spec(), timeout=30)
    assert [p for p, _, _ in release.files] == ["model.gguf"]


def test_latest_hf_release_none_when_no_matching_files(monkeypatch):
    monkeypatch.setattr(hf, "fetch_json", lambda url, token="", timeout=60: {"sha": "abc123"})
    monkeypatch.setattr(
        hf, "fetch_tree_pages", lambda url, timeout=60: [{"type": "file", "path": "README.md"}]
    )
    assert hf.latest_hf_release(_hf_spec(files=("*.gguf",)), timeout=30) is None


def test_fetch_tree_pages_follows_link_next(monkeypatch):
    import json as jsonlib

    pages = {
        "https://huggingface.co/api/x/tree/sha?recursive=true": (
            [{"type": "file", "path": "a.gguf"}],
            '<https://huggingface.co/api/x/tree/sha?recursive=true&cursor=2>; rel="next"',
        ),
        "https://huggingface.co/api/x/tree/sha?recursive=true&cursor=2": (
            [{"type": "file", "path": "b.gguf"}],
            "",
        ),
    }

    class FakeResponse:
        def __init__(self, body, link):
            self._body = body
            self.headers = {"Link": link}

        def read(self):
            return jsonlib.dumps(self._body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=60):
        return FakeResponse(*pages[request.full_url])

    monkeypatch.setattr(hf.urllib.request, "urlopen", fake_urlopen)
    items = hf.fetch_tree_pages("https://huggingface.co/api/x/tree/sha?recursive=true", timeout=30)
    assert [i["path"] for i in items] == ["a.gguf", "b.gguf"]


def test_cached_hf_versions_newest_first_by_mtime(tmp_path):
    for i, version in enumerate(("sha_old", "sha_mid", "sha_new")):
        d = hf.hf_dir(str(tmp_path), "Qwen/Qwen3-8B-GGUF", version)
        d.mkdir(parents=True)
        os.utime(d, (1000 + i, 1000 + i))
    assert hf.cached_hf_versions(str(tmp_path), "Qwen/Qwen3-8B-GGUF") == [
        "sha_new", "sha_mid", "sha_old",
    ]
    assert hf.cached_hf_versions(str(tmp_path), "Qwen/Other") == []


def test_apply_hf_retention_keeps_only_keep_versions_and_drops_incomplete(tmp_path):
    for i, version in enumerate(("sha_old", "sha_mid", "sha_new", "sha_partial")):
        d = hf.hf_dir(str(tmp_path), "Qwen/Qwen3-8B-GGUF", version)
        (d / "model.gguf").parent.mkdir(parents=True, exist_ok=True)
        (d / "model.gguf").write_bytes(b"x")
        os.utime(d, (1000 + i, 1000 + i))
    # sha_partial is newest on disk but has no manifest entries (failed download)
    removed = hf.apply_hf_retention(
        str(tmp_path), "Qwen/Qwen3-8B-GGUF", keep_versions={"sha_mid", "sha_new"}
    )
    assert sorted(removed) == ["sha_old", "sha_partial"]
    remaining = [p.name for p in (tmp_path / "hf" / "Qwen/Qwen3-8B-GGUF").iterdir()]
    assert sorted(remaining) == ["sha_mid", "sha_new"]


def test_download_atomic_rejects_unsafe_filename(tmp_path):
    src = tmp_path / "model.gguf"
    src.write_bytes(b"weights")
    dest_dir = tmp_path / "cache"
    for bad in ("sub/dir/model.gguf", "../escape.gguf", "/abs/model.gguf", ".."):
        try:
            cache.download_atomic(src.as_uri(), str(dest_dir), bad, 30, allow_file=True)
        except RuntimeError:
            pass
        else:
            raise AssertionError(f"unsafe filename accepted: {bad!r}")
    # plain basenames still work, including dotfiles like .gitattributes
    for good in ("model.gguf", ".gitattributes"):
        path, sha256 = cache.download_atomic(src.as_uri(), str(dest_dir), good, 30, allow_file=True)
        assert path == dest_dir / good
        assert path.read_bytes() == b"weights"
        assert len(sha256) == 64


def test_record_and_prune_versions_align_with_disk_retention(tmp_path, db_conn):
    conn = db_conn
    manifest.record_fetch_multi(
        conn, "qwen3-8b", "sha_old", [("hf/o/m/sha_old/a.gguf", "0" * 64, True)],
        "huggingface", "2026-11-01T00:00:00+00:00",
    )
    manifest.record_fetch_multi(
        conn, "qwen3-8b", "sha_new", [("hf/o/m/sha_new/a.gguf", "1" * 64, True),
                                      ("hf/o/m/sha_new/config.json", "2" * 64, True)],
        "huggingface", "2026-11-08T00:00:00+00:00",
    )
    manifest.record_fetch_multi(
        conn, "qwen3-8b", "sha_newer", [("hf/o/m/sha_newer/a.gguf", "3" * 64, True)],
        "huggingface", "2026-11-09T00:00:00+00:00",
    )
    # newest-first by fetched_at; keep newest 2 -> prune sha_old from the DB
    assert manifest.versions(conn, "qwen3-8b") == ["sha_newer", "sha_new", "sha_old"]
    kept = set(manifest.versions(conn, "qwen3-8b")[:2])
    removed = manifest.prune_versions(conn, "qwen3-8b", kept)
    assert removed == ["sha_old"]
    entries = manifest.entries(conn, "qwen3-8b")
    assert {e["version"] for e in entries} == {"sha_new", "sha_newer"}
    assert len(entries) == 3


def test_migrate_legacy_manifest_json_once(tmp_path, db_conn):
    import json as jsonlib

    legacy = tmp_path / "manifest.json"
    legacy.write_text(jsonlib.dumps({"artifacts": {"a": [
        {"version": "1.0", "filename": "a-1.0-py3-none-any.whl", "sha256": "x" * 64,
         "source": "pypi", "url": "", "fetched_at": "2026-11-01T00:00:00+00:00"}]}}))
    conn = db_conn
    assert manifest.migrate_legacy(conn, str(legacy)) is True
    assert manifest.migrate_legacy(conn, str(legacy)) is False  # once only
    assert manifest.versions(conn, "a") == ["1.0"]


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
