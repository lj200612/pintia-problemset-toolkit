#!/usr/bin/env python3
"""把 PTA 题集的题目抓取并保存为本地 Markdown（命令行入口）。

用法示例：
    python pta_fetch.py --list                 # 只列出题目目录，不抓取
    python pta_fetch.py                          # 抓取全部题目到 problems/（已存在则跳过）
    python pta_fetch.py --overwrite             # 覆盖已存在的 md
    python pta_fetch.py --type CODE_COMPLETION  # 只抓函数题
    python pta_fetch.py --only 2042...,2043...  # 只抓指定题目
    python pta_fetch.py --combined              # 额外生成合并文件 _all.md
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Union, cast

try:
    cast(Any, sys.stdout).reconfigure(encoding="utf-8")
    cast(Any, sys.stderr).reconfigure(encoding="utf-8")
except Exception:
    pass

from pta.client import PTAAuthError, PTAClient, PTAError
from pta.config import load_config
from pta.discovery import auto_configure_problem_set, format_candidates
from pta.fetcher import format_problem, safe_filename
from pta.submitter import build_catalog


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="PTA 题目抓取工具（保存为本地 Markdown）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--config", default="config.json", help="配置文件路径（默认 config.json）")
    p.add_argument("--out", default=None, help="输出目录（默认取配置 problemsDir）")
    p.add_argument("--only", default="", help="逗号分隔题目 id，仅抓取这些题")
    p.add_argument("--type", default="", help="仅抓取指定题型 CODE_COMPLETION / PROGRAMMING")
    p.add_argument("--overwrite", action="store_true", help="覆盖已存在的 md（默认跳过）")
    p.add_argument("--combined", action="store_true", help="额外生成合并文件 _all.md")
    p.add_argument("--list", action="store_true", help="只列出题目目录，不抓取")
    return p.parse_args()


def read_text(path: Union[str, Path]) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def is_ascii_problem_id(value: object) -> bool:
    """PTA problem IDs used in filenames must contain ASCII digits only."""

    problem_id = str(value)
    return bool(problem_id) and all("0" <= char <= "9" for char in problem_id)


def resolve_output_path(out_dir: Path, filename: str) -> Path:
    """Resolve an output filename and reject paths (including symlinks) outside root."""

    root = out_dir.resolve()
    candidate = (root / filename).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("输出路径超出目标目录") from exc
    if candidate == root:
        raise ValueError("输出路径不能是目标目录本身")
    return candidate


def _display_untrusted_id(value: object) -> str:
    text = str(value)
    if len(text) > 80:
        text = text[:80] + "..."
    return repr(text)


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

    before_psid = cfg.problem_set_id
    try:
        # --list 是只读操作，不应因为自动选择题集而改写 config.json。
        selection = auto_configure_problem_set(
            cfg,
            None if args.list else args.config,
        )
        if selection.choice and cfg.problem_set_id != before_psid:
            print(
                f"[自动选择题集] {cfg.problem_set_id}  {selection.choice.name}"
                f"（{selection.reason}）"
            )
    except PTAError as exc:
        selection = None
        if not cfg.problem_set_id:
            print(f"[错误] 自动获取 problemSetId 失败：{exc}")
            return 2

    if not cfg.problem_set_id:
        print("[错误] 没有可自动选择的 problemSetId。")
        if selection and selection.candidates:
            print("候选题集：")
            print(format_candidates(selection.candidates))
        return 2

    out_dir = args.out or cfg.problems_dir
    client = PTAClient(cfg)

    try:
        catalog = build_catalog(client.get_problem_types())
    except PTAAuthError as exc:
        print(f"[鉴权失败] {exc}")
        return 2
    except PTAError as exc:
        print(f"[错误] 获取题目目录失败：{exc}")
        return 2

    metas = sorted(catalog.values(), key=lambda m: m.pspid)

    only_ids = {x.strip() for x in args.only.split(",") if x.strip()}
    if only_ids:
        metas = [m for m in metas if m.pspid in only_ids]
    if args.type.strip():
        t = args.type.strip().upper()
        metas = [m for m in metas if (m.ptype or "").upper() == t]

    selected_count = len(metas)
    valid_metas = []
    invalid_count = 0
    for meta in metas:
        if is_ascii_problem_id(meta.pspid):
            valid_metas.append(meta)
            continue
        invalid_count += 1
        print(f"[错误] 平台返回无效题号 {_display_untrusted_id(meta.pspid)}，已跳过。")
    metas = valid_metas

    if args.list:
        for m in metas:
            print(f"  {m.pspid}  [{m.ptype}]  {m.title}")
        print(f"\n共 {len(metas)} 题")
        if invalid_count:
            print(f"另跳过无效题号 {invalid_count} 项")
        return 0

    if not metas:
        if invalid_count:
            print("没有可抓取的有效题目。")
        else:
            print("没有匹配的题目。")
        return 0

    out_root = Path(out_dir).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    combined_parts = []
    ok = skip = 0
    fail = invalid_count

    for idx, m in enumerate(metas, 1):
        fname = f"{m.pspid}-{safe_filename(m.title)}.md"
        prefix = f"[{idx}/{selected_count}] {m.pspid}"
        try:
            fpath = resolve_output_path(out_root, fname)
        except ValueError as exc:
            fail += 1
            print(f"{prefix} 失败: {exc}")
            continue
        exists = fpath.exists()

        if exists and not args.overwrite:
            skip += 1
            print(f"{prefix} 跳过(已存在) {fname}")
            if args.combined:
                combined_parts.append(read_text(fpath))
            continue

        try:
            detail = client.get_problem_detail(m.pspid)
            text = format_problem(detail)
        except PTAError as exc:
            fail += 1
            print(f"{prefix} 失败: {exc}")
            continue

        with fpath.open("w", encoding="utf-8") as f:
            f.write(text)
        ok += 1
        print(f"{prefix} 已保存 {fname}")
        if args.combined:
            combined_parts.append(text)

        if cfg.fetch_delay_seconds > 0 and idx < len(metas):
            time.sleep(cfg.fetch_delay_seconds)

    if args.combined and combined_parts:
        try:
            combined_path = resolve_output_path(out_root, "_all.md")
        except ValueError as exc:
            fail += 1
            print(f"\n[错误] 合并文件未写入: {exc}")
        else:
            with combined_path.open("w", encoding="utf-8") as f:
                f.write("\n\n".join(combined_parts))
            print(f"\n合并文件: {combined_path}")

    print("\n" + "=" * 50)
    print(f"完成：保存 {ok}，跳过 {skip}，失败 {fail}，共 {selected_count} 题。")
    print(f"输出目录: {out_root}")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
