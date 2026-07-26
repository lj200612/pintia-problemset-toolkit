# Open-source release TODO

> Status on 2026-07-26: **technical hardening is complete, but do not publish
> yet**. The remaining blockers require a maintainer decision, a repository
> remote, or permission that cannot be inferred from the codebase.

## Manual blockers before publication

- [x] The project license is confirmed as MIT. `LICENSE`, `pyproject.toml`,
  `README.md`, `CONTRIBUTING.md`, and package metadata are aligned.
- [ ] Confirm that the intended PTA/Pintia automation is permitted by the
  platform and by the relevant school, course, and problem-set organizer. No
  public API authorization or usable public automation terms were verified;
  the absence of a prohibition is not permission.
- [x] The public GitHub repository exists and its final URL is included in
  package metadata.
- [ ] Enable GitHub private vulnerability reporting, or publish a concrete
  private security contact.
- [ ] Require a green first hosted CI run. CI covers Python 3.10–3.13 on Linux
  plus Python 3.13 on Windows and macOS.
- [ ] Only after the permission and CI items above: tag `0.1.0` and consider a
  package release. Do not upload the package to PyPI first.

## Completed technical closeout

- [x] Local/private content is ignored and excluded: real configuration,
  answers, fetched problem statements, logs, screenshots/previews, binaries,
  editor state, and private API notes.
- [x] WebUI is loopback-only and enforces strict Host/Origin checks, a random
  per-process API token, no-store/CSP browser headers, SSE-token log redaction,
  safe answer paths, and app/config-scoped caches.
- [x] CLI/Web persistent logs keep only sanitized error metadata; remote error
  response bodies and submitted source cannot enter JSONL logs.
- [x] Fetched and local-answer IDs require ASCII digits, and resolved paths are
  contained under their configured output roots.
- [x] GET caches include the complete effective authentication-header identity,
  use non-lossy SHA-256 keys, expire entries, and have a 256-entry LRU bound.
- [x] Inline icons were matched to `lucide-static` 1.27.0; Lucide ISC and
  Feather MIT notices are included in `licenses/LUCIDE.txt` and the wheel.
- [x] The release guard is allowlist-oriented and checks index/worktree paths,
  file count and size, UTF-8 encoding, binary signatures, links/junctions, and
  common credential or personal-data patterns without printing matched values.
- [x] Ruff formatting/Lint, Mypy, Python compilation, JavaScript syntax, and
  `uv lock --check` pass. These checks are also configured in CI.
- [x] Full offline test suite passes: `79 passed, 1 skipped` on Python 3.10.18.
  The skip is the expected Windows environment without symlink permission.
- [x] `pip-audit -r requirements-browser.txt --strict` reports no known
  vulnerabilities; the locked environment also passes dependency consistency
  checks.
- [x] Final sdist/wheel build and Twine metadata checks pass. All 17 packaged
  source/static files match the workspace byte-for-byte; private paths are
  absent from both archives.
- [x] The wheel installs in a clean Python 3.10 environment; all four CLI
  `--help` commands, installed static assets, API-token enforcement, and Flask
  index/status smoke checks pass without contacting PTA/Pintia.
- [x] The explicit 54-file public allowlist is staged and passes the release
  guard. The pre-existing remote history contained only the reviewed MIT
  `LICENSE` file.

## Maintainer release commands

Run these only after resolving every manual blocker above:

```powershell
uv sync --locked --all-extras
uv run ruff format --check .
uv run ruff check .
uv run python -m mypy
uv run python -m pytest -p no:cacheprovider
uv run python -m pip_audit -r requirements-browser.txt --strict
python scripts/check_release.py
git diff --cached --check
git diff --cached --name-only
```

Do not run real PTA submissions during release verification. Tests and smoke
checks must use mocks, temporary directories, empty credentials, and
`example.invalid`.
