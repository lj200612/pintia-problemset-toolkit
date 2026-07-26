from __future__ import annotations

import json

import pytest

from pta.config import load_config, load_config_or_default, update_config_file
from pta.fetcher import safe_filename


def test_load_config_applies_defaults_normalizes_values_and_honors_env_cookie(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "baseUrl": "https://example.invalid/",
                "problemSetId": 12345,
                "cookie": "cookie-from-file",
                "answersDir": "local-answers",
                "compilerByExtension": {".CXX": "CUSTOM_GXX"},
                "pollIntervalSeconds": "0.25",
                "pollTimeoutSeconds": 9,
                "extraHeaders": {"X-Test": "yes"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PTA_COOKIE", "  cookie-from-env  ")

    cfg = load_config(str(config_path))

    assert cfg.base_url == "https://example.invalid"
    assert cfg.problem_set_id == "12345"
    assert cfg.cookie == "cookie-from-env"
    assert cfg.answers_dir == "local-answers"
    assert cfg.poll_interval_seconds == 0.25
    assert cfg.poll_timeout_seconds == 9.0
    assert cfg.extra_headers == {"X-Test": "yes"}
    assert cfg.compiler_for_extension(".cxx") == "CUSTOM_GXX"
    assert cfg.compiler_for_extension(".PY") == "PYTHON3"
    assert cfg.compiler_for_extension(".unknown") == "GCC"


def test_load_config_missing_file_and_default_fallback(tmp_path, monkeypatch):
    missing = tmp_path / "missing.json"
    monkeypatch.setenv("PTA_COOKIE", "fallback-cookie")

    with pytest.raises(FileNotFoundError, match="找不到配置文件"):
        load_config(str(missing))

    cfg = load_config_or_default(str(missing))
    assert cfg.base_url == "https://pintia.cn"
    assert cfg.problem_set_id == ""
    assert cfg.cookie == "fallback-cookie"


def test_load_config_or_default_recovers_from_invalid_json(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text("{not valid json", encoding="utf-8")
    monkeypatch.delenv("PTA_COOKIE", raising=False)

    cfg = load_config_or_default(str(config_path))

    assert cfg.problem_set_id == ""
    assert cfg.cookie == ""


def test_update_config_file_creates_parent_merges_and_leaves_no_temp_file(tmp_path):
    config_path = tmp_path / "nested" / "config.json"

    update_config_file(str(config_path), {"cookie": "first", "problemSetId": "100", "kept": True})
    update_config_file(str(config_path), {"cookie": "second", "newKey": "value"})

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    assert raw == {
        "cookie": "second",
        "problemSetId": "100",
        "kept": True,
        "newKey": "value",
    }
    assert list(config_path.parent.glob("*.tmp")) == []


def test_update_config_file_replaces_malformed_existing_content(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text("not-json", encoding="utf-8")

    update_config_file(str(config_path), {"problemSetId": "200"})

    assert json.loads(config_path.read_text(encoding="utf-8")) == {"problemSetId": "200"}


@pytest.mark.parametrize(
    ("raw", "maxlen", "expected"),
    [
        ('  bad\\name:/with*illegal?"<>|\tchars.  ', 80, "bad_name_with_illegal_chars"),
        ("...", 80, "untitled"),
        ("", 80, "untitled"),
        ("abcdefgh", 5, "abcde"),
        ("题目名称", 3, "题目名"),
    ],
)
def test_safe_filename_removes_windows_unsafe_characters(raw, maxlen, expected):
    assert safe_filename(raw, maxlen=maxlen) == expected
