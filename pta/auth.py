"""Playwright 浏览器辅助登录：自动填账号密码，用户拖滑块完成登录后抓取 cookie。

由于 PTA 账密登录页带滑块验证码，无法用纯 HTTP 自动登录；这里打开一个真实
浏览器窗口，自动填好账号密码，剩下的滑块/点击由用户完成，登录成功后程序自动
读取 PTASession 等 cookie 并返回完整 cookie 字符串。
"""

from __future__ import annotations

import time
from typing import Callable, List, Optional

from .client import extract_current_user
from .network import chromium_direct_args, direct_session

LOGIN_URL = "https://pintia.cn/auth/login"
PASSPORT_CURRENT_URL = "https://passport.pintia.cn/api/u/current"
SUCCESS_URL_HINT = "pintia.cn"
COOKIE_KEY = "PTASession"
STANDARD_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.7827.115 Safari/537.36"
)


class PlaywrightNotInstalled(RuntimeError):
    pass


def _ensure_playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError as exc:
        raise PlaywrightNotInstalled(
            "未安装 playwright。请先执行：\n"
            "  pip install playwright\n"
            "  python -m playwright install chromium"
        ) from exc
    from playwright.sync_api import sync_playwright

    return sync_playwright


def _is_pintia_cookie_domain(value: object) -> bool:
    domain = str(value or "").strip().lower()
    if domain.startswith("."):
        domain = domain[1:]
    domain = domain.rstrip(".")
    if not domain or len(domain) > 253:
        return False
    for label in domain.split("."):
        if (
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or any(not char.isascii() or not (char.isalnum() or char == "-") for char in label)
        ):
            return False
    return domain == "pintia.cn" or domain.endswith(".pintia.cn")


def _build_cookie_string(cookies: List[dict]) -> str:
    parts = []
    seen = set()
    for c in cookies:
        if not _is_pintia_cookie_domain(c.get("domain")):
            continue
        name = c.get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        parts.append(f"{name}={c.get('value', '')}")
    return "; ".join(parts)


def _cookie_has_logged_in_user(cookie_str: str) -> bool:
    if not cookie_str:
        return False
    try:
        session = direct_session()
        resp = session.get(
            PASSPORT_CURRENT_URL,
            headers={
                "Accept": "application/json;charset=UTF-8",
                "Accept-Language": "zh-CN",
                "User-Agent": STANDARD_CHROME_UA,
                "Cookie": cookie_str,
            },
            timeout=10,
        )
        if not resp.ok or not resp.text:
            return False
        return bool(extract_current_user(resp.json()))
    except Exception:
        return False


def _try_fill(page, selectors: List[str], value: str) -> bool:
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el:
                el.fill(value)
                return True
        except Exception:
            continue
    return False


def browser_login(
    email: str = "",
    password: str = "",
    timeout: float = 240.0,
    on_status: Optional[Callable[[str], None]] = None,
) -> str:
    """打开浏览器辅助登录，返回完整 cookie 字符串（含 PTASession）。

    email/password 可留空（则完全手动登录）。timeout 为等待用户完成登录的秒数。
    """

    def notify(msg: str) -> None:
        if on_status:
            try:
                on_status(msg)
            except Exception:
                pass

    sync_playwright = _ensure_playwright()

    with sync_playwright() as p:
        notify("正在启动浏览器...")
        browser = p.chromium.launch(headless=False, args=list(chromium_direct_args()))
        context = browser.new_context(user_agent=STANDARD_CHROME_UA)
        page = context.new_page()
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass

        page.wait_for_timeout(1500)
        if email:
            filled = _try_fill(
                page,
                [
                    'input[type="text"]',
                    'input[type="email"]',
                    'input[placeholder*="邮箱"]',
                    'input[placeholder*="手机"]',
                    'input[name="username"]',
                ],
                email,
            )
            notify("已尝试填入账号" if filled else "未找到账号输入框，请手动输入")
        if password:
            _try_fill(
                page,
                [
                    'input[type="password"]',
                    'input[placeholder*="密码"]',
                    'input[name="password"]',
                ],
                password,
            )

        notify("请在浏览器窗口完成登录（拖动滑块并点击登录）...")

        cookie_str = ""
        last_notice = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                cookies = context.cookies()
            except Exception:
                cookies = []
            names = {c.get("name") for c in cookies}
            if COOKIE_KEY in names:
                candidate = _build_cookie_string(cookies)
                if candidate and _cookie_has_logged_in_user(candidate):
                    cookie_str = candidate
                    notify("检测到已登录用户，正在保存 cookie...")
                    break
                if last_notice != "waiting_user":
                    notify("检测到会话 cookie，继续等待登录完成...")
                    last_notice = "waiting_user"
            try:
                page.wait_for_timeout(1000)
            except Exception:
                time.sleep(1)

        try:
            browser.close()
        except Exception:
            pass

    if not cookie_str:
        raise TimeoutError("超时未检测到已登录用户。请确认滑块验证和登录按钮都已完成。")
    return cookie_str
