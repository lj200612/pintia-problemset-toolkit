"""题集发现与自动选择 problemSetId。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from .client import PTAClient
from .config import Config, update_config_file


@dataclass
class ProblemSetChoice:
    id: str
    name: str
    end_at: str = ""
    scope: str = ""


@dataclass
class ProblemSetSelection:
    choice: Optional[ProblemSetChoice]
    reason: str
    candidates: List[ProblemSetChoice]


def _choice(raw: Dict, scope: str) -> Optional[ProblemSetChoice]:
    psid = str(raw.get("id") or "").strip()
    if not psid:
        return None
    return ProblemSetChoice(
        id=psid,
        name=raw.get("name") or raw.get("title") or psid,
        end_at=raw.get("endAt") or "",
        scope=scope,
    )


def _choices(problem_sets: Sequence[Dict], scope: str) -> List[ProblemSetChoice]:
    return [c for c in (_choice(ps, scope) for ps in problem_sets) if c]


def _find(choices: Sequence[ProblemSetChoice], psid: str) -> Optional[ProblemSetChoice]:
    if not psid:
        return None
    return next((c for c in choices if c.id == psid), None)


def _fetch_my_sets(client: PTAClient, only_active: bool) -> List[ProblemSetChoice]:
    scope = "active" if only_active else "all"
    resp = client.get_my_problem_sets(only_active=only_active)
    return _choices(resp.get("problemSets") or [], scope)


def _fetch_available_sets(client: PTAClient) -> List[ProblemSetChoice]:
    resp = client.get_always_available_problem_sets()
    return _choices(resp.get("problemSets") or [], "available")


def select_problem_set(client: PTAClient, current_id: str = "") -> ProblemSetSelection:
    """选择一个可用 problemSetId。

    策略：
    1. 如果当前配置属于“进行中”或“我的题集”，尊重配置。
    2. 否则如果只有一个进行中的题集，自动选择它。
    3. 没有配置且“我的题集”只有一个时，选择它。
    4. 如果当前配置是公开题集，且没有更明确的唯一进行中题集，尊重配置。
    """
    current_id = str(current_id or "").strip()

    active = _fetch_my_sets(client, only_active=True)
    current = _find(active, current_id)
    if current:
        return ProblemSetSelection(current, "当前配置是进行中的题集", active)

    all_sets = _fetch_my_sets(client, only_active=False)
    current = _find(all_sets, current_id)
    if current:
        return ProblemSetSelection(current, "当前配置属于我的题集", all_sets)

    if len(active) == 1:
        return ProblemSetSelection(active[0], "检测到唯一进行中的题集", active)

    if not current_id and len(all_sets) == 1:
        return ProblemSetSelection(all_sets[0], "检测到唯一已加入题集", all_sets)

    available: List[ProblemSetChoice] = []
    if current_id:
        available = _fetch_available_sets(client)
        current = _find(available, current_id)
        if current:
            return ProblemSetSelection(current, "当前配置是公开题集", available)

    candidates = active or all_sets or available
    if not candidates:
        return ProblemSetSelection(None, "没有发现可自动选择的题集", [])
    return ProblemSetSelection(None, "发现多个候选题集，无法自动选择", candidates)


def format_candidates(candidates: Sequence[ProblemSetChoice], limit: int = 8) -> str:
    lines = []
    for c in list(candidates)[:limit]:
        end = f" 截止 {c.end_at}" if c.end_at else ""
        lines.append(f"  {c.id}  {c.name}{end}")
    if len(candidates) > limit:
        lines.append(f"  ... 另有 {len(candidates) - limit} 个")
    return "\n".join(lines)


def auto_configure_problem_set(
    cfg: Config, config_path: Optional[str] = None
) -> ProblemSetSelection:
    """按登录账号自动选择题集，并可写回 config.json。"""
    selection = select_problem_set(PTAClient(cfg), cfg.problem_set_id)
    if selection.choice and selection.choice.id != cfg.problem_set_id:
        cfg.problem_set_id = selection.choice.id
        if config_path:
            update_config_file(config_path, {"problemSetId": selection.choice.id})
    return selection
