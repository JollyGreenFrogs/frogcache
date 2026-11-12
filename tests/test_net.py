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


def test_file_scheme_rejected_by_default():
    # production paths must never fetch file:// (local file read into the cache)
    with pytest.raises(RuntimeError):
        net.safe_url("file:///etc/passwd")
    with pytest.raises(RuntimeError):
        net.checked_url("file:///etc/passwd")
    assert net.safe_url("file:///tmp/x", allow_file=True) == "file:///tmp/x"


def test_redirect_strips_authorization_cross_host():
    handler = net._SafeRedirectHandler()
    req = urllib.request.Request("https://huggingface.co/a", headers={"Authorization": "Bearer secret"})
    r = handler.redirect_request(req, None, 302, "Found", {}, "https://api.github.com/x")
    assert r is not None and r.get_header("Authorization") is None  # token never crosses hosts
    req2 = urllib.request.Request("https://huggingface.co/a", headers={"Authorization": "Bearer secret"})
    r2 = handler.redirect_request(req2, None, 302, "Found", {}, "https://huggingface.co/b")
    assert r2.get_header("Authorization") == "Bearer secret"  # same origin keeps it
    # same host, different port = different origin -> strip
    req3 = urllib.request.Request("https://huggingface.co/a", headers={"Authorization": "Bearer secret"})
    r3 = handler.redirect_request(req3, None, 302, "Found", {}, "https://huggingface.co:8443/b")
    assert r3.get_header("Authorization") is None


def test_redact_url_strips_query_and_fragment():
    assert net.redact_url("https://objects.githubusercontent.com/a?X-Amz-Signature=secret&x=1#f") == \
        "https://objects.githubusercontent.com/a"
    assert net.redact_url("https://huggingface.co/a/b/resolve/main/f?cursor=abc") == \
        "https://huggingface.co/a/b/resolve/main/f"


def test_same_origin_upload_location():
    from frogcache import docker_source

    base = "http://registry.lan:5000"
    assert docker_source._same_origin_url(base, "/v2/x/blobs/uploads/abc?_state=1") == \
        "http://registry.lan:5000/v2/x/blobs/uploads/abc?_state=1"
    assert docker_source._same_origin_url(base, "http://registry.lan:5000/v2/x/blobs/uploads/abc") == \
        "http://registry.lan:5000/v2/x/blobs/uploads/abc"
    with pytest.raises(RuntimeError):
        docker_source._same_origin_url(base, "http://evil.example.com/upload")
    with pytest.raises(RuntimeError):
        docker_source._same_origin_url(base, "https://registry.lan:5000/upload")  # scheme change
    with pytest.raises(RuntimeError):
        docker_source._same_origin_url(base, "http://registry.lan:9999/upload")  # port change = different origin
    with pytest.raises(RuntimeError):
        docker_source._same_origin_url(base, "")
    # same origin with default port spelled out explicitly is fine
    assert docker_source._same_origin_url("https://registry.lan", "https://registry.lan:443/x") == \
        "https://registry.lan:443/x"
