#!/usr/bin/env python3
"""PTA 题集批量提交工具（命令行入口）。

用法示例：
    python pta_submit.py --list                # 只看目录 / 答案覆盖情况，不提交
    python pta_submit.py --dry-run             # 预演：显示将提交哪些题，不真正提交
    python pta_submit.py                        # 真正提交（提交前需二次确认）
    python pta_submit.py --yes                  # 跳过确认直接提交
    python pta_submit.py --only 2042...,2043... # 只提交指定题目 id
    python pta_submit.py --include-accepted     # 连已通过的题也重新提交

安全说明见 README.md。本工具默认只跳过已 ACCEPTED 的题，
不会触碰交卷、删除、重判等高风险接口。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Optional, Set, cast

# Windows 控制台中文输出兜底
try:
    cast(Any, sys.stdout).reconfigure(encoding="utf-8")
    cast(Any, sys.stderr).reconfigure(encoding="utf-8")
except Exception:
    pass

from pta.client import PTAAuthError, PTAClient, PTAError
from pta.config import load_config
from pta.discovery import auto_configure_problem_set, format_candidates
from pta.submitter import (
    ACCEPTED_PROBLEM_STATUS,
    SubmitResult,
    apply_status,
    available_compilers,
    build_catalog,
    build_payload,
    extract_submission_id,
    poll_result,
    scan_answers,
    select_targets,
    status_text,
    validate_exam,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="PTA 题集批量提交工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--config", default="config.json", help="配置文件路径（默认 config.json）")
    p.add_argument("--list", action="store_true", help="只列出题目与答案覆盖情况，不提交")
    p.add_argument("--dry-run", action="store_true", help="预演，显示将提交的题但不真正提交")
    p.add_argument("-y", "--yes", action="store_true", help="跳过二次确认直接提交")
    p.add_argument("--force", action="store_true", help="即使考试非 PROCESSING/已结束也尝试提交")
    p.add_argument("--no-poll", action="store_true", help="提交后不轮询判题结果")
    p.add_argument("--include-accepted", action="store_true", help="连已通过的题也重新提交")
    p.add_argument("--only", default="", help="逗号分隔的题目 id，仅提交这些题")
    return p.parse_args()


def print_listing(catalog, answers) -> None:
    print("\n题目目录与答案覆盖情况：")
    print("-" * 78)
    by_type: dict[str, list] = {}
    for meta in catalog.values():
        by_type.setdefault(meta.ptype or "UNKNOWN", []).append(meta)
    for ptype in sorted(by_type):
        metas = sorted(by_type[ptype], key=lambda m: m.pspid)
        have = sum(1 for m in metas if m.pspid in answers)
        acc = sum(1 for m in metas if m.status == ACCEPTED_PROBLEM_STATUS)
        print(f"\n[{ptype}] 共 {len(metas)} 题，已通过 {acc}，本地有答案 {have}")
        for m in metas:
            mark_ans = "有答案" if m.pspid in answers else "  -  "
            mark_acc = "已通过" if m.status == ACCEPTED_PROBLEM_STATUS else (m.status or "未作答")
            print(f"  {m.pspid}  [{mark_ans}] [{mark_acc:<16}] {m.title}")
    print("-" * 78)


def confirm(prompt: str) -> bool:
    try:
        ans = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("y", "yes")


def open_log(log_dir: str):
    os.makedirs(log_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(log_dir, f"submit-{stamp}.jsonl")
    return open(path, "a", encoding="utf-8"), path


def log_line(fp, payload: dict) -> None:
    fp.write(json.dumps(payload, ensure_ascii=False) + "\n")
    fp.flush()


def main() -> int:
    args = parse_args()

    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[错误] {exc}")
        return 2

    only_ids: Optional[Set[str]] = None
    if args.only.strip():
        only_ids = {x.strip() for x in args.only.split(",") if x.strip()}

    if not cfg.cookie and not args.list:
        print("[错误] 未配置 cookie。请在 config.json 填写 cookie，或设置环境变量 PTA_COOKIE。")
        return 2

    selection = None
    if cfg.cookie:
        before_psid = cfg.problem_set_id
        try:
            # --list / --dry-run 承诺不修改本地状态，因此只在真实提交流程中
            # 将自动选择结果写回配置文件。
            selection = auto_configure_problem_set(
                cfg,
                None if (args.list or args.dry_run) else args.config,
            )
            if selection.choice and cfg.problem_set_id != before_psid:
                print(
                    f"[自动选择题集] {cfg.problem_set_id}  {selection.choice.name}"
                    f"（{selection.reason}）"
                )
        except PTAError as exc:
            if not cfg.problem_set_id:
                print(f"[错误] 自动获取 problemSetId 失败：{exc}")
                return 2

    if not cfg.problem_set_id:
        print("[错误] 没有可自动选择的 problemSetId。")
        if selection and selection.candidates:
            print("候选题集：")
            print(format_candidates(selection.candidates))
        return 2

    client = PTAClient(cfg)

    # 1. 考试元信息
    try:
        exam_resp = client.get_exam()
        exam_id, exam_status, exam_ended = validate_exam(exam_resp)
    except PTAAuthError as exc:
        print(f"[鉴权失败] {exc}")
        return 2
    except PTAError as exc:
        print(f"[错误] 获取考试信息失败：{exc}")
        return 2

    compilers = available_compilers(exam_resp)
    print(f"题集: {cfg.problem_set_id}")
    print(f"exam.id: {exam_id}  status: {exam_status}  ended: {exam_ended}")
    if compilers:
        print(f"可用编译器: {', '.join(compilers)}")

    # 2. 目录 + 状态
    try:
        catalog = build_catalog(client.get_problem_types())
        apply_status(catalog, client.get_problem_status(exam_id))
    except PTAError as exc:
        print(f"[错误] 获取题目目录/状态失败：{exc}")
        return 2

    # 3. 答案扫描
    answers = scan_answers(cfg.answers_dir)

    if args.list:
        print_listing(catalog, answers)
        return 0

    if not answers:
        print(f"\n[提示] 在 {cfg.answers_dir}/ 没有发现以题目 id 命名的答案文件。")
        print("       请把答案放进该目录，文件名形如 <problem-id>.c")
        return 0

    targets, unknown, skipped = select_targets(catalog, answers, args.include_accepted, only_ids)

    if unknown:
        print(f"\n[警告] 以下答案文件 id 不在题集目录中，已忽略：{', '.join(unknown)}")
    if skipped:
        print(f"[跳过] {len(skipped)} 题已通过（如需重交加 --include-accepted）。")

    if not targets:
        print("\n没有需要提交的题目。")
        return 0

    # 编译器校验（仅 PROGRAMMING 题需要）
    print(f"\n将提交 {len(targets)} 题：")
    bad_compiler = []
    for meta, ans in targets:
        if meta.ptype == "PROGRAMMING":
            comp = cfg.compiler_for_extension(ans.ext)
            tag = comp
            if compilers and comp not in compilers:
                tag = f"{comp}(不在可用列表!)"
                bad_compiler.append((meta, ans, comp))
            print(f"  {meta.pspid}  [{meta.ptype}]  编译器={tag}  {meta.title}")
        else:
            print(f"  {meta.pspid}  [{meta.ptype}]  {meta.title}")

    if bad_compiler and not args.dry_run:
        names = ", ".join(compilers) if compilers else "（接口未返回）"
        print(
            "\n[错误] 上述题目解析出的编译器不在可用列表中。"
            f"\n可用编译器: {names}"
            "\n请在 config.json 的 compilerByExtension/defaultCompiler 中改正后重试。"
        )
        return 3

    # 考试状态检查
    if (exam_status != "PROCESSING" or exam_ended) and not args.dry_run and not args.force:
        print(
            f"\n[拒绝] 考试状态为 {exam_status}、ended={exam_ended}，默认不提交。"
            "\n确需提交请加 --force。"
        )
        return 3

    if args.dry_run:
        print("\n[dry-run] 预演结束，未提交任何内容。")
        return 0

    # 二次确认
    if not args.yes:
        if not confirm(f"\n确认向题集 {cfg.problem_set_id} 提交以上 {len(targets)} 题? [y/N] "):
            print("已取消。")
            return 0

    # 提交主循环
    log_fp, log_path = open_log(cfg.log_dir)
    print(f"\n操作日志: {log_path}")
    results = []
    try:
        for idx, (meta, ans) in enumerate(targets, 1):
            code = ans.read()
            error_fields = None
            compiler = cfg.compiler_for_extension(ans.ext) if meta.ptype == "PROGRAMMING" else None
            prefix = f"[{idx}/{len(targets)}] {meta.pspid} {meta.title}"
            try:
                payload = build_payload(meta, code, compiler)
                resp = client.submit(exam_id, payload)
                sid = extract_submission_id(resp)
                if not sid:
                    res = SubmitResult(
                        meta.pspid,
                        meta.title,
                        meta.ptype,
                        False,
                        "NO_SUBMISSION_ID",
                        compiler=compiler,
                        message="提交返回中没有 submissionId",
                    )
                elif args.no_poll:
                    res = SubmitResult(
                        meta.pspid,
                        meta.title,
                        meta.ptype,
                        True,
                        "SUBMITTED",
                        submission_id=sid,
                        compiler=compiler,
                    )
                else:
                    print(f"{prefix} -> 已提交 (sid={sid})，轮询判题中...")
                    status, score, _raw = poll_result(
                        client,
                        exam_id,
                        sid,
                        cfg,
                        problem_set_problem_id=meta.pspid,
                        previous_problem_status=meta.status,
                    )
                    res = SubmitResult(
                        meta.pspid,
                        meta.title,
                        meta.ptype,
                        status.upper() == "ACCEPTED",
                        status,
                        score=score,
                        submission_id=sid,
                        compiler=compiler,
                    )
            except PTAError as exc:
                error_fields = exc.safe_log_fields()
                res = SubmitResult(
                    meta.pspid,
                    meta.title,
                    meta.ptype,
                    False,
                    "ERROR",
                    compiler=compiler,
                    message=str(exc),
                )

            results.append(res)
            tag = "OK " if res.ok else "XX "
            extra = f" 得分={res.score}" if res.score is not None else ""
            extra += f" {res.message}" if res.message else ""
            print(f"{prefix} -> [{tag}] {status_text(res.status)}{extra}")

            log_record = {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "problemSetProblemId": res.pspid,
                "title": res.title,
                "problemType": res.ptype,
                "compiler": res.compiler,
                "submissionId": res.submission_id,
                "status": res.status,
                "score": res.score,
                "ok": res.ok,
            }
            if error_fields is not None:
                # Exception messages can contain response bodies that echo source
                # code or credentials.  Persist only the exception's safe schema.
                log_record["error"] = error_fields
            elif res.message:
                log_record["message"] = res.message
            log_line(log_fp, log_record)

            if idx < len(targets) and cfg.submit_delay_seconds > 0:
                time.sleep(cfg.submit_delay_seconds)
    finally:
        log_fp.close()

    # 汇总
    ok = sum(1 for r in results if r.ok)
    fail = len(results) - ok
    print("\n" + "=" * 60)
    print(f"完成：成功 {ok} 题，未通过/失败 {fail} 题，共 {len(results)} 题。")
    if fail:
        print("未通过/失败明细：")
        for r in results:
            if not r.ok:
                msg = f" {r.message}" if r.message else ""
                print(f"  {r.pspid}  {status_text(r.status)}{msg}  {r.title}")
    print(f"详细日志: {log_path}")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
