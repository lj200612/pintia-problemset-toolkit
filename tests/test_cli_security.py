from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import pta_fetch
import pta_submit
from pta.client import PTAHTTPError


def _write_config(tmp_path, **updates):
    values = {
        "baseUrl": "https://example.invalid",
        "problemSetId": "ps-1",
        "cookie": "session=CLI_COOKIE_SECRET",
        "answersDir": str(tmp_path / "answers"),
        "problemsDir": str(tmp_path / "problems"),
        "logDir": str(tmp_path / "logs"),
        "fetchDelaySeconds": 0,
        "submitDelaySeconds": 0,
    }
    values.update(updates)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    return path


def _selection():
    return SimpleNamespace(choice=None, candidates=[], reason="test")


def _args(config_path, **updates):
    values = {
        "config": str(config_path),
        "list": False,
        "dry_run": False,
        "yes": True,
        "force": False,
        "no_poll": False,
        "include_accepted": False,
        "only": "",
    }
    values.update(updates)
    return SimpleNamespace(**values)


def test_submit_jsonl_uses_safe_http_error_fields(tmp_path, monkeypatch, capsys):
    source = "int main(void) { return 0; } /* CLI_SOURCE_SECRET */"
    answers_dir = tmp_path / "answers"
    answers_dir.mkdir()
    (answers_dir / "100.c").write_text(source, encoding="utf-8")
    config_path = _write_config(tmp_path)

    class ErrorClient:
        def get_exam(self):
            return {
                "exam": {"id": "exam-1", "status": "PROCESSING", "ended": False},
                "problemSet": {"problemSetConfig": {"compilers": ["GCC"]}},
            }

        def get_problem_types(self):
            return {
                "labels": [
                    {
                        "id": "100",
                        "title": "Safe title",
                        "type": "PROGRAMMING",
                        "score": 20,
                        "indexInProblemPool": 0,
                    }
                ]
            }

        def get_problem_status(self, _exam_id):
            return {
                "problemStatus": [{"id": "100", "problemSubmissionStatus": "PROBLEM_NO_ANSWER"}]
            }

        def submit(self, _exam_id, _payload):
            raise PTAHTTPError(
                502,
                f"remote echo: {source}; cookie=session=CLI_COOKIE_SECRET",
                (
                    "https://url-user:url-password@example.invalid/api/submissions"
                    "?cookie=CLI_COOKIE_SECRET"
                ),
            )

    monkeypatch.setattr(pta_submit, "PTAClient", lambda _cfg: ErrorClient())
    monkeypatch.setattr(pta_submit, "auto_configure_problem_set", lambda *_: _selection())
    monkeypatch.setattr(pta_submit, "parse_args", lambda: _args(config_path))

    assert pta_submit.main() == 1
    terminal = capsys.readouterr().out
    assert "CLI_SOURCE_SECRET" in terminal

    log_path = next((tmp_path / "logs").glob("submit-*.jsonl"))
    log_text = log_path.read_text(encoding="utf-8")
    record = json.loads(log_text)
    assert record["status"] == "ERROR"
    assert "message" not in record
    assert record["error"] == {
        "errorClass": "PTAHTTPError",
        "errorCode": "PTA_HTTP_ERROR",
        "httpStatus": 502,
        "requestUrl": "https://example.invalid/api/submissions",
    }
    for secret in ("CLI_SOURCE_SECRET", "CLI_COOKIE_SECRET", "url-password"):
        assert secret not in log_text


def test_fetch_skips_non_ascii_or_non_numeric_platform_ids(tmp_path, monkeypatch, capsys):
    config_path = _write_config(tmp_path)
    output_dir = tmp_path / "problems"

    class FetchClient:
        def __init__(self):
            self.detail_calls = []

        def get_problem_types(self):
            return {
                "labels": [
                    {
                        "id": problem_id,
                        "title": title,
                        "type": "PROGRAMMING",
                        "score": 20,
                        "indexInProblemPool": index,
                    }
                    for index, (problem_id, title) in enumerate(
                        [
                            ("100", "Safe problem"),
                            ("../outside", "Traversal"),
                            ("１２３", "Unicode digits"),
                            ("100/../../escape", "Nested traversal"),
                        ]
                    )
                ]
            }

        def get_problem_detail(self, problem_id):
            self.detail_calls.append(problem_id)
            return {"id": problem_id}

    client = FetchClient()
    monkeypatch.setattr(pta_fetch, "PTAClient", lambda _cfg: client)
    monkeypatch.setattr(pta_fetch, "auto_configure_problem_set", lambda *_: _selection())
    monkeypatch.setattr(pta_fetch, "format_problem", lambda detail: f"# {detail['id']}")
    monkeypatch.setattr(
        pta_fetch,
        "parse_args",
        lambda: SimpleNamespace(
            config=str(config_path),
            out=str(output_dir),
            only="",
            type="",
            overwrite=False,
            combined=True,
            list=False,
        ),
    )

    assert pta_fetch.main() == 1
    output = capsys.readouterr().out
    assert output.count("平台返回无效题号") == 3
    assert client.detail_calls == ["100"]
    assert (output_dir / "100-Safe problem.md").read_text(encoding="utf-8") == "# 100"
    assert (output_dir / "_all.md").read_text(encoding="utf-8") == "# 100"
    assert not (tmp_path / "outside.md").exists()
    assert not (tmp_path / "escape.md").exists()


def test_resolve_output_path_rejects_parent_escape_and_accepts_combined(tmp_path):
    output_dir = tmp_path / "problems"
    output_dir.mkdir()

    with pytest.raises(ValueError, match="超出目标目录"):
        pta_fetch.resolve_output_path(output_dir, str(Path("..") / "outside.md"))

    combined = pta_fetch.resolve_output_path(output_dir, "_all.md")
    assert combined == output_dir.resolve() / "_all.md"
