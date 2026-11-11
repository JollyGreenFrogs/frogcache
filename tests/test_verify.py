"""Tests for frogcache.verify (ADR-003 §D staged verification)."""

import zipfile

import pytest

from frogcache import verify


def _sha(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def test_verify_digest_match_mismatch_required_missing(tmp_path):
    f = tmp_path / "f.bin"
    f.write_bytes(b"payload")
    digest = _sha(b"payload")
    assert verify.verify_digest(f, digest, required=True) is True
    with pytest.raises(RuntimeError):
        verify.verify_digest(f, "0" * 64, required=True)
    with pytest.raises(RuntimeError):
        verify.verify_digest(f, "", required=True)  # required but missing -> fail
    assert verify.verify_digest(f, "", required=False) is False  # allowed, unverified


def test_check_format_wheel_json_gguf(tmp_path):
    whl = tmp_path / "a-1.0-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as zf:
        zf.writestr("a-1.0.dist-info/METADATA", "Name: a")
        zf.writestr("a-1.0.dist-info/WHEEL", "Wheel-Version: 1.0")
    verify.check_format(whl)
    bad = tmp_path / "b-1.0-py3-none-any.whl"
    bad.write_bytes(b"not a zip")
    with pytest.raises(RuntimeError):
        verify.check_format(bad)
    js = tmp_path / "c.json"
    js.write_text('{"ok": true}')
    verify.check_format(js)
    badjson = tmp_path / "d.json"
    badjson.write_text("{nope")
    with pytest.raises(RuntimeError):
        verify.check_format(badjson)
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"GGUF" + b"\x00" * 16)
    verify.check_format(gguf)
    badg = tmp_path / "m2.gguf"
    badg.write_bytes(b"NOPE")
    with pytest.raises(RuntimeError):
        verify.check_format(badg)


def test_verify_and_promote_success_and_quarantine(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "scan_malware", lambda path, timeout=600: None)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    dest = tmp_path / "cache" / "f.bin"
    data = b"clean bytes"
    part = sandbox / "f.bin.part"
    part.write_bytes(data)
    ok, digest, verified = verify.verify_and_promote(
        part, dest, expected_sha256=_sha(data), quarantine=tmp_path / "quarantine", digest_required=True
    )
    assert ok and verified and dest.read_bytes() == data and not part.exists()

    # mismatch -> quarantined, dest absent
    part2 = sandbox / "g.bin.part"
    part2.write_bytes(b"evil")
    ok2, _, verified2 = verify.verify_and_promote(
        part2, tmp_path / "cache" / "g.bin", expected_sha256="0" * 64,
        quarantine=tmp_path / "quarantine", digest_required=True,
    )
    assert ok2 is False and verified2 is False
    assert not (tmp_path / "cache" / "g.bin").exists()
    assert (tmp_path / "quarantine" / "g.bin.part").exists()


def test_fetch_and_promote_via_file_url(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "scan_malware", lambda path, timeout=600: None)
    src = tmp_path / "pkg.whl"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("pkg-1.0.dist-info/METADATA", "Name: pkg")
        zf.writestr("pkg-1.0.dist-info/WHEEL", "Wheel-Version: 1.0")
    dest = tmp_path / "cache" / "pkg.whl"
    ok, digest, verified = verify.fetch_and_promote(
        src.as_uri(), dest, tmp_path / "sandbox", 30, allow_file=True,
        expected_sha256=_sha(src.read_bytes()), digest_required=True,
    )
    assert ok and verified and dest.is_file()


def test_check_format_strips_part_suffix(tmp_path):
    whl = tmp_path / "a-1.0-py3-none-any.whl.part"
    with zipfile.ZipFile(whl, "w") as zf:
        zf.writestr("a-1.0.dist-info/METADATA", "Name: a")
        zf.writestr("a-1.0.dist-info/WHEEL", "Wheel-Version: 1.0")
    verify.check_format(whl)  # must not raise despite the .part suffix


def test_scan_malware_timeout_is_runtime_error(tmp_path, monkeypatch):
    import subprocess

    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired("clamscan", 600)

    monkeypatch.setattr(verify.subprocess, "run", boom)
    with pytest.raises(RuntimeError):
        verify.scan_malware(tmp_path / "x.bin")
