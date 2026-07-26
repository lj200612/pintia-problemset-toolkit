import os
import subprocess
from pathlib import Path

import pytest

from scripts import check_release as guard


def _write(root: Path, name: str, content: str | bytes = "public\n") -> Path:
    path = root.joinpath(*name.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_allowlist_accepts_public_source_document_and_metadata_shapes(tmp_path):
    names = [
        "README.md",
        "pyproject.toml",
        "uv.lock",
        "pta/client.py",
        "pta/web/index.html",
        "pta/web/style.css",
        "pta/web/app.js",
        "scripts/check_release.py",
        "tests/test_example.py",
        "docs/guide.rst",
        "licenses/LUCIDE.txt",
        ".github/release-notes/v0.1.0.md",
        ".github/workflows/ci.yml",
        ".github/ISSUE_TEMPLATE/bug_report.yaml",
        "answers/.gitkeep",
    ]
    for name in names:
        _write(tmp_path, name)

    assert guard.collect_violations(names, tmp_path) == []


@pytest.mark.parametrize(
    "name",
    [
        "screenshot.png",
        "tool.exe",
        "random.py",
        "config.json",
        "pta/web/screenshot.jpg",
        "pta/nested/hidden.py",
        "tests/fixtures/archive.zip",
        "licenses/icon.png",
        ".github/private/key.pem",
        "../README.md",
        "C:/Users/example/README.md",
    ],
)
def test_allowlist_rejects_unreviewed_paths(name):
    assert list(guard.path_rules(name))


@pytest.mark.parametrize(
    ("payload", "rule"),
    [
        ("hello".encode("utf-16"), "utf-16-or-32-text-not-allowed"),
        ("hello".encode("utf-16-le"), "utf-16-or-32-text-not-allowed"),
        (b"\x80\x81", "non-utf8-text-not-allowed"),
        (b"\x89PNG\r\n\x1a\nrest", "image-content-not-allowed"),
        (b"MZfake executable", "executable-content-not-allowed"),
        (b"PK\x03\x04fake archive", "archive-content-not-allowed"),
        (b"abc\0def", "binary-content-not-allowed"),
        (b"valid text\x01", "binary-control-characters"),
    ],
)
def test_content_guard_rejects_binary_and_non_utf8_payloads(payload, rule):
    assert rule in set(guard.content_rules("pta/web/app.js", payload))


def test_single_file_total_and_file_count_limits(tmp_path, monkeypatch):
    _write(tmp_path, "README.md", "1234")
    _write(tmp_path, "LICENSE", "5678")

    monkeypatch.setattr(guard, "MAX_FILE_BYTES", 3)
    violations = guard.collect_violations(["README.md"], tmp_path)
    assert ("README.md", "file-size-limit-exceeded") in violations

    monkeypatch.setattr(guard, "MAX_FILE_BYTES", 10)
    monkeypatch.setattr(guard, "MAX_TOTAL_BYTES", 7)
    violations = guard.collect_violations(["README.md", "LICENSE"], tmp_path)
    assert ("<release>", "total-size-limit-exceeded") in violations

    monkeypatch.setattr(guard, "MAX_FILE_COUNT", 1)
    violations = guard.collect_violations(["README.md", "LICENSE"], tmp_path)
    assert ("<release>", "file-count-limit-exceeded") in violations


def test_outside_symlink_is_rejected(tmp_path):
    repository = tmp_path / "repository"
    outside = tmp_path / "outside"
    repository.mkdir()
    _write(outside, "client.py")
    try:
        os.symlink(outside, repository / "pta", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("creating directory symlinks is unavailable")

    violations = guard.collect_violations(["pta/client.py"], repository)
    assert (
        "pta/client.py",
        "symlink-or-junction-outside-repository",
    ) in violations


def test_internal_junction_or_reparse_point_is_not_silently_followed(tmp_path, monkeypatch):
    _write(tmp_path, "pta/client.py")
    junction = tmp_path / "pta"
    monkeypatch.setattr(
        guard,
        "_is_junction_or_reparse",
        lambda path: path == junction,
    )

    violations = guard.collect_violations(["pta/client.py"], tmp_path)
    assert ("pta/client.py", "symlink-or-junction-not-allowed") in violations


def test_credential_rule_output_never_prints_matched_value(tmp_path, monkeypatch, capsys):
    secret = "gh" + "p_" + "A" * 30
    _write(tmp_path, "README.md", f"token={secret}\n")
    monkeypatch.setattr(guard, "ROOT", tmp_path)

    assert guard.main(["--candidate", "README.md"]) == 1
    captured = capsys.readouterr()
    assert "github-token" in captured.out
    assert secret not in captured.out
    assert secret not in captured.err


def test_staged_and_default_modes_work_before_first_commit_and_ignore_local_data(
    tmp_path, monkeypatch, capsys
):
    _git(tmp_path, "init", "-q")
    _write(tmp_path, ".gitignore", "config.json\n")
    _write(tmp_path, "README.md")
    ignored_secret = "gh" + "p_" + "B" * 30
    _write(tmp_path, "config.json", ignored_secret)
    _git(tmp_path, "add", "--", ".gitignore", "README.md")
    monkeypatch.setattr(guard, "ROOT", tmp_path)

    assert guard.main(["--staged"]) == 0
    assert guard.main([]) == 0
    captured = capsys.readouterr()
    assert ignored_secret not in captured.out
    assert ignored_secret not in captured.err


def test_default_mode_checks_worktree_as_well_as_safe_staged_blob(tmp_path, monkeypatch, capsys):
    _git(tmp_path, "init", "-q")
    _write(tmp_path, "README.md", "safe\n")
    _git(tmp_path, "add", "--", "README.md")
    secret = "gh" + "p_" + "C" * 30
    _write(tmp_path, "README.md", secret)
    monkeypatch.setattr(guard, "ROOT", tmp_path)

    assert guard.main([]) == 1
    captured = capsys.readouterr()
    assert "github-token" in captured.out
    assert secret not in captured.out


def test_explicit_utf8_candidate_list_supports_untracked_initial_review(
    tmp_path, monkeypatch, capsys
):
    _write(tmp_path, "README.md")
    manifest = tmp_path / "reviewed-files.txt"
    manifest.write_text("README.md\n", encoding="utf-8")
    monkeypatch.setattr(guard, "ROOT", tmp_path)

    assert guard.main(["--candidates-from", str(manifest)]) == 0
    assert "checked 1 explicit candidate files" in capsys.readouterr().out
