#!/usr/bin/env python3
"""PTA WebUI 启动入口。

用法：
    python pta_web.py                 # 启动并自动打开浏览器
    python pta_web.py --port 8800     # 指定端口
    python pta_web.py --no-open       # 不自动打开浏览器

首次使用：在网页里用「浏览器登录」（自动填账密、你拖滑块）或「粘贴 Cookie」登录，
然后选择题集即可浏览题目、编辑代码、提交并查看判题结果。
    浏览器登录需要 Playwright：
    python -m pip install -r requirements-browser.txt
    python -m playwright install chromium
"""

from __future__ import annotations

import argparse
import importlib.util
import ipaddress
import os
import sys
import threading
import webbrowser
from typing import Any, List, cast

try:
    cast(Any, sys.stdout).reconfigure(encoding="utf-8")
    cast(Any, sys.stderr).reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
REQUIREMENTS_PATH = os.path.join(ROOT_DIR, "requirements.txt")
REQUIRED_MODULES = {
    "requests": "requests",
    "flask": "flask",
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="PTA WebUI")
    p.add_argument("--config", default="config.json", help="配置文件路径（默认 config.json）")
    p.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    p.add_argument("--port", type=int, default=8765, help="端口（默认 8765）")
    p.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    return p.parse_args()


def missing_dependencies() -> List[str]:
    missing = []
    for module_name, package_name in REQUIRED_MODULES.items():
        if importlib.util.find_spec(module_name) is None:
            missing.append(package_name)
    return missing


def ensure_dependencies() -> None:
    missing = missing_dependencies()
    if not missing:
        return

    missing_text = ", ".join(missing)
    raise RuntimeError(
        f"缺少 WebUI 必需的 Python 依赖：{missing_text}\n"
        f"请先执行：{sys.executable} -m pip install -r {REQUIREMENTS_PATH}"
    )


def is_loopback_host(host: str) -> bool:
    value = (host or "").strip().lower()
    if value == "localhost":
        return True
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def host_for_url(host: str) -> str:
    value = host.strip()
    return f"[{value}]" if ":" in value and not value.startswith("[") else value


def host_for_bind(host: str) -> str:
    value = host.strip()
    return value[1:-1] if value.startswith("[") and value.endswith("]") else value


def main() -> int:
    args = parse_args()
    if not is_loopback_host(args.host):
        print(
            f"[错误] --host {args.host!r} 不是本机回环地址；WebUI 仅允许监听 localhost/127.0.0.0/8/::1。",
            file=sys.stderr,
        )
        return 2
    try:
        ensure_dependencies()
    except RuntimeError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1

    from pta.webapp import create_app

    app = create_app(args.config)
    bind_host = host_for_bind(args.host)
    url = f"http://{host_for_url(bind_host)}:{args.port}/"
    print(f"PTA WebUI 已启动: {url}")
    print("按 Ctrl+C 退出。")
    if not args.no_open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    # threaded=True：浏览器登录是长阻塞操作时，其它请求仍可响应
    app.run(host=bind_host, port=args.port, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
