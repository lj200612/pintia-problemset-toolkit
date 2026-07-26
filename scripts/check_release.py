#!/usr/bin/env python3
"""Verify that a proposed public tree stays inside the release boundary.

The default mode checks every file in Git's index (tracked and staged), plus
the corresponding worktree files.  ``--staged`` checks only staged additions
and modifications from the index.  Explicit candidate modes exist for the
first, still-untracked review of a repository.

Only file names and generic rule names are printed.  File contents and matched
credential values are never included in output.
"""

from __future__ import annotations

import argparse
import re
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]

# These limits are intentionally well above the current public tree while
# still preventing an accidentally staged archive, bundle, or generated dump.
MAX_FILE_COUNT = 500
MAX_FILE_BYTES = 1 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_PATH_CHARACTERS = 240

ALLOWED_ROOT_FILES = frozenset(
    {
        ".editorconfig",
        ".env.example",
        ".gitattributes",
        ".gitignore",
        "CHANGELOG.md",
        "CODE_OF_CONDUCT.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "OPEN_SOURCE_TODO.md",
        "PRIVACY.md",
        "README.md",
        "SECURITY.md",
        "THIRD_PARTY_NOTICES.md",
        "config.example.json",
        "pta_fetch.py",
        "pta_list.py",
        "pta_submit.py",
        "pta_web.py",
        "pyproject.toml",
        "requirements-browser.txt",
        "requirements-dev.txt",
        "requirements.txt",
        "uv.lock",
    }
)
ALLOWED_PLACEHOLDERS = frozenset({"answers/.gitkeep", "problems/.gitkeep"})
ALLOWED_DOC_SUFFIXES = frozenset({".md", ".rst", ".txt"})
ALLOWED_WEB_SUFFIXES = frozenset({".css", ".html", ".js"})
ALLOWED_GITHUB_SUFFIXES = frozenset({".md", ".yaml", ".yml"})

# Binary web assets must be reviewed and added here by exact repository path.
# Keeping this empty is deliberate: the current UI uses only UTF-8 HTML/CSS/JS
# and inline SVG.  An extension alone must never authorize binary content.
ALLOWED_BINARY_STATIC_FILES: frozenset[str] = frozenset()

