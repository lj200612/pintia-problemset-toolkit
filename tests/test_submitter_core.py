from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from pta.client import PTAError
from pta.submitter import (
    ACCEPTED_PROBLEM_STATUS,
    AnswerFile,
    ProblemMeta,
    apply_status,
    available_compilers,
    build_catalog,
    build_payload,
    extract_submission_id,
    scan_answers,
    select_targets,
    summarize_submission_result,
    validate_exam,
)


def _meta(
    pspid: str,
    *,
    ptype: str = "PROGRAMMING",
    status: Optional[str] = None,
    index: Optional[int] = 0,
) -> ProblemMeta:
    return ProblemMeta(
        pspid=pspid,
        title=f"Problem {pspid}",
        ptype=ptype,
        score=20,
        index_in_pool=index,
        status=status,
    )


def _answer(pspid: str, path: Optional[Path] = None, ext: str = ".c") -> AnswerFile:
    return AnswerFile(pspid=pspid, path=str(path or Path(f"{pspid}{ext}")), ext=ext)


def test_scan_answers_only_accepts_numeric_regular_files_and_first_extension(tmp_path):
    (tmp_path / "100.py").write_text("print(1)", encoding="utf-8")
    (tmp_path / "100.c").write_text("int main(void){}", encoding="utf-8")
    (tmp_path / "200.CPP").write_text("int main(){}", encoding="utf-8")
    (tmp_path / "README.md").write_text("ignored", encoding="utf-8")
    (tmp_path / "300.c").mkdir()
    (tmp_path / "１２３.c").write_text("full-width digits", encoding="utf-8")
    (tmp_path / "١٢٣.py").write_text("non-ASCII digits", encoding="utf-8")

    answers = scan_answers(str(tmp_path))

    assert list(answers) == ["100", "200"]
    # sorted directory traversal sees 100.c before 100.py, so duplicates are deterministic
    assert answers["100"].ext == ".c"
    assert answers["200"].ext == ".cpp"
    assert scan_answers(str(tmp_path / "does-not-exist")) == {}


def test_scan_answers_rejects_entries_resolving_outside_root(tmp_path, monkeypatch):
    answers_dir = tmp_path / "answers"
    answers_dir.mkdir()
    escaped_entry = answers_dir / "123.c"
    escaped_entry.write_text("inside directory entry", encoding="utf-8")
    safe_entry = answers_dir / "456.c"
    safe_entry.write_text("safe", encoding="utf-8")
    outside = tmp_path / "outside.c"
    outside.write_text("must not be submitted", encoding="utf-8")

    real_resolve = Path.resolve
    outside_resolved = outside.resolve()

    def resolve_with_escaped_entry(self, strict=False):
        if self == escaped_entry:
            return outside_resolved
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve_with_escaped_entry)

    scanned = scan_answers(str(answers_dir))

    assert list(scanned) == ["456"]
    assert Path(scanned["456"].path).parent == real_resolve(answers_dir)


def test_select_targets_filters_only_ids_unknown_and_accepted_then_sorts():
    catalog = {
        "20": _meta("20"),
        "10": _meta("10", status=ACCEPTED_PROBLEM_STATUS),
        "30": _meta("30"),
    }
    answers = {
        "30": _answer("30"),
        "999": _answer("999"),
        "20": _answer("20"),
        "10": _answer("10"),
    }

    targets, unknown, skipped = select_targets(
        catalog, answers, include_accepted=False, only_ids=None
    )

    assert [meta.pspid for meta, _ in targets] == ["20", "30"]
    assert unknown == ["999"]
    assert [meta.pspid for meta in skipped] == ["10"]

    only_targets, only_unknown, only_skipped = select_targets(
        catalog, answers, include_accepted=True, only_ids={"10", "999"}
    )
    assert [meta.pspid for meta, _ in only_targets] == ["10"]
    assert only_unknown == ["999"]
    assert only_skipped == []


def test_build_payload_for_programming_and_code_completion():
    programming = build_payload(_meta("101", index=7), "return 0;", "GXX")
    completion = build_payload(_meta("102", ptype="CODE_COMPLETION", index=None), "return x;", None)

    assert programming == {
        "problemType": "PROGRAMMING",
        "details": [
            {
                "problemId": "7",
                "problemSetProblemId": "101",
                "programmingSubmissionDetail": {
                    "program": "return 0;",
                    "compiler": "GXX",
                },
            }
        ],
    }
    assert completion == {
        "problemType": "CODE_COMPLETION",
        "details": [
            {
                "problemId": "0",
                "problemSetProblemId": "102",
                "codeCompletionSubmissionDetail": {"program": "return x;"},
            }
        ],
    }


