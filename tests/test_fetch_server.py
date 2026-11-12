"""Tests for frogcache.fetch_server path-shape source inference (ADR-003 §A)."""

from frogcache import fetch_server


def test_infer_wheel_path_goes_to_pypi():
    spec = fetch_server.infer_spec_from_path("en_core_web_sm-3.8.0-py3-none-any.whl")
    assert spec is not None
    assert spec.name == "en-core-web-sm"
    assert spec.source.type == "pypi"


def test_infer_hf_path_goes_to_huggingface():
    spec = fetch_server.infer_spec_from_path("hf/Qwen/Qwen3-8B-GGUF/abc123/model.gguf")
    assert spec is not None
    assert spec.source.type == "huggingface"
    assert spec.source.repo == "Qwen/Qwen3-8B-GGUF"


def test_infer_unknown_path_returns_none():
    assert fetch_server.infer_spec_from_path("etc/passwd") is None
    assert fetch_server.infer_spec_from_path("hf/onlyone") is None


def test_safe_target_rejects_traversal(tmp_path):
    from frogcache import fetch_server
    from frogcache.data_models.config import Config

    cfg = Config(cache_dir=str(tmp_path), artifacts_file=str(tmp_path / "a.json"),
                 secrets_file=str(tmp_path / "s.env"))
    handler = fetch_server._Handler.__new__(fetch_server._Handler)
    handler.config = cfg
    assert handler._safe_target("../etc/passwd") is None
    assert handler._safe_target("sub/ok.whl") is not None
