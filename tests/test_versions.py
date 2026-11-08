"""Tests for frogcache.versions."""

from frogcache.versions import is_newer, parse_version


def test_parse_version_numeric_parts():
    assert parse_version("3.8.0") == (3, 8, 0)
    assert parse_version("3.10.0a1") == (3, 10, 0)


def test_is_newer_basic():
    assert is_newer("3.9.0", "3.8.0")
    assert not is_newer("3.8.0", "3.8.0")
    assert not is_newer("3.7.9", "3.8.0")


def test_is_newer_pads_shorter_versions():
    assert not is_newer("3.8", "3.8.0")
    assert is_newer("3.8.1", "3.8")
