#!/usr/bin/env python3
"""列出 PTA 题集及其 problemSetId（命令行入口）。

problemSetId 就是题集 URL 里的那串数字：
    https://pintia.cn/problem-sets/{problemSetId}/...

用法示例：
    python pta_list.py              # 我加入的、进行中的题集
    python pta_list.py --all        # 我加入的全部题集（含已结束）
    python pta_list.py --available  # 额外列出长期开放/公开题集
    python pta_list.py --me         # 顺带显示当前登录用户
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, cast

try:
    cast(Any, sys.stdout).reconfigure(encoding="utf-8")
    cast(Any, sys.stderr).reconfigure(encoding="utf-8")
except Exception:
    pass

from pta.client import PTAAuthError, PTAClient, PTAError, extract_current_user
from pta.config import load_config


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="列出 PTA 题集及其 problemSetId",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--config", default="config.json", help="配置文件路径（默认 config.json）")
    p.add_argument("--all", action="store_true", help="我加入的全部题集（含已结束）")
    p.add_argument("--available", action="store_true", help="额外列出长期开放/公开题集")
    p.add_argument("--me", action="store_true", help="显示当前登录用户")
    return p.parse_args()


def print_sets(title: str, problem_sets: list) -> None:
    print(f"\n{title}（共 {len(problem_sets)} 个）")
    print("-" * 72)
    if not problem_sets:
        print("  （无）")
        return
    for ps in problem_sets:
        psid = ps.get("id", "")
        name = ps.get("name", "") or ps.get("title", "")
        extra = ""
        if ps.get("endAt"):
            extra = f"  截止 {ps.get('endAt')}"
        print(f"  {psid}  {name}{extra}")


def main() -> int:
    args = parse_args()

    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[错误] {exc}")
        return 2

    if not cfg.cookie:
        print("[错误] 未配置 cookie。请在 config.json 填写 cookie，或设置环境变量 PTA_COOKIE。")
        return 2

    client = PTAClient(cfg)

    if args.me:
        try:
            user = client.get_current_user()
            u = extract_current_user(user)
            if u:
                nick = u.get("nickname") or u.get("name") or u.get("email") or "PTA 用户"
                print(f"当前用户: {nick}  id={u.get('id', '')}")
            else:
                print("[警告] 当前 cookie 未通过登录校验。")
        except PTAError as exc:
            print(f"[警告] 获取当前用户失败：{exc}")

    try:
        resp = client.get_my_problem_sets(only_active=not args.all)
        my_sets = resp.get("problemSets") or []
    except PTAAuthError as exc:
        print(f"[鉴权失败] {exc}")
        return 2
    except PTAError as exc:
        print(f"[错误] 获取我的题集失败：{exc}")
        return 2

    title = "我加入的题集（全部）" if args.all else "我加入的题集（进行中）"
    print_sets(title, my_sets)

    if args.available:
        try:
            resp2 = client.get_always_available_problem_sets()
            avail = resp2.get("problemSets") or []
            print_sets("长期开放 / 公开题集", avail)
        except PTAError as exc:
            print(f"[警告] 获取长期开放题集失败：{exc}")

    print(
        "\n提示：把上面某个 id 填进 config.json 的 problemSetId，即可用 pta_fetch.py / pta_submit.py。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
