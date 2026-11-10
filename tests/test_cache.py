"""Tests for frogcache.cache (retention, download, manifest)."""

import json

from frogcache import cache, manifest
from frogcache.data_models.artifact import ManifestEntry


def _touch_wheel(dir, name, version):
    path = dir / f"{name}-{version}-py3-none-any.whl"
    path.write_bytes(f"wheel-{version}".encode())
    return path.name


def test_parse_wheel_filename():
    assert cache.parse_wheel_filename("en_core_web_sm-3.8.0-py3-none-any.whl") == (
        "en_core_web_sm",
        "3.8.0",
    )
    assert cache.parse_wheel_filename("notawheel.txt") is None


def test_cached_files_sorted_newest_first(tmp_path):
    _touch_wheel(tmp_path, "en_core_web_sm", "3.7.0")
    _touch_wheel(tmp_path, "en_core_web_sm", "3.10.0")
    _touch_wheel(tmp_path, "en_core_web_sm", "3.8.0")
    _touch_wheel(tmp_path, "de_core_web_sm", "9.9.9")
    files = cache.cached_files(str(tmp_path), "en-core-web-sm")
    assert [v for v, _ in files] == ["3.10.0", "3.8.0", "3.7.0"]


def test_newest_cached_version(tmp_path):
    assert cache.newest_cached_version(str(tmp_path), "en-core-web-sm") is None
    _touch_wheel(tmp_path, "en_core_web_sm", "3.8.0")
    assert cache.newest_cached_version(str(tmp_path), "en-core-web-sm") == "3.8.0"


def test_apply_retention_versions_keeps_db_keep_set_not_version_order(tmp_path):
    for version in ("3.6.0", "3.7.0", "3.8.0", "3.9.0"):
        _touch_wheel(tmp_path, "en_core_web_sm", version)
    # DB keep-set is fetch-time based: 3.9.0 and 3.7.0 recorded, 3.8.0 pruned
    # in the DB — version ordering would wrongly keep 3.8.0 and delete 3.7.0
    removed = cache.apply_retention_versions(str(tmp_path), "en-core-web-sm", {"3.9.0", "3.7.0"})
    assert sorted(removed) == [
        "en_core_web_sm-3.6.0-py3-none-any.whl",
        "en_core_web_sm-3.8.0-py3-none-any.whl",
    ]
    remaining = sorted(p.name for p in tmp_path.glob("*.whl"))
    assert remaining == [
        "en_core_web_sm-3.7.0-py3-none-any.whl",
        "en_core_web_sm-3.9.0-py3-none-any.whl",
    ]


def test_download_atomic_and_manifest(tmp_path, tmp_path_factory, db_conn):
    payload = tmp_path_factory.mktemp("src") / "file.whl"
    payload.write_bytes(b"hello wheel")
    cache_dir = tmp_path / "cache"
    path, sha256 = cache.download_atomic(payload.as_uri(), str(cache_dir), "a-1.0-py3-none-any.whl", 30)
    assert path.name == "a-1.0-py3-none-any.whl"
    assert path.read_bytes() == b"hello wheel"
    assert len(sha256) == 64
    assert not list(cache_dir.glob(".*.part"))

    conn = db_conn
    manifest.record_fetch(
        conn,
        "a",
        ManifestEntry(version="1.0", filename=path.name, sha256=sha256, source="pypi", url=""),
        keep=2,
    )
    rows = manifest.entries(conn, "a")
    assert rows[0]["sha256"] == sha256 and rows[0]["digest_verified"] == 0


def test_is_update_available(tmp_path):
    assert cache.is_update_available("3.8.0", str(tmp_path), "en-core-web-sm")  # nothing cached
    _touch_wheel(tmp_path, "en_core_web_sm", "3.8.0")
    assert not cache.is_update_available("3.8.0", str(tmp_path), "en-core-web-sm")
    assert cache.is_update_available("3.9.0", str(tmp_path), "en-core-web-sm")


def test_apply_retention_versions_prunes_unrecorded_files(tmp_path):
    for v in ("1.0", "2.0", "3.0"):
        _touch_wheel(tmp_path, "pkg", v)
    # only 1.0 is in the manifest keep-set (recently re-fetched): unrecorded
    # 2.0/3.0 are deleted even though version ordering would keep them
    removed = cache.apply_retention_versions(str(tmp_path), "pkg", {"1.0"})
    assert sorted(removed) == ["pkg-2.0-py3-none-any.whl", "pkg-3.0-py3-none-any.whl"]
    remaining = sorted(p.name for p in tmp_path.glob("*.whl"))
    assert remaining == ["pkg-1.0-py3-none-any.whl"]
