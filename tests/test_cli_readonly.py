from __future__ import annotations

import json
from types import SimpleNamespace

import pta_fetch
import pta_list
import pta_submit


def _write_config(tmp_path, **updates):
    values = {
        "baseUrl": "https://example.invalid",
        "problemSetId": "ps-1",
        "cookie": "fake-cookie",
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


def test_list_cli_missing_config_returns_failure_without_constructing_client(
    tmp_path, monkeypatch, capsys
):
    missing = tmp_path / "missing.json"
    monkeypatch.setattr(
        pta_list,
        "parse_args",
        lambda: SimpleNamespace(config=str(missing), all=False, available=False, me=False),
    )

    class ClientMustNotBeCreated:
        def __init__(self, _cfg):
            raise AssertionError("client constructed after configuration failure")

    monkeypatch.setattr(pta_list, "PTAClient", ClientMustNotBeCreated)

    assert pta_list.main() == 2
    assert "找不到配置文件" in capsys.readouterr().out


def test_fetch_list_mode_only_reads_catalog_and_creates_no_output(tmp_path, monkeypatch, capsys):
    config_path = _write_config(tmp_path)
    output_dir = tmp_path / "must-not-be-created"

    class ReadOnlyClient:
        def __init__(self, _cfg):
            self.detail_calls = 0

        def get_problem_types(self):
            return {
                "labels": [
                    {
                        "id": "100",
                        "title": "Read only problem",
                        "type": "PROGRAMMING",
                        "score": 20,
                        "indexInProblemPool": 0,
                    }
                ]
            }

        def get_problem_detail(self, _pspid):
            self.detail_calls += 1
            raise AssertionError("--list must not fetch problem details")

    client = ReadOnlyClient(None)
    monkeypatch.setattr(pta_fetch, "PTAClient", lambda _cfg: client)
    monkeypatch.setattr(pta_fetch, "auto_configure_problem_set", lambda *_: _selection())
    monkeypatch.setattr(
        pta_fetch,
        "parse_args",
        lambda: SimpleNamespace(
            config=str(config_path),
            out=str(output_dir),
            only="",
            type="",
            overwrite=False,
            combined=False,
            list=True,
        ),
    )

    assert pta_fetch.main() == 0
    captured = capsys.readouterr().out
    assert "Read only problem" in captured
    assert "共 1 题" in captured
    assert client.detail_calls == 0
    assert not output_dir.exists()


def test_submit_list_mode_never_submits_or_creates_log(tmp_path, monkeypatch, capsys):
    answers_dir = tmp_path / "answers"
    answers_dir.mkdir()
    (answers_dir / "100.c").write_text("int main(void) { return 0; }", encoding="utf-8")
    config_path = _write_config(tmp_path)

    class ReadOnlyClient:
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
                        "title": "Listed answer",
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

        def submit(self, *_args, **_kwargs):
            raise AssertionError("--list must never submit")

    monkeypatch.setattr(pta_submit, "PTAClient", lambda _cfg: ReadOnlyClient())
    monkeypatch.setattr(pta_submit, "auto_configure_problem_set", lambda *_: _selection())
    monkeypatch.setattr(
        pta_submit,
        "parse_args",
        lambda: SimpleNamespace(
            config=str(config_path),
            list=True,
            dry_run=False,
            yes=False,
            force=False,
            no_poll=False,
            include_accepted=False,
            only="",
        ),
    )

    assert pta_submit.main() == 0
    captured = capsys.readouterr().out
    assert "Listed answer" in captured
    assert "有答案" in captured
    assert not (tmp_path / "logs").exists()


def test_submit_dry_run_with_answer_never_submits_or_creates_log(tmp_path, monkeypatch, capsys):
    answers_dir = tmp_path / "answers"
    answers_dir.mkdir()
    (answers_dir / "100.c").write_text("int main(void) { return 0; }", encoding="utf-8")
    config_path = _write_config(tmp_path)

    class DryRunClient:
        def get_exam(self):
            # Dry-run remains non-mutating even if the exam is not currently submit-able.
            return {
                "exam": {"id": "exam-1", "status": "ENDED", "ended": True},
                "problemSet": {"problemSetConfig": {"compilers": ["GCC"]}},
            }

        def get_problem_types(self):
            return {
                "labels": [
                    {
                        "id": "100",
                        "title": "Dry run answer",
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

        def submit(self, *_args, **_kwargs):
            raise AssertionError("--dry-run must never submit")

    monkeypatch.setattr(pta_submit, "PTAClient", lambda _cfg: DryRunClient())
    monkeypatch.setattr(pta_submit, "auto_configure_problem_set", lambda *_: _selection())
    monkeypatch.setattr(
        pta_submit,
        "parse_args",
        lambda: SimpleNamespace(
            config=str(config_path),
            list=False,
            dry_run=True,
            yes=True,
            force=False,
            no_poll=False,
            include_accepted=False,
            only="",
        ),
    )

    assert pta_submit.main() == 0
    assert "未提交任何内容" in capsys.readouterr().out
    assert not (tmp_path / "logs").exists()
