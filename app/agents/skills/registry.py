"""Skill 注册表：扫描技能仓库（skills/），解析 SKILL.md frontmatter，校验工具依赖。

- 供规划节点注入 <SkillsIndex>（name + when_to_use），让 planner 知道有哪些标准做法；
- 供 skill_probe 任务判定可用性（requires_tools ⊆ 已注册工具）。
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

import yaml

from app.agents.skills.models import SkillMeta

logger = logging.getLogger(__name__)

__all__ = ["scan_skills", "find_skill", "skill_index_text", "validate_skill"]


def skills_root() -> Path:
    """技能仓库根目录（config.yaml skills.dir，相对项目根）。"""
    try:
        from app.config import get_app_config

        cfg = get_app_config().skills
        p = Path(cfg.dir)
        if p.is_absolute():
            return p
        return Path(__file__).resolve().parent.parent.parent.parent / p
    except Exception:
        return Path(__file__).resolve().parent.parent.parent.parent / "skills"


def _parse_frontmatter(text: str) -> dict | None:
    """解析 SKILL.md 开头的 YAML frontmatter（--- 包裹），失败返回 None。"""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    try:
        data = yaml.safe_load(text[3:end])
        return data if isinstance(data, dict) else None
    except yaml.YAMLError:
        return None


def _load_meta(skill_dir: Path) -> SkillMeta | None:
    """读取单个技能目录的 SKILL.md，返回 SkillMeta。"""
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.exists():
        return None
    try:
        content = skill_file.read_text(encoding="utf-8")
    except OSError:
        return None
    fm = _parse_frontmatter(content)
    if not fm:
        return None
    sid = str(fm.get("name") or skill_dir.name)
    return SkillMeta(
        id=sid,
        dir=skill_dir,
        name=str(fm.get("name") or sid),
        description=str(fm.get("description", "") or ""),
        when_to_use=str(fm.get("when_to_use", "") or ""),
        requires_tools=[str(t) for t in (fm.get("requires_tools") or [])],
        errors_file=str(fm.get("errors", "errors.yaml") or "errors.yaml"),
        cleanup_file=str(fm.get("cleanup", "cleanup.yaml") or "cleanup.yaml"),
        has_scripts=(skill_dir / "scripts").is_dir(),
    )


def scan_skills(*, refresh: bool = False) -> list[SkillMeta]:
    """扫描技能仓库，返回全部 SkillMeta（按目录名排序，开发期可 refresh 热更）。"""
    if refresh:
        _scan_skills.cache_clear()
    return _scan_skills()


@lru_cache(maxsize=1)
def _scan_skills() -> list[SkillMeta]:
    metas: list[SkillMeta] = []
    root = skills_root()
    if not root.exists():
        logger.warning("技能仓库目录不存在: %s", root)
        return metas
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        meta = _load_meta(child)
        if meta is not None:
            metas.append(meta)
    return metas


def find_skill(skill_id: str) -> SkillMeta | None:
    """按 skill_id 精确查找。"""
    for meta in scan_skills():
        if meta.id == skill_id:
            return meta
    return None


def validate_skill(meta: SkillMeta, available_tools: set[str]) -> tuple[bool, list[str]]:
    """校验 skill 是否可用：依赖工具 ⊆ 已注册执行工具。返回 (可用, 缺失工具列表)。"""
    missing = [t for t in meta.requires_tools if t not in available_tools]
    return not missing, missing


def is_skills_enabled() -> bool:
    """SKILL 总开关（config.yaml skills.enabled）：关闭后各节点不再走技能链路。"""
    try:
        from app.config import get_app_config

        return bool(get_app_config().skills.enabled)
    except Exception:
        return False


def skill_index_text(max_candidates: int = 3, available_tools: set[str] | None = None) -> str:
    """导出 <SkillsIndex> 文本（注入规划节点提示词，每 skill ≤2 行）。"""
    metas = scan_skills()
    if not metas:
        return ""
    lines = ["## 可用技能（SkillsIndex）：需求命中时选用标准技能；**一个技能 = 一个执行任务**（不要按步骤拆分，执行 agent 会读完整份 SKILL 自行完成）", ""]
    for meta in metas:
        ok, missing = validate_skill(meta, available_tools or set())
        status = "" if ok else f"（缺工具: {','.join(missing)}，不可用）"
        lines.append(f"- **{meta.id}**: {meta.when_to_use or meta.description}{status}")
    return "\n".join(lines)
