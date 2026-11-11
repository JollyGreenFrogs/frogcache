"""Tests for net.py URL allowlist + safe redirect handling."""

import urllib.request

import pytest

from frogcache import net


def test_safe_url_allows_listed_hosts_and_rejects_others():
    assert net.safe_url("https://pypi.org/pypi/x/json").startswith("https://pypi.org/")
    assert net.safe_url("https://huggingface.co/a/b/resolve/main/f.bin").startswith(
        "https://huggingface.co/"
    )
    with pytest.raises(RuntimeError):
        net.safe_url("https://evil.example.com/x")
    with pytest.raises(RuntimeError):
        net.safe_url("http://pypi.org/x")  # http only for the private registry


def test_redirect_handler_allows_artifact_cdn_targets():
    handler = net._SafeRedirectHandler()
    req = urllib.request.Request("https://github.com/o/r/releases/download/v/a.bin")
    for target in (
        "https://release-assets.githubusercontent.com/abc",
        "https://objects.githubusercontent.com/abc",
        "https://cas-bridge.xethub.hf.co/abc",
    ):
        r = handler.redirect_request(req, None, 302, "Found", {}, target)
        assert r is not None and r.full_url == target


def test_redirect_handler_rejects_non_allowlisted_target():
    handler = net._SafeRedirectHandler()
    req = urllib.request.Request("https://huggingface.co/a/b/resolve/main/f.bin")
    with pytest.raises(RuntimeError):
        handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example.com/x")
    with pytest.raises(RuntimeError):
        handler.redirect_request(req, None, 302, "Found", {}, "file:///etc/passwd")