def test_build_payload_defaults_compiler_and_rejects_unsupported_type():
    payload = build_payload(_meta("101"), "code", None)
    detail = payload["details"][0]["programmingSubmissionDetail"]
    assert detail["compiler"] == "GCC"

    with pytest.raises(PTAError, match="暂不支持的题型"):
        build_payload(_meta("103", ptype="MULTIPLE_CHOICE"), "answer", None)


def test_summarize_submission_result_prefers_terminal_submission_status():
    result = summarize_submission_result(
        {
            "queued": True,
            "submission": {
                "status": "wrong_answer",
                "score": 5,
                "time": 12,
                "memory": 128,
            },
        }
    )

    assert result["status"] == "WRONG_ANSWER"
    assert result["statusText"] == "答案错误"
    assert result["queued"] is False
    assert result["pending"] is False
    assert result["source"] == "submission"


def test_summarize_submission_result_derives_nested_judge_statuses():
    accepted = summarize_submission_result(
        {
            "queued": True,
            "submission": {
                "status": "SUBMITTED",
                "judgeResponseContents": [{"cases": [{"result": "AC"}, {"verdict": "passed"}]}],
            },
        }
    )
    mixed = summarize_submission_result(
        {
            "submission": {
                "status": "JUDGING",
                "judgeResponseContents": [
                    {"status": "WRONG_ANSWER"},
                    {"status": "TIME_LIMIT_EXCEEDED"},
                ],
            }
        }
    )

    assert accepted["status"] == "ACCEPTED"
    assert accepted["source"] == "judgeResponseContents"
    assert accepted["pending"] is False
    assert mixed["status"] == "MULTIPLE_ERROR"
    assert mixed["pending"] is False


def test_problem_status_fallback_requires_a_nonaccepted_previous_status():
    pending = {
        "queued": True,
        "submission": {"status": "SUBMITTED", "score": None},
    }
    row = {"problemSubmissionStatus": "PROBLEM_ACCEPTED", "score": 20}

    fallback = summarize_submission_result(
        pending,
        problem_status_row=row,
        previous_problem_status="PROBLEM_NO_ANSWER",
    )
    no_history = summarize_submission_result(
        pending, problem_status_row=row, previous_problem_status=None
    )
    historical_ac = summarize_submission_result(
        pending,
        problem_status_row=row,
        previous_problem_status=ACCEPTED_PROBLEM_STATUS,
    )

    assert fallback["status"] == "ACCEPTED"
    assert fallback["score"] == 20
    assert fallback["source"] == "problemStatus"
    assert fallback["pending"] is False
    assert no_history["pending"] is True
    assert historical_ac["pending"] is True


def test_catalog_status_exam_compilers_and_submission_id_helpers():
    catalog = build_catalog(
        {
            "labels": [
                {
                    "id": 42,
                    "label": "A",
                    "type": "PROGRAMMING",
                    "score": 10,
                    "indexInProblemPool": 3,
                }
            ]
        }
    )
    apply_status(
        catalog,
        {"problemStatus": [{"id": "42", "problemSubmissionStatus": "PROBLEM_SUBMITTED"}]},
    )

    assert catalog["42"].title == "A"
    assert catalog["42"].status == "PROBLEM_SUBMITTED"
    assert validate_exam({"exam": {"id": 9, "status": "PROCESSING", "ended": False}}) == (
        "9",
        "PROCESSING",
        False,
    )
    with pytest.raises(PTAError, match="exam.id"):
        validate_exam({"exam": {}})

    assert available_compilers(
        {
            "problemSet": {
                "problemSetConfig": {
                    "compilers": ["GCC", {"value": "PYTHON3"}, {"name": "JAVAC"}, {}]
                }
            }
        }
    ) == ["GCC", "PYTHON3", "JAVAC"]
    assert extract_submission_id({"submissionId": 123}) == "123"
    assert extract_submission_id({"submission": {"id": 456}}) == "456"
    assert extract_submission_id({}) is None