# Patterns deliberately favor low false-positive rates.  Descriptions stay
# generic so CI output cannot reveal the matching value.
SENSITIVE_PATTERNS: Sequence[Tuple[str, re.Pattern[str]]] = (
    (
        "private-key-material",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    ),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    (
        "github-token",
        re.compile(r"\b(?:gh[opurs]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    ),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    (
        "openai-style-token",
        re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b"),
    ),
    (
        "bearer-credential",
        re.compile(r"(?i)\bauthorization\s*[:=]\s*['\"]?bearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    ),
    (
        "pta-session-cookie",
        re.compile(r"(?i)\bPTASession\s*=\s*[A-Za-z0-9_%+/=-]{16,}"),
    ),
    (
        "realistic-problem-set-id",
        re.compile(r"(?i)(?:problemSetId['\"]?\s*[:=]\s*['\"]|/problem-sets/)\d{12,}"),
    ),
    (
        "personal-windows-profile-path",
        re.compile(r"(?i)\b[A-Z]:\\Users\\(?!user(?:name)?(?:\\|\b)|<)[^\\\s]+\\"),
    ),
)

BINARY_SIGNATURES: Sequence[Tuple[str, Tuple[bytes, ...]]] = (
    (
        "image-content-not-allowed",
        (
            b"\x89PNG\r\n\x1a\n",
            b"\xff\xd8\xff",
            b"GIF87a",
            b"GIF89a",
            b"BM",
            b"\x00\x00\x01\x00",  # ICO
        ),
    ),
    ("executable-content-not-allowed", (b"MZ", b"\x7fELF")),
    ("archive-content-not-allowed", (b"PK\x03\x04", b"PK\x05\x06", b"7z\xbc\xaf'\x1c")),
    ("pdf-content-not-allowed", (b"%PDF-",)),
)

Violation = Tuple[str, str]
IndexEntry = Tuple[str, int]


class GitError(RuntimeError):
    """A deliberately detail-free Git error suitable for release output."""


def _run_git(arguments: Sequence[str], root: Path) -> bytes:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise GitError("Git is unavailable") from exc
    except subprocess.CalledProcessError as exc:
        # Never relay Git stderr: a path, remote, hook, or filter may include
        # information that does not belong in public CI logs.
        raise GitError("not a usable Git worktree/index") from exc
    return result.stdout


def _decode_git_names(raw: bytes) -> List[str]:
    return [item.decode("utf-8", "surrogateescape") for item in raw.split(b"\0") if item]


def git_tracked_files(root: Path = ROOT) -> List[str]:
    """Return the complete tracked/staged file set from Git's index."""

    return _decode_git_names(_run_git(["ls-files", "--cached", "-z", "--"], root))


def git_staged_files(root: Path = ROOT) -> List[str]:
    """Return staged additions, copies, modifications, and rename targets."""

    raw = _run_git(
        ["diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z", "--"],
        root,
    )
    return _decode_git_names(raw)


def git_index_entries(root: Path = ROOT) -> Mapping[str, List[IndexEntry]]:
    """Map an index path to its (mode, stage) records."""

    records: Dict[str, List[IndexEntry]] = {}
    raw = _run_git(["ls-files", "--stage", "-z", "--"], root)
    for item in raw.split(b"\0"):
        if not item or b"\t" not in item:
            continue
        metadata, encoded_name = item.split(b"\t", 1)
        fields = metadata.split()
        if len(fields) != 3:
            continue
        try:
            mode = fields[0].decode("ascii")
            stage = int(fields[2])
        except (UnicodeDecodeError, ValueError):
            continue
        name = encoded_name.decode("utf-8", "surrogateescape")
        records.setdefault(name, []).append((mode, stage))
    return records


def git_index_blob(name: str, root: Path = ROOT) -> bytes:
    """Read an exact staged blob without exposing Git errors."""

    return _run_git(["cat-file", "blob", f":{name}"], root)


def git_index_blob_size(name: str, root: Path = ROOT) -> int:
    """Read a staged blob's size before deciding whether to load it."""

    raw = _run_git(["cat-file", "-s", f":{name}"], root)
    try:
        size = int(raw.strip())
    except ValueError as exc:
        raise GitError("Git returned an invalid index object size") from exc
    if size < 0:
        raise GitError("Git returned an invalid index object size")
    return size


def _canonical_name(name: str) -> Tuple[str | None, str | None]:
    if not isinstance(name, str) or not name or "\0" in name:
        return None, "unsafe-repository-path"
    normalized = name.replace("\\", "/")
    if len(normalized) > MAX_PATH_CHARACTERS:
        return None, "path-length-limit-exceeded"
    if normalized.startswith(("/", "//")) or re.match(r"^[A-Za-z]:", normalized):
        return None, "unsafe-repository-path"
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None, "unsafe-repository-path"
    path = PurePosixPath(normalized)
    if path.is_absolute():
        return None, "unsafe-repository-path"
    return path.as_posix(), None


def _is_allowlisted_path(name: str) -> bool:
    if name in ALLOWED_ROOT_FILES or name in ALLOWED_PLACEHOLDERS:
        return True
    if name in ALLOWED_BINARY_STATIC_FILES:
        return True

    path = PurePosixPath(name)
    parts = path.parts
    suffix = path.suffix.lower()

    # Python modules are intentionally one level deep.  New package layouts
    # require an explicit release-boundary review instead of silently passing.
    if len(parts) == 2 and parts[0] in {"pta", "scripts", "tests"}:
        return suffix in {".py", ".pyi"}

    # Browser source is textual and kept directly under pta/web.
    if len(parts) == 3 and parts[:2] == ("pta", "web"):
        return suffix in ALLOWED_WEB_SUFFIXES

    # License attributions are small UTF-8 text files, never downloaded blobs.
    if len(parts) == 2 and parts[0] == "licenses":
        return suffix == ".txt"

    # Documentation may be grouped below docs/, but images and archives are
    # deliberately excluded even when a Markdown page references them.
    if len(parts) >= 2 and parts[0] == "docs":
        return suffix in ALLOWED_DOC_SUFFIXES

    # GitHub metadata is limited to textual Markdown/YAML in its conventional
    # root, workflow, and issue-template locations.
    if parts and parts[0] == ".github" and suffix in ALLOWED_GITHUB_SUFFIXES:
        if len(parts) == 2:
            return True
        if len(parts) == 3 and parts[1] in {"ISSUE_TEMPLATE", "workflows"}:
            return True

    return False


def path_rules(name: str) -> Iterable[str]:
    canonical, error = _canonical_name(name)
    if error:
        yield error
        return
    assert canonical is not None
    if not _is_allowlisted_path(canonical):
        yield "path-not-allowlisted"


def _looks_like_utf16_without_bom(raw: bytes) -> bool:
    sample = raw[:1024]
    # A single NUL in a short binary fragment is not enough evidence of
    # UTF-16; require several code units before applying the alternating-NUL
    # heuristic.
    if len(sample) < 8:
        return False
    even = sample[0::2]
    odd = sample[1::2]
    even_nuls = even.count(0) / max(1, len(even))
    odd_nuls = odd.count(0) / max(1, len(odd))
    return (even_nuls >= 0.30 and odd_nuls <= 0.05) or (odd_nuls >= 0.30 and even_nuls <= 0.05)


def content_rules(name: str, raw: bytes) -> Iterable[str]:
    """Validate one allowlisted payload without ever returning its content."""

    if len(raw) > MAX_FILE_BYTES:
        yield "file-size-limit-exceeded"
        return
    if name in ALLOWED_BINARY_STATIC_FILES:
        return

    if raw.startswith((b"\xff\xfe", b"\xfe\xff", b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        yield "utf-16-or-32-text-not-allowed"
        return
    if _looks_like_utf16_without_bom(raw):
        yield "utf-16-or-32-text-not-allowed"
        return

    for rule, signatures in BINARY_SIGNATURES:
        if raw.startswith(signatures):
            yield rule
            return
    if len(raw) >= 12 and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        yield "image-content-not-allowed"
        return
    if b"\0" in raw:
        yield "binary-content-not-allowed"
        return

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        yield "non-utf8-text-not-allowed"
        return

    if any(
        (ord(character) < 32 and character not in "\t\n\r\f") or ord(character) == 127
        for character in text
    ):
        yield "binary-control-characters"
        return

    for rule, pattern in SENSITIVE_PATTERNS:
        if pattern.search(text):
            yield rule


def _is_junction_or_reparse(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None:
        try:
            if is_junction():
                return True
        except OSError:
            return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def link_rules(root: Path, name: str) -> Iterable[str]:
    """Reject symlinks/junctions and specifically identify escapes."""

    canonical, error = _canonical_name(name)
    if error or canonical is None:
        return
    root_resolved = root.resolve(strict=False)
    current = root
    try:
        for part in PurePosixPath(canonical).parts:
            current = current / part
            if current.is_symlink() or _is_junction_or_reparse(current):
                resolved = current.resolve(strict=False)
                if not _is_within(resolved, root_resolved):
                    yield "symlink-or-junction-outside-repository"
                else:
                    yield "symlink-or-junction-not-allowed"
                return

        resolved_target = current.resolve(strict=False)
        if not _is_within(resolved_target, root_resolved):
            yield "symlink-or-junction-outside-repository"
    except (OSError, RuntimeError):
        yield "unreadable-or-looping-path"


def _validated_names(names: Sequence[str]) -> Tuple[List[str], List[Violation]]:
    raw_names = list(names)
    violations: List[Violation] = []
    if len(raw_names) > MAX_FILE_COUNT:
        violations.append(("<release>", "file-count-limit-exceeded"))
        raw_names = raw_names[:MAX_FILE_COUNT]

    valid: List[str] = []
    seen = set()
    for original in raw_names:
        canonical, canonical_error = _canonical_name(original)
        if canonical_error or canonical is None:
            violations.append((original, canonical_error or "unsafe-repository-path"))
            continue
        if canonical in seen:
            continue
        seen.add(canonical)
        rules = list(path_rules(canonical))
        if rules:
            violations.extend((canonical, rule) for rule in rules)
            continue
        valid.append(canonical)
    return valid, violations


def collect_worktree_violations(names: Sequence[str], root: Path = ROOT) -> List[Violation]:
    valid_names, violations = _validated_names(names)
    total_bytes = 0

    for name in valid_names:
        found_link_rules = list(link_rules(root, name))
        if found_link_rules:
            violations.extend((name, rule) for rule in found_link_rules)
            continue

        disk_path = root.joinpath(*PurePosixPath(name).parts)
        try:
            if not disk_path.exists():
                violations.append((name, "missing-candidate-file"))
                continue
            if not disk_path.is_file():
                violations.append((name, "candidate-is-not-regular-file"))
                continue
            size = disk_path.stat().st_size
        except OSError:
            violations.append((name, "unreadable-candidate-file"))
            continue

        total_bytes += size
        if size > MAX_FILE_BYTES:
            violations.append((name, "file-size-limit-exceeded"))
            continue
        try:
            # Bound the read even if the file grows after the stat call.
            with disk_path.open("rb") as handle:
                raw = handle.read(MAX_FILE_BYTES + 1)
        except OSError:
            violations.append((name, "unreadable-candidate-file"))
            continue
        violations.extend((name, rule) for rule in content_rules(name, raw))

    if total_bytes > MAX_TOTAL_BYTES:
        violations.append(("<release>", "total-size-limit-exceeded"))
    return sorted(set(violations))


def collect_index_violations(names: Sequence[str], root: Path = ROOT) -> List[Violation]:
    valid_names, violations = _validated_names(names)
    total_bytes = 0
    try:
        entries = git_index_entries(root)
    except GitError:
        return sorted(set([*violations, ("<release>", "unreadable-git-index")]))

    for name in valid_names:
        violations.extend((name, rule) for rule in link_rules(root, name))
        path_entries = entries.get(name, [])
        if not path_entries:
            violations.append((name, "missing-index-entry"))
            continue
        if len(path_entries) != 1 or path_entries[0][1] != 0:
            violations.append((name, "unmerged-index-entry"))
            continue
        mode = path_entries[0][0]
        if mode == "120000":
            violations.append((name, "tracked-symlink-not-allowed"))
            continue
        if mode == "160000":
            violations.append((name, "git-submodule-not-allowed"))
            continue
        if mode not in {"100644", "100755"}:
            violations.append((name, "unsupported-index-file-mode"))
            continue
        try:
            size = git_index_blob_size(name, root)
        except GitError:
            violations.append((name, "unreadable-index-file"))
            continue
        total_bytes += size
        if size > MAX_FILE_BYTES:
            violations.append((name, "file-size-limit-exceeded"))
            continue
        try:
            raw = git_index_blob(name, root)
        except GitError:
            violations.append((name, "unreadable-index-file"))
            continue
        if len(raw) != size:
            violations.append((name, "unreadable-index-file"))
            continue
        violations.extend((name, rule) for rule in content_rules(name, raw))

    if total_bytes > MAX_TOTAL_BYTES:
        violations.append(("<release>", "total-size-limit-exceeded"))
    return sorted(set(violations))


def collect_violations(names: Sequence[str], root: Path = ROOT) -> List[Violation]:
    """Backward-compatible helper for explicit worktree candidates."""

    return collect_worktree_violations(names, root)


def _candidate_file_names(source: str) -> List[str]:
    try:
        if source == "-":
            text = sys.stdin.read()
        else:
            text = Path(source).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("candidate list is unreadable or not UTF-8") from exc
    return [line for line in text.splitlines() if line]


def _safe_display_path(name: str) -> str:
    rendered = []
    for character in name[:MAX_PATH_CHARACTERS]:
        if character.isprintable() and character not in "\r\n\t":
            rendered.append(character)
        else:
            rendered.append(f"\\u{ord(character):04x}")
    if len(name) > MAX_PATH_CHARACTERS:
        rendered.append("...")
    return "".join(rendered)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check an explicit, staged, or complete Git release candidate."
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--staged",
        action="store_true",
        help="check staged additions/modifications from the Git index",
    )
    source.add_argument(
        "--candidate",
        nargs="+",
        metavar="PATH",
        help="check only these explicit repository-relative worktree paths",
    )
    source.add_argument(
        "--candidates-from",
        "--candidate-list",
        metavar="FILE",
        help="read repository-relative candidate paths from a UTF-8 line list (or -)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    try:
        if args.candidate is not None:
            names = list(args.candidate)
            mode = "explicit candidate"
            violations = collect_worktree_violations(names, ROOT)
        elif args.candidates_from is not None:
            names = _candidate_file_names(args.candidates_from)
            mode = "explicit candidate"
            violations = collect_worktree_violations(names, ROOT)
        elif args.staged:
            names = git_staged_files(ROOT)
            mode = "staged"
            violations = collect_index_violations(names, ROOT)
        else:
            names = git_tracked_files(ROOT)
            mode = "Git tracked/staged"
            # The index is the prospective public tree, while the worktree is
            # what build tools consume.  The default release gate checks both.
            violations = sorted(
                set(
                    collect_index_violations(names, ROOT) + collect_worktree_violations(names, ROOT)
                )
            )
    except (GitError, RuntimeError) as exc:
        print(f"[release-check] ERROR: {exc}", file=sys.stderr)
        return 2

    if not names:
        print(f"[release-check] ERROR: no {mode} files to check", file=sys.stderr)
        return 2

    if violations:
        print("[release-check] Refusing release; files violate the public boundary:")
        for name, rule in violations:
            print(f"  - {_safe_display_path(name)}: {rule}")
        print("[release-check] Matched values and file contents are intentionally omitted.")
        return 1

    print(f"[release-check] OK: checked {len(set(names))} {mode} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
