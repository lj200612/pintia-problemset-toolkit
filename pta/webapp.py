"""WebUI 后端：Flask 应用，封装登录 / 题集 / 题目 / 提交 / 判题结果接口。

复用 pta/client.py 等模块；配置（含 cookie）持久化到 config.json。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from flask import Flask, Response, current_app, jsonify, request, stream_with_context
from werkzeug.exceptions import HTTPException

from .auth import PlaywrightNotInstalled, browser_login
from .client import PTAAuthError, PTAClient, PTAError, extract_current_user
from .config import Config, load_config_or_default, update_config_file
from .discovery import auto_configure_problem_set
from .fetcher import _first, extract_problem
from .submitter import (
    ProblemMeta,
    apply_status,
    available_compilers,
    build_catalog,
    build_payload,
    extract_submission_id,
    find_problem_status,
    normalize_status,
    scan_answers,
    select_targets,
    summarize_submission_result,
    validate_exam,
    wait_for_submission_result,
)

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
PROBLEM_SET_CACHE_SECONDS = 60.0
_WEB_AUDIT_LOCK = threading.Lock()
_TOKEN_FILTER_INSTALL_LOCK = threading.Lock()

API_TOKEN_QUERY_PARAM = "_token"
ALLOWED_ANSWER_EXTENSIONS = frozenset({".c", ".cc", ".cpp", ".cxx", ".java", ".py"})
_API_TOKEN_QUERY_RE = re.compile(r"(?i)(?P<prefix>[?&](?:_|%5f)token(?:=|%3d))[^&\s#\"']*")


class _TokenRedactionFilter(logging.Filter):
    """Prevent the SSE query token from appearing in Werkzeug access logs."""

    def _redact(self, value: Any) -> Any:
        if isinstance(value, str):
            return _API_TOKEN_QUERY_RE.sub(
                lambda match: match.group("prefix") + "[REDACTED]", value
            )
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self._redact(value) for value in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: self._redact(value) for key, value in record.args.items()}
        return True


_TOKEN_REDACTION_FILTER = _TokenRedactionFilter()


def _install_token_redaction_filter() -> None:
    logger = logging.getLogger("werkzeug")
    with _TOKEN_FILTER_INSTALL_LOCK:
        if _TOKEN_REDACTION_FILTER not in logger.filters:
            logger.addFilter(_TOKEN_REDACTION_FILTER)


def _json_body() -> Dict[str, Any]:
    """Return a JSON object without turning a wrong content type into a 500."""

    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _authority_hostname(authority: str) -> str:
    value = (authority or "").strip()
    if not value or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        return ""
    if any(char in value for char in ("/", "\\", "?", "#")):
        return ""
    try:
        parts = urlsplit("//" + value)
        # Accessing ``port`` also validates malformed/non-numeric/out-of-range ports.
        _ = parts.port
    except ValueError:
        return ""
    if parts.username is not None or parts.password is not None:
        return ""
    return parts.hostname or ""


def _is_loopback_hostname(hostname: str) -> bool:
    hostname = (hostname or "").strip().lower()
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _effective_port(parts: Any) -> Optional[int]:
    try:
        if parts.port is not None:
            return parts.port
    except ValueError:
        return None
    return (
        443 if parts.scheme.lower() == "https" else 80 if parts.scheme.lower() == "http" else None
    )


def _origin_matches_request(origin: str) -> bool:
    try:
        supplied = urlsplit(origin)
        expected = urlsplit(request.host_url)
    except ValueError:
        return False
    if supplied.scheme.lower() not in {"http", "https"}:
        return False
    if not supplied.hostname or not expected.hostname:
        return False
    if supplied.path not in {"", "/"} or supplied.query or supplied.fragment:
        return False
    return (
        supplied.scheme.lower() == expected.scheme.lower()
        and supplied.hostname.lower() == expected.hostname.lower()
        and _effective_port(supplied) == _effective_port(expected)
        and not supplied.username
        and not supplied.password
    )


def _has_api_token(expected: str) -> bool:
    authorization = request.headers.get("Authorization", "")
    scheme, separator, value = authorization.partition(" ")
    supplied = value.strip() if separator and scheme.lower() == "bearer" else ""
    if not supplied and request.path == "/api/overview-stream":
        supplied = request.args.get(API_TOKEN_QUERY_PARAM, "")
    return bool(supplied) and secrets.compare_digest(supplied, expected)


def _audit_web_submission(
    cfg: Config,
    *,
    event: str,
    problem_set_id: Any,
    pspid: Any,
    submission_id: Any = None,
    status: Any = None,
    error: Any = None,
) -> None:
    """Append a source-free, cookie-free Web submission audit event."""

    payload = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": "web",
        "event": str(event),
        "problemSetId": str(problem_set_id or ""),
        "problemSetProblemId": str(pspid or ""),
        "submissionId": str(submission_id) if submission_id else None,
        "status": str(status) if status else None,
        # Exception class / stable error code only: response bodies can echo submitted source.
        "error": str(error) if error else None,
    }
    log_dir = Path(cfg.log_dir).expanduser()
    log_path = log_dir / f"submit-web-{time.strftime('%Y%m%d')}.jsonl"
    try:
        with _WEB_AUDIT_LOCK:
            log_dir.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(json.dumps(payload, ensure_ascii=False) + "\n")
                log_file.flush()
    except OSError as exc:
        logging.getLogger(__name__).warning(
            "Unable to write the Web submission audit log (%s)", type(exc).__name__
        )


def _json_line(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False) + "\n"


def _sse(payload: Dict[str, Any]) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _problem_payload(catalog: Dict[str, ProblemMeta]) -> List[Dict[str, Any]]:
    return [
        {
            "pspid": m.pspid,
            "label": m.label,
            "title": m.title,
            "type": m.ptype,
            "score": m.score,
            "status": m.status,
            "index": m.index_in_pool,
        }
        for m in sorted(catalog.values(), key=lambda x: x.pspid)
    ]


def _web_state() -> Dict[str, Any]:
    state = current_app.extensions.get("pta_web")
    if not isinstance(state, dict):
        raise RuntimeError("PTA Web application state is not initialized")
    return state


def _fetch_executor() -> ThreadPoolExecutor:
    executor = _web_state().get("fetch_executor")
    if not isinstance(executor, ThreadPoolExecutor):
        raise RuntimeError("PTA Web fetch executor is not initialized")
    return executor


def _problem_sets_cache_key(cfg: Config, scope: str) -> Tuple[str, str, str, str]:
    credential_material = json.dumps(
        {
            "cookie": cfg.cookie or "",
            "extraHeaders": cfg.extra_headers or {},
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    credential_sig = hashlib.sha256(credential_material.encode("utf-8")).hexdigest()
    config_identity = os.path.normcase(os.path.realpath(_config_path()))
    return config_identity, cfg.base_url.rstrip("/"), credential_sig, scope


def _problem_sets_cache_get(cfg: Config, scope: str) -> Optional[Dict[str, Any]]:
    state = _web_state()
    key = _problem_sets_cache_key(cfg, scope)
    now = time.time()
    with state["problem_sets_cache_lock"]:
        cache = state["problem_sets_cache"]
        hit = cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
        if hit:
            cache.pop(key, None)
    return None


def _problem_sets_cache_set(cfg: Config, scope: str, payload: Dict[str, Any]) -> None:
    state = _web_state()
    key = _problem_sets_cache_key(cfg, scope)
    now = time.time()
    with state["problem_sets_cache_lock"]:
        cache = state["problem_sets_cache"]
        for expired_key, (expires_at, _value) in list(cache.items()):
            if expires_at <= now:
                cache.pop(expired_key, None)
        cache[key] = (now + PROBLEM_SET_CACHE_SECONDS, payload)


def _scan_answers_within_root(answers_dir: str) -> Dict[str, Any]:
    """Ignore answer entries that resolve outside the configured answers directory."""

    root = Path(answers_dir).expanduser().resolve()
    safe_answers: Dict[str, Any] = {}
    for pspid, answer in scan_answers(str(root)).items():
        try:
            resolved = Path(answer.path).resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            continue
        if not resolved.is_file():
            continue
        # Read the already-resolved path later, rather than following the directory entry again.
        answer.path = str(resolved)
        safe_answers[pspid] = answer
    return safe_answers


def _overview_base(
    psid: str,
) -> Tuple[
    Config,
    Any,
    Dict[str, ProblemMeta],
    Dict[str, Any],
    str,
    Optional[str],
    Optional[bool],
    List[str],
    Dict[str, Any],
]:
    cfg = _cfg()
    cfg.problem_set_id = str(psid)
    executor = _fetch_executor()
    exam_future = executor.submit(PTAClient(cfg).get_exam)
    types_future = executor.submit(PTAClient(cfg).get_problem_types)
    status_future = executor.submit(PTAClient(cfg).get_exam_problem_status)
    exam_resp = exam_future.result()
    exam_id, exam_status, exam_ended = validate_exam(exam_resp)
    compilers = available_compilers(exam_resp)
    ps = exam_resp.get("problemSet") or {}
    catalog = build_catalog(types_future.result())
    status_resp = status_future.result()
    return cfg, exam_resp, catalog, status_resp, exam_id, exam_status, exam_ended, compilers, ps


def _cfg() -> Config:
    return load_config_or_default(str(current_app.config["PTA_CONFIG_PATH"]))


def _config_path() -> str:
    return str(current_app.config["PTA_CONFIG_PATH"])


def _client_for(psid: Optional[str] = None) -> PTAClient:
    cfg = _cfg()
    if psid:
        cfg.problem_set_id = str(psid)
    return PTAClient(cfg)


def _status_payload(cfg: Optional[Config] = None) -> Dict[str, Any]:
    cfg = cfg or _cfg()
    payload: Dict[str, Any] = {
        "loggedIn": False,
        "user": None,
        "problemSetId": cfg.problem_set_id,
        "answersDir": cfg.answers_dir,
        "defaultCompiler": cfg.default_compiler,
    }
    if cfg.cookie:
        client = PTAClient(cfg)
        try:
            u = extract_current_user(client.get_current_user())
            if u:
                if not cfg.problem_set_id:
                    try:
                        auto_configure_problem_set(cfg, _config_path())
                        payload["problemSetId"] = cfg.problem_set_id
                    except PTAError:
                        pass
                payload["loggedIn"] = True
                payload["user"] = {
                    "id": u.get("id"),
                    "nickname": u.get("nickname") or u.get("name") or u.get("email") or "PTA 用户",
                }
                return payload
        except PTAError:
            pass

        # 某些登录态只对主站接口有效、对 passport 的 current 接口无效；
        # 这时回退到题集考试接口探活，避免 WebUI 误判为“未登录”。
        if cfg.problem_set_id:
            try:
                exam_resp = client.get_exam()
                exam = exam_resp.get("exam") or {}
                if exam.get("id"):
                    payload["loggedIn"] = True
                    payload["user"] = {"id": None, "nickname": "PTA 用户"}
            except PTAError:
                payload["loggedIn"] = False
    return payload


def api_route(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except PTAAuthError as exc:
            return jsonify({"error": str(exc), "auth": True}), 401
        except PlaywrightNotInstalled as exc:
            return jsonify({"error": str(exc)}), 500
        except TimeoutError as exc:
            return jsonify({"error": str(exc)}), 504
        except PTAError as exc:
            return jsonify({"error": str(exc)}), 502
        except HTTPException as exc:
            return jsonify({"error": exc.description}), exc.code or 500
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500

    return wrapper


def create_app(config_path: str = "config.json") -> Flask:
    app = Flask(__name__, static_folder=WEB_DIR, static_url_path="")
    app.config["PTA_CONFIG_PATH"] = os.path.abspath(config_path)
    app.extensions["pta_web"] = {
        "fetch_executor": ThreadPoolExecutor(
            max_workers=8, thread_name_prefix=f"pta-web-{id(app):x}"
        ),
        "problem_sets_cache": {},
        "problem_sets_cache_lock": threading.RLock(),
    }
    api_token = secrets.token_urlsafe(32)
    app.config["PTA_API_TOKEN"] = api_token
    _install_token_redaction_filter()

    @app.before_request
    def enforce_local_security():
        hostname = _authority_hostname(request.host)
        if not _is_loopback_hostname(hostname):
            if request.path.startswith("/api/"):
                return jsonify({"error": "仅允许通过本机回环地址访问 WebUI"}), 403
            return Response("Forbidden", status=403, mimetype="text/plain")

        origin = request.headers.get("Origin")
        if origin and not _origin_matches_request(origin):
            if request.path.startswith("/api/"):
                return jsonify({"error": "请求 Origin 与本地 WebUI 不匹配"}), 403
            return Response("Forbidden", status=403, mimetype="text/plain")

        if request.path.startswith("/api/") and not _has_api_token(api_token):
            return jsonify({"error": "缺少或无效的本地 API 访问令牌"}), 401
        return None

    @app.after_request
    def add_browser_security_headers(response: Response):
        if request.path == "/" or request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "; ".join(
                (
                    "default-src 'self'",
                    "base-uri 'none'",
                    "connect-src 'self'",
                    "font-src 'self'",
                    "form-action 'none'",
                    "frame-ancestors 'none'",
                    "img-src 'self' data: https:",
                    "object-src 'none'",
                    "script-src 'self'",
                    "style-src 'self' 'unsafe-inline'",
                )
            ),
        )
        return response

    # ---------- 静态页面 ----------
    @app.route("/")
    def index():
        with open(os.path.join(WEB_DIR, "index.html"), "r", encoding="utf-8") as source:
            page = source.read()
        token_meta = f'<meta name="pta-api-token" content="{api_token}">'
        page = page.replace("</head>", f"  {token_meta}\n</head>", 1)
        response = Response(page, mimetype="text/html")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    # ---------- 登录 / 状态 ----------
    @app.route("/api/status")
    @api_route
    def status():
        return jsonify(_status_payload())

    @app.route("/api/login/manual", methods=["POST"])
    @api_route
    def login_manual():
        cookie = str(_json_body().get("cookie") or "").strip()
        if not cookie:
            return jsonify({"error": "cookie 不能为空"}), 400
        cfg = _cfg()
        cfg.cookie = cookie
        payload = _status_payload(cfg)
        if not payload["loggedIn"]:
            return jsonify({"error": "cookie 未通过登录校验，已拒绝保存。"}), 401
        update_config_file(_config_path(), {"cookie": cookie})
        return jsonify(payload)

    @app.route("/api/login/browser", methods=["POST"])
    @api_route
    def login_browser():
        data = _json_body()
        cookie = browser_login(
            email=data.get("email", "").strip(),
            password=data.get("password", ""),
            timeout=float(data.get("timeout", 240)),
        )
        cfg = _cfg()
        cfg.cookie = cookie
        payload = _status_payload(cfg)
        if not payload["loggedIn"]:
            return jsonify({"error": "浏览器返回的 cookie 未通过登录校验，已拒绝保存。"}), 401
        update_config_file(_config_path(), {"cookie": cookie})
        return jsonify(payload)

    @app.route("/api/logout", methods=["POST"])
    @api_route
    def logout():
        update_config_file(_config_path(), {"cookie": ""})
        return jsonify(_status_payload())

    @app.route("/api/config", methods=["POST"])
    @api_route
    def set_config():
        data = _json_body()
        updates: Dict[str, Any] = {}
        if "problemSetId" in data:
            updates["problemSetId"] = str(data["problemSetId"]).strip()
        if "defaultCompiler" in data:
            updates["defaultCompiler"] = str(data["defaultCompiler"]).strip()
        if updates:
            update_config_file(_config_path(), updates)
        cfg = _cfg()
        return jsonify(
            {
                "ok": True,
                "problemSetId": cfg.problem_set_id,
                "answersDir": cfg.answers_dir,
                "defaultCompiler": cfg.default_compiler,
            }
        )

    # ---------- 题集发现 ----------
    @app.route("/api/problem-sets")
    @api_route
    def problem_sets():
        scope = request.args.get("scope", "active")
        if scope not in {"active", "all", "available"}:
            return jsonify({"error": "scope 必须是 active、all 或 available"}), 400
        refresh = request.args.get("refresh") == "1"
        cfg = _cfg()
        cached = None if refresh else _problem_sets_cache_get(cfg, scope)
        if cached:
            return jsonify(cached)
        client = PTAClient(cfg)
        if scope == "available":
            data = client.get_always_available_problem_sets()
        else:
            data = client.get_my_problem_sets(only_active=(scope == "active"))
        sets = data.get("problemSets") or []
        out = [
            {
                "id": str(s.get("id")),
                "name": s.get("name") or s.get("title") or str(s.get("id")),
                "endAt": s.get("endAt"),
            }
            for s in sets
        ]
        payload = {"problemSets": out, "cachedForSeconds": PROBLEM_SET_CACHE_SECONDS}
        _problem_sets_cache_set(cfg, scope, payload)
        return jsonify(payload)

    # ---------- 题集总览（考试 + 目录 + 状态）----------
    @app.route("/api/overview")
    @api_route
    def overview():
        psid = request.args.get("psid") or _cfg().problem_set_id
        if not psid:
            return jsonify({"error": "缺少 problemSetId"}), 400
        (
            _cfg_obj,
            _exam_resp,
            catalog,
            status_resp,
            exam_id,
            exam_status,
            exam_ended,
            compilers,
            ps,
        ) = _overview_base(str(psid))
        apply_status(catalog, status_resp)
        return jsonify(
            {
                "problemSetId": str(psid),
                "problemSetName": ps.get("name") or "",
                "examId": exam_id,
                "status": exam_status,
                "ended": exam_ended,
                "compilers": compilers,
                "problems": _problem_payload(catalog),
            }
        )

    @app.route("/api/overview-stream")
    def overview_stream():
        psid = request.args.get("psid") or _cfg().problem_set_id
        if not psid:
            return jsonify({"error": "缺少 problemSetId"}), 400

        def generate():
            try:
                cfg = _cfg()
                cfg.problem_set_id = str(psid)
                yield _sse({"phase": "start", "problemSetId": str(psid)})

                executor = _fetch_executor()
                exam_future = executor.submit(PTAClient(cfg).get_exam)
                types_future = executor.submit(PTAClient(cfg).get_problem_types)
                status_future = executor.submit(PTAClient(cfg).get_exam_problem_status)
                catalog = build_catalog(types_future.result())
                yield _sse(
                    {
                        "phase": "catalog",
                        "problemSetId": str(psid),
                        "problems": _problem_payload(catalog),
                    }
                )

                apply_status(catalog, status_future.result())
                yield _sse(
                    {
                        "phase": "status",
                        "problemSetId": str(psid),
                        "problems": _problem_payload(catalog),
                    }
                )

                exam_resp = exam_future.result()
                exam_id, exam_status, exam_ended = validate_exam(exam_resp)
                compilers = available_compilers(exam_resp)
                ps = exam_resp.get("problemSet") or {}
                base = {
                    "problemSetId": str(psid),
                    "problemSetName": ps.get("name") or "",
                    "examId": exam_id,
                    "status": exam_status,
                    "ended": exam_ended,
                    "compilers": compilers,
                }
                yield _sse({"phase": "exam", **base})
                yield _sse({"phase": "done", **base, "problems": _problem_payload(catalog)})
            except PTAAuthError as exc:
                yield _sse({"phase": "error", "error": str(exc), "auth": True})
            except PTAError as exc:
                yield _sse({"phase": "error", "error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                yield _sse({"phase": "error", "error": f"{type(exc).__name__}: {exc}"})

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ---------- 单题详情 ----------
    @app.route("/api/problem")
    @api_route
    def problem():
        psid = request.args.get("psid") or _cfg().problem_set_id
        pspid = request.args.get("pspid")
        if not psid or not pspid:
            return jsonify({"error": "缺少 psid 或 pspid"}), 400
        detail = _client_for(psid).get_problem_detail(pspid)
        p = extract_problem(detail)
        psp = detail.get("problemSetProblem") or {}
        local_answer = _scan_answers_within_root(_cfg().answers_dir).get(str(pspid))
        examples = []
        for ex in p["examples"]:
            if isinstance(ex, dict):
                examples.append(
                    {
                        "input": _first(ex, "input", "in", "testInput", "stdin", default=""),
                        "output": _first(
                            ex,
                            "output",
                            "out",
                            "testOutput",
                            "expectedOutput",
                            "stdout",
                            default="",
                        ),
                    }
                )
        return jsonify(
            {
                "id": p["id"],
                "title": p["title"],
                "type": p["type"],
                "score": p["score"],
                "difficulty": p["difficulty"],
                "compiler": p["compiler"] or psp.get("compiler"),
                "content": p["content"],
                "description": p["description"],
                "timeLimit": p["timeLimit"],
                "memoryLimit": p["memoryLimit"],
                "codeSizeLimit": p["codeSizeLimit"],
                "examples": examples,
                "indexInPool": psp.get("indexInProblemPool", 0),
                "lastSubmissionId": psp.get("lastSubmissionId"),
                "localAnswerExists": bool(local_answer),
            }
        )

    # ---------- 提交单题 ----------
    @app.route("/api/submit", methods=["POST"])
    @api_route
    def submit():
        data = _json_body()
        cfg = _cfg()
        psid = data.get("psid") or cfg.problem_set_id
        pspid = str(data.get("pspid") or "")
        code = str(data.get("code") or "")
        ptype = str(data.get("type") or "")
        index = data.get("index", 0)
        compiler = str(data.get("compiler") or cfg.default_compiler)
        if not psid or not pspid or not ptype:
            return jsonify({"error": "缺少 psid / pspid / type"}), 400
        if not pspid.isascii() or not pspid.isdigit():
            return jsonify({"error": "pspid 必须是纯数字"}), 400
        if not code.strip():
            return jsonify({"error": "代码为空"}), 400

        cfg.problem_set_id = str(psid)
        client = PTAClient(cfg)
        exam_resp = client.get_exam()
        exam_id, exam_status, exam_ended = validate_exam(exam_resp)
        if str(exam_status or "").upper() != "PROCESSING" or bool(exam_ended):
            return jsonify(
                {"error": f"考试状态为 {exam_status}、ended={exam_ended}，无法提交"}
            ), 400
        compilers = available_compilers(exam_resp)
        if ptype == "PROGRAMMING" and compilers and compiler not in compilers:
            return jsonify({"error": f"编译器 {compiler} 不在考试可用列表中"}), 400

        previous_problem_status = None
        try:
            row = find_problem_status(client.get_problem_status(exam_id), pspid)
            if row:
                previous_problem_status = row.get("problemSubmissionStatus")
        except PTAError:
            previous_problem_status = None

        meta = ProblemMeta(pspid=pspid, title="", ptype=ptype, score=None, index_in_pool=index)
        payload = build_payload(meta, code, compiler if ptype == "PROGRAMMING" else None)
        try:
            resp = client.submit(exam_id, payload)
        except Exception as exc:
            _audit_web_submission(
                cfg,
                event="submit",
                problem_set_id=psid,
                pspid=pspid,
                status="ERROR",
                error=type(exc).__name__,
            )
            raise
        sid = extract_submission_id(resp)
        if not sid:
            _audit_web_submission(
                cfg,
                event="submit",
                problem_set_id=psid,
                pspid=pspid,
                status="ERROR",
                error="NO_SUBMISSION_ID",
            )
            return jsonify({"error": "提交返回中没有 submissionId"}), 502
        _audit_web_submission(
            cfg,
            event="submit",
            problem_set_id=psid,
            pspid=pspid,
            submission_id=sid,
            status="SUBMITTED",
        )
        return jsonify(
            {
                "submissionId": sid,
                "examId": exam_id,
                "previousProblemStatus": previous_problem_status,
                "pspid": pspid,
            }
        )

    # ---------- 批量提交（answers/ 目录）----------
    @app.route("/api/batch-submit", methods=["POST"])
    @api_route
    def batch_submit():
        data = _json_body()
        psid = data.get("psid") or _cfg().problem_set_id
        include_accepted = bool(data.get("includeAccepted", False))
        if not psid:
            return jsonify({"error": "缺少 problemSetId"}), 400

        cfg = _cfg()
        cfg.problem_set_id = str(psid)
        client = PTAClient(cfg)
        exam_resp = client.get_exam()
        exam_id, exam_status, exam_ended = validate_exam(exam_resp)
        if str(exam_status or "").upper() != "PROCESSING" or bool(exam_ended):
            return jsonify(
                {"error": f"考试状态为 {exam_status}、ended={exam_ended}，无法提交"}
            ), 400
        compilers = available_compilers(exam_resp)

        catalog = build_catalog(client.get_problem_types())
        apply_status(catalog, client.get_problem_status(exam_id))
        answers = _scan_answers_within_root(cfg.answers_dir)
        targets, unknown, skipped = select_targets(catalog, answers, include_accepted, None)

        results: list[dict[str, Any]] = []
        for idx, (meta, ans) in enumerate(targets, 1):
            code = ans.read()
            compiler = cfg.compiler_for_extension(ans.ext) if meta.ptype == "PROGRAMMING" else None
            if compiler and compilers and compiler not in compilers:
                results.append(
                    {
                        "pspid": meta.pspid,
                        "title": meta.title,
                        "error": f"编译器 {compiler} 不在考试可用列表中",
                    }
                )
                _audit_web_submission(
                    cfg,
                    event="submit",
                    problem_set_id=psid,
                    pspid=meta.pspid,
                    status="ERROR",
                    error="INVALID_COMPILER",
                )
                continue
            try:
                payload = build_payload(meta, code, compiler)
                resp = client.submit(exam_id, payload)
                sid = extract_submission_id(resp)
                if not sid:
                    results.append(
                        {
                            "pspid": meta.pspid,
                            "title": meta.title,
                            "error": "提交返回中没有 submissionId",
                        }
                    )
                    _audit_web_submission(
                        cfg,
                        event="submit",
                        problem_set_id=psid,
                        pspid=meta.pspid,
                        status="ERROR",
                        error="NO_SUBMISSION_ID",
                    )
                    continue
                results.append(
                    {
                        "pspid": meta.pspid,
                        "title": meta.title,
                        "submissionId": sid,
                        "examId": exam_id,
                        "previousProblemStatus": meta.status,
                    }
                )
                _audit_web_submission(
                    cfg,
                    event="submit",
                    problem_set_id=psid,
                    pspid=meta.pspid,
                    submission_id=sid,
                    status="SUBMITTED",
                )
            except PTAError as exc:
                results.append({"pspid": meta.pspid, "title": meta.title, "error": str(exc)})
                _audit_web_submission(
                    cfg,
                    event="submit",
                    problem_set_id=psid,
                    pspid=meta.pspid,
                    status="ERROR",
                    error=type(exc).__name__,
                )
            if idx < len(targets) and cfg.submit_delay_seconds > 0:
                time.sleep(cfg.submit_delay_seconds)

        return jsonify(
            {
                "examId": exam_id,
                "results": results,
                "unknown": unknown,
                "skipped": [m.pspid for m in skipped],
            }
        )

    @app.route("/api/batch-submit-stream", methods=["POST"])
    def batch_submit_stream():
        data = _json_body()
        psid = data.get("psid") or _cfg().problem_set_id
        include_accepted = bool(data.get("includeAccepted", False))
        if not psid:
            return jsonify({"error": "缺少 problemSetId"}), 400

        def generate():
            try:
                cfg = _cfg()
                cfg.problem_set_id = str(psid)
                client = PTAClient(cfg)
                exam_resp = client.get_exam()
                exam_id, exam_status, exam_ended = validate_exam(exam_resp)
                if str(exam_status or "").upper() != "PROCESSING" or bool(exam_ended):
                    yield _json_line(
                        {
                            "phase": "error",
                            "error": f"考试状态为 {exam_status}、ended={exam_ended}，无法提交",
                            "fatal": True,
                        }
                    )
                    return
                compilers = available_compilers(exam_resp)

                catalog = build_catalog(client.get_problem_types())
                apply_status(catalog, client.get_problem_status(exam_id))
                answers = _scan_answers_within_root(cfg.answers_dir)
                targets, unknown, skipped = select_targets(catalog, answers, include_accepted, None)
                yield _json_line(
                    {
                        "phase": "prepared",
                        "examId": exam_id,
                        "total": len(targets),
                        "unknown": unknown,
                        "skipped": [m.pspid for m in skipped],
                    }
                )

                for idx, (meta, ans) in enumerate(targets, 1):
                    compiler = (
                        cfg.compiler_for_extension(ans.ext) if meta.ptype == "PROGRAMMING" else None
                    )
                    yield _json_line(
                        {
                            "phase": "submitting",
                            "index": idx,
                            "total": len(targets),
                            "pspid": meta.pspid,
                            "title": meta.title,
                        }
                    )
                    if compiler and compilers and compiler not in compilers:
                        _audit_web_submission(
                            cfg,
                            event="submit",
                            problem_set_id=psid,
                            pspid=meta.pspid,
                            status="ERROR",
                            error="INVALID_COMPILER",
                        )
                        yield _json_line(
                            {
                                "phase": "error",
                                "pspid": meta.pspid,
                                "title": meta.title,
                                "error": f"编译器 {compiler} 不在考试可用列表中",
                            }
                        )
                        continue
                    try:
                        payload = build_payload(meta, ans.read(), compiler)
                        resp = client.submit(exam_id, payload)
                        sid = extract_submission_id(resp)
                        if not sid:
                            _audit_web_submission(
                                cfg,
                                event="submit",
                                problem_set_id=psid,
                                pspid=meta.pspid,
                                status="ERROR",
                                error="NO_SUBMISSION_ID",
                            )
                            yield _json_line(
                                {
                                    "phase": "error",
                                    "pspid": meta.pspid,
                                    "title": meta.title,
                                    "error": "提交返回中没有 submissionId",
                                }
                            )
                            continue
                        _audit_web_submission(
                            cfg,
                            event="submit",
                            problem_set_id=psid,
                            pspid=meta.pspid,
                            submission_id=sid,
                            status="SUBMITTED",
                        )
                        yield _json_line(
                            {
                                "phase": "submitted",
                                "pspid": meta.pspid,
                                "title": meta.title,
                                "submissionId": sid,
                                "examId": exam_id,
                            }
                        )
                        result, _raw = wait_for_submission_result(
                            client,
                            exam_id,
                            sid,
                            cfg,
                            problem_set_problem_id=meta.pspid,
                            previous_problem_status=meta.status,
                        )
                        _audit_web_submission(
                            cfg,
                            event="result",
                            problem_set_id=psid,
                            pspid=meta.pspid,
                            submission_id=sid,
                            status=result.get("status") or "UNKNOWN",
                        )
                        yield _json_line(
                            {
                                "phase": "result",
                                "pspid": meta.pspid,
                                "title": meta.title,
                                "submissionId": sid,
                                "examId": exam_id,
                                **result,
                            }
                        )
                    except PTAError as exc:
                        _audit_web_submission(
                            cfg,
                            event="submit",
                            problem_set_id=psid,
                            pspid=meta.pspid,
                            status="ERROR",
                            error=type(exc).__name__,
                        )
                        yield _json_line(
                            {
                                "phase": "error",
                                "pspid": meta.pspid,
                                "title": meta.title,
                                "error": str(exc),
                            }
                        )

                    if idx < len(targets) and cfg.submit_delay_seconds > 0:
                        time.sleep(cfg.submit_delay_seconds)

                yield _json_line({"phase": "done", "examId": exam_id, "total": len(targets)})
            except PTAAuthError as exc:
                yield _json_line({"phase": "error", "error": str(exc), "auth": True, "fatal": True})
            except PTAError as exc:
                yield _json_line({"phase": "error", "error": str(exc), "fatal": True})
            except Exception as exc:  # noqa: BLE001
                yield _json_line(
                    {"phase": "error", "error": f"{type(exc).__name__}: {exc}", "fatal": True}
                )

        return Response(
            stream_with_context(generate()),
            mimetype="application/x-ndjson",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ---------- 判题结果轮询 ----------
    @app.route("/api/result")
    @api_route
    def result():
        exam_id = request.args.get("examId")
        sid = request.args.get("submissionId")
        psid = request.args.get("psid")
        pspid = request.args.get("pspid")
        previous_problem_status = request.args.get("previousProblemStatus")
        if not exam_id or not sid:
            return jsonify({"error": "缺少 examId 或 submissionId"}), 400
        cfg = _cfg()
        effective_psid = psid or cfg.problem_set_id
        if psid:
            cfg.problem_set_id = str(psid)
        client = PTAClient(cfg)
        try:
            data = client.get_submission(exam_id, sid)
        except Exception as exc:
            _audit_web_submission(
                cfg,
                event="result",
                problem_set_id=effective_psid,
                pspid=pspid,
                submission_id=sid,
                status="ERROR",
                error=type(exc).__name__,
            )
            raise
        result_payload = summarize_submission_result(data)
        problem_status_row = None
        previous_status = normalize_status(previous_problem_status)
        if (
            result_payload["pending"]
            and pspid
            and previous_status
            and previous_status != "PROBLEM_ACCEPTED"
        ):
            try:
                problem_status_row = find_problem_status(client.get_problem_status(exam_id), pspid)
            except PTAError:
                problem_status_row = None
        result_payload = summarize_submission_result(
            data,
            problem_status_row=problem_status_row,
            previous_problem_status=previous_problem_status,
        )
        if not result_payload.get("pending"):
            _audit_web_submission(
                cfg,
                event="result",
                problem_set_id=effective_psid,
                pspid=pspid,
                submission_id=sid,
                status=result_payload.get("status") or "UNKNOWN",
            )
        return jsonify(result_payload)

    # ---------- 本地答案读写 ----------
    @app.route("/api/answer")
    @api_route
    def get_answer():
        pspid = request.args.get("pspid")
        if not pspid:
            return jsonify({"error": "缺少 pspid"}), 400
        if not pspid.isascii() or not pspid.isdigit():
            return jsonify({"error": "pspid 必须是纯数字"}), 400
        cfg = _cfg()
        answers = _scan_answers_within_root(cfg.answers_dir)
        ans = answers.get(str(pspid))
        if not ans:
            return jsonify({"exists": False})
        return jsonify({"exists": True, "code": ans.read(), "ext": ans.ext})

    @app.route("/api/save-answer", methods=["POST"])
    @api_route
    def save_answer():
        data = _json_body()
        pspid = str(data.get("pspid") or "")
        code = str(data.get("code") or "")
        ext = str(data.get("ext") or ".c").strip().lower()
        if not pspid:
            return jsonify({"error": "缺少 pspid"}), 400
        if not pspid.isascii() or not pspid.isdigit():
            return jsonify({"error": "pspid 必须是纯数字"}), 400
        if not ext.startswith("."):
            ext = "." + ext
        if ext not in ALLOWED_ANSWER_EXTENSIONS:
            allowed = ", ".join(sorted(ALLOWED_ANSWER_EXTENSIONS))
            return jsonify({"error": f"不允许的答案扩展名；仅支持: {allowed}"}), 400
        cfg = _cfg()
        answers_dir = Path(cfg.answers_dir).expanduser().resolve()
        answers_dir.mkdir(parents=True, exist_ok=True)
        candidate = answers_dir / f"{pspid}{ext}"
        if candidate.is_symlink():
            return jsonify({"error": "答案文件不能是符号链接"}), 400
        path = candidate.resolve()
        try:
            path.relative_to(answers_dir)
        except ValueError:
            return jsonify({"error": "答案路径越出 answersDir，已拒绝"}), 400
        with path.open("w", encoding="utf-8") as f:
            f.write(code)
        return jsonify({"ok": True, "path": path.name})

    return app
