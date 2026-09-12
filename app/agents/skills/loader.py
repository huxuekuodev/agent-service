"""Skill 上下文加载：整份 SKILL.md + 文件清单 + 规则（errors/cleanup）。

结构化数据一律用 **YAML**（比 markdown 表格语义更完整、解析更稳）：
``errors.yaml`` / ``cleanup.yaml`` 为纯 YAML 列表，或 ``{errors: [...]}`` / ``{cleanups: [...]}`` 包裹。

**不解析步骤**：技能的执行流程写在 ``SKILL.md`` 正文里（人机共读），框架原样交给执行 LLM；
小体积文本文件（md/yaml/txt/json/csv，如 ``reference/*.md`` 参数说明）一并内联，
其余文件（脚本、大数据文件）只列清单——它们在沙箱里可用 ``cat`` / 直接执行。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

from app.agents.skills.models import CLEANUP_AUTO, CleanupRule, ErrorRule, SkillContext, SkillFile, is_text_path
from app.agents.skills.registry import find_skill

__all__ = ["load_skill_context", "load_skill_context_by_id", "read_skill_file"]

#: 单个文本文件内联上限（超过只列清单，避免把大文件灌进上下文）
_MAX_INLINE_FILE_BYTES = 30_000
#: 内联文本文件数量上限
_MAX_INLINE_FILES = 30


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """拆分 SKILL.md 的 frontmatter 与正文；无 frontmatter 时返回空字典 + 全文。"""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    try:
        data = yaml.safe_load(text[3:end])
    except yaml.YAMLError:
        return {}, text
    body = text[end + 4 :]
    return (data if isinstance(data, dict) else {}), body.lstrip("\n")


def _collect_files(skill_dir: Path) -> list[SkillFile]:
    """扫描技能目录：文本文件内联内容，其余只记大小。"""
    files: list[SkillFile] = []
    inlined = 0
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(skill_dir).as_posix()
        if "__pycache__" in rel or rel.endswith(".pyc"):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        text: str | None = None
        if is_text_path(rel) and size <= _MAX_INLINE_FILE_BYTES and inlined < _MAX_INLINE_FILES:
            try:
                text = path.read_text(encoding="utf-8")
                inlined += 1
            except (OSError, UnicodeDecodeError):
                text = None
        files.append(SkillFile(path=rel, size=size, text=text))
    return files


def _parse_error_rules(text: str) -> list[ErrorRule]:
    """解析 errors.yaml（支持裸列表或 {errors: [...]}）。"""
    try:
        data = yaml.safe_load(text) if text.strip() else None
    except yaml.YAMLError:
        return []
    items = data.get("errors") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    rules: list[ErrorRule] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("code"):
            continue
        rules.append(
            ErrorRule(
                code=str(item["code"]),
                condition=str(item.get("condition", "") or ""),
                action=str(item.get("action", "retry") or "retry"),
                recovery=str(item.get("recovery", "") or ""),
                note=str(item.get("note", "") or ""),
            )
        )
    return rules


def _parse_cleanup_rules(text: str) -> list[CleanupRule]:
    """解析 cleanup.yaml（支持裸列表或 {cleanups: [...]}）。"""
    try:
        data = yaml.safe_load(text) if text.strip() else None
    except yaml.YAMLError:
        return []
    items = data.get("cleanups") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    rules: list[CleanupRule] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("path"):
            continue
        rules.append(
            CleanupRule(
                path=str(item["path"]),
                kind=str(item.get("kind", "file") or "file"),
                level=str(item.get("level", CLEANUP_AUTO) or CLEANUP_AUTO),
                note=str(item.get("note", "") or ""),
            )
        )
    return rules


def _read_text(path: Path) -> str:
    """同步读文本（放线程池调用，避免阻塞事件循环）。"""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


async def load_skill_context(skill_dir: str | Path) -> SkillContext | None:
    """加载一个技能目录的完整上下文；无 SKILL.md 时返回 None。"""
    directory = Path(skill_dir)
    skill_md_path = directory / "SKILL.md"
    if not skill_md_path.exists():
        return None

    meta = await asyncio.to_thread(find_skill, directory.name)
    if meta is None:
        return None

    raw = await asyncio.to_thread(_read_text, skill_md_path)
    _, body = _parse_frontmatter(raw)
    files = await asyncio.to_thread(_collect_files, directory)
    errors_text = await asyncio.to_thread(_read_text, directory / meta.errors_file)
    cleanup_text = await asyncio.to_thread(_read_text, directory / meta.cleanup_file)

    return SkillContext(
        meta=meta,
        skill_md=body or raw,
        files=files,
        error_rules=_parse_error_rules(errors_text),
        cleanup_rules=_parse_cleanup_rules(cleanup_text),
    )


async def load_skill_context_by_id(skill_id: str) -> SkillContext | None:
    """按技能 id 加载完整上下文（不存在返回 None）。"""
    meta = await asyncio.to_thread(find_skill, skill_id)
    if meta is None:
        return None
    return await load_skill_context(meta.dir)


async def read_skill_file(skill_id: str, rel_path: str) -> str | None:
    """读取技能目录内的单个文件（相对路径，禁止越界）；不存在/越界返回 None。"""
    meta = await asyncio.to_thread(find_skill, skill_id)
    if meta is None:
        return None
    root = meta.dir.resolve()
    target = (root / rel_path).resolve()
    if root not in target.parents and target != root:
        return None
    if not target.is_file():
        return None
    return await asyncio.to_thread(_read_text, target)
