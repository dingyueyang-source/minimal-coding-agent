import pytest

from app.config import Settings, require_llm_environment
from shared.path_policy import PolicyError, validate_path, validate_request, validate_write


@pytest.mark.parametrize("path", ["../secret.txt", "a/../../x", "a/../b", "/etc/passwd", "/workspace/a.txt",
    "//server/x", "C:\\x", "\\\\server\\x", "a\\..\\b", "C:/x", "c:relative", "", ".", "a/", "a//b", "./a", "a/./b",
    "nul\0", "line\n", "tab\t", "a\x7f", "a\x80", "a\x9f", "\ud800", "x" * 256, "中" * 86,
    "a/" + "b" * 254, None, 1, [], {}])
def test_rejected_paths(path):
    with pytest.raises(PolicyError):
        validate_path(path)


@pytest.mark.parametrize("path", ["sub/dir/a.txt", "中文/空 格!.txt", "~$%20<>.txt", "x" * 255,
    "中" * 85, "a/" + "b" * 253, "name:part", ".../file", "😀.txt"])
def test_allowed_paths(path):
    assert validate_path(path) == path


def test_write_boundaries():
    assert validate_write({"path": "a", "content": ""}) == ("a", b"")
    assert len(validate_write({"path": "a", "content": "x" * 65536})[1]) == 65536
    assert len(validate_write({"path": "a", "content": "中" * 21845})[1]) == 65535
    for payload in ({"path": "a", "content": "x" * 65537}, {"path": "a", "content": "中" * 21846},
                    {"path": "a", "content": "\udfff"}, {"path": "a", "content": None},
                    {"path": "a", "content": 1}, {"path": "a"}, {"path": "a", "content": "", "extra": 1}, []):
        with pytest.raises(PolicyError):
            validate_write(payload)


def test_request_preserves_original():
    assert validate_request("  task\n") == "  task\n"
    assert len(validate_request("x" * 8192)) == 8192
    for value in (" ", "x" * 8193, None, 1, "\ud800"):
        with pytest.raises(PolicyError):
            validate_request(value)


@pytest.mark.parametrize("kwargs", [{"concurrency": 0}, {"task_seconds": -1}, {"cleanup_seconds": float("inf")},
    {"max_files": True}, {"max_llm_calls": 0}, {"project_id": "../x"}, {"image": "python:latest"},
    {"max_records": 1}, {"docker_seconds": 0}, {"scan_seconds": float("nan")}])
def test_bad_settings(kwargs):
    with pytest.raises(ValueError):
        Settings(**kwargs)


def test_production_credentials(monkeypatch):
    for key in ("LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError):
        require_llm_environment()
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("LLM_BASE_URL", "http://remote.example/v1")
    with pytest.raises(ValueError):
        require_llm_environment()
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9999/v1")
    config = require_llm_environment()
    assert config.model == "test-model" and "test-key" not in repr(config)
