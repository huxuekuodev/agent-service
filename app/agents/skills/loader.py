"""Skill 上下文加载：解析 SOP / errors / cleanup 为数据模型。

结构化数据一律用 **YAML**（比 markdown 表格语义更完整、解析更稳），约定：

  - ``errors.yaml`` / ``cleanup.yaml``：纯 YAML 列表（或 ``{errors: [...]}`` / ``{cleanups: [...]}`` 包裹）。
  - ``SOP.md``：人读的步骤说明；每步 ``## step-xx 标题`` 之后紧跟一个 ```yaml 代码块，
    声明机器字段（script / tool），块外正文为该步详细说明。

示例（errors.yaml）::

    errors:
      - code: YQ_NO_VERSION
        condition: 目标日期之前无已发布版本
        action: retry
        note: 放宽目标日期重试，或直接回答"无历史版本"

示例（SOP.md）::

    ## step-01 生成数据画像

    ```yaml
    script: scripts/profile.py
    ```

    在沙箱内运行 profile 脚本统计 data.csv…
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import yaml

from app.agents.skills.models import CLEANUP_AUTO, CleanupRule, ErrorRule, SkillContext, SkillMeta, SopStep
from app.agents.skills.registry import find_skill

__all__ = ["load_skill_context", "load_skill_context_by_id"]

#: SOP 步骤标题：## step-01 标题文本
_STEP_HEAD = re.compile(r"^##\s+(\S+)\s*(.*)$", re.MULTILINE)
#: YAML 代码块围栏（可带 yaml/yml 语言标记）
_YAML_FENCE = re.compile(r"^```(?:yaml|yml)?\s*$", re.MULTILINE)


def _read_sync(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


async def _read(path: Path) -> str:
    return await asyncio.to_thread(_read_sync, path)


def _safe_load_yaml(text: str, *, what: str) -> dict | list | None:
    """解析 YAML；失败返回 None（不抛，保持加载其余部分的能力）。"""
    try:
        data = yaml.safe_load(text)
        return data if isinstance(data, (dict, list)) else None
    except yaml.YAMLError:
        return None


def _normalize_records(data: dict | list | None, key: str) -> list[dict]:
    """接受 ``{key: [...]}`` 或裸列表两种形态。"""
    if isinstance(data, dict):
        items = data.get(key) or []
    elif isinstance(data, list):
        items = data
    else:
        return []
    return [d for d in items if isinstance(d, dict)]


def _parse_sop(text: str) -> list[SopStep]:
    """解析 SOP.md：按 ``## step-xx`` 分段；步骤首部 YAML 块提供 script/tool。"""
    steps: list[SopStep] = []
    matches = list(_STEP_HEAD.finditer(text))
    for i, m in enumerate(matches):
        step_id = m.group(1)
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip()

        # 提取第一个 fenced YAML 块作为机器字段
        script, tool = "", ""
        args_required, args_hint = False, ""
        fence_start = _YAML_FENCE.search(body)
        if fence_start:
            fence_end = _YAML_FENCE.search(body, fence_start.end())
            if fence_end:
                meta = _safe_load_yaml(body[fence_start.end() : fence_end.start()], what="SOP step meta")
                if isinstance(meta, dict):
                    script = str(meta.get("script", "") or "")
                    tool = str(meta.get("tool", "") or "")
                    args_required = bool(meta.get("args_required", False))
                    args_hint = str(meta.get("args_hint", "") or "")
                body = (body[: fence_start.start()] + body[fence_end.end() :]).strip()

        steps.append(
            SopStep(
                step=step_id,
                title=m.group(2).strip(),
                description=body[:800],
                script=script,
                tool=tool,
                args_required=args_required,
                args_hint=args_hint,
            )
        )
    return steps


def _parse_errors(text: str) -> list[ErrorRule]:
    """解析 errors.yaml：记录字段 code/condition/action/recovery/note。"""
    data = _safe_load_yaml(text, what="errors.yaml")
    rules: list[ErrorRule] = []
    for rec in _normalize_records(data, "errors"):
        action = str(rec.get("action", "retry") or "retry").lower()
        rules.append(
            ErrorRule(
                code=str(rec.get("code", "") or ""),
                condition=str(rec.get("condition", "") or ""),
                action=action,
                recovery=str(rec.get("recovery", "") or ""),
                note=str(rec.get("note", "") or ""),
            )
        )
    # 丢弃 code 为空的非法记录
    return [r for r in rules if r.code]


def _parse_cleanup(text: str) -> list[CleanupRule]:
    """解析 cleanup.yaml：记录字段 path/kind/level/note。"""
    data = _safe_load_yaml(text, what="cleanup.yaml")
    rules: list[CleanupRule] = []
    for rec in _normalize_records(data, "cleanups"):
        level = str(rec.get("level", CLEANUP_AUTO) or CLEANUP_AUTO).lower()
        if level not in ("auto", "confirm", "forbidden"):
            level = CLEANUP_AUTO
        rules.append(
            CleanupRule(
                path=str(rec.get("path", "") or ""),
                kind=str(rec.get("kind", "file") or "file").lower(),
                level=level,
                note=str(rec.get("note", "") or ""),
            )
        )
    return [r for r in rules if r.path]


async def load_skill_context(meta: SkillMeta) -> SkillContext:
    """加载一个 skill 的完整上下文（SOP / errors / cleanup）。"""
    sop_text, err_text, clean_text = await asyncio.gather(
        _read(meta.dir / meta.sop_file),
        _read(meta.dir / meta.errors_file),
        _read(meta.dir / meta.cleanup_file),
    )
    return SkillContext(
        meta=meta,
        sop_steps=_parse_sop(sop_text),
        error_rules=_parse_errors(err_text),
        cleanup_rules=_parse_cleanup(clean_text),
    )


async def load_skill_context_by_id(skill_id: str) -> SkillContext | None:
    """按 skill_id 加载上下文；skill 不存在返回 None。"""
    meta = find_skill(skill_id)
    if meta is None:
        return None
    return await load_skill_context(meta)
