"""语雀类工具：newest_doc（最新修订 vs 历史版本对比）。

业务分类：yuque。

语义：取指定日期（默认今天）**之前**发布的最新一个版本（通常是昨天修改的版本）
与其前一个历史版本（前天，或更早的某一天——只要小于目标日期即可），
返回两者完整内容，供 agent 对比「昨天改了什么」。

依赖 app.model.data.yuque.YuqueClient（语雀 Open API v2）：
  - GET /doc_versions?doc_id={id}   历史版本列表（按时间倒序）
  - GET /doc_versions/{id}          版本详情（含正文 body_md / diff）

``make_newest_doc_tool`` 从 config.yaml ``tools[].extra`` 读取配置：

    extra:
      token: $YUQUE_TOKEN      # 必填，语雀个人令牌
      login: $YUQUE_LOGIN      # 可选，默认登录名（本工具不需要）
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from langchain.tools import BaseTool, tool

from app.model.data.yuque import YuqueClient, YuqueDocVersion, YuqueDocVersionDetail


def _to_local_date(iso: str) -> date | None:
    """把语雀 ISO8601 时间（UTC）转成本地日期；解析失败返回 None。"""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().date()
    except (ValueError, TypeError):
        return None


def _parse_target_date(target_date: str) -> date:
    """解析目标日期（YYYY-MM-DD）；为空时默认今天（本地时区）。"""
    if not target_date:
        return datetime.now().astimezone().date()
    try:
        return datetime.strptime(target_date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"target_date 格式应为 YYYY-MM-DD，收到: {target_date!r}") from exc


def _version_body(version: YuqueDocVersion, detail: YuqueDocVersionDetail) -> str:
    """取版本正文（优先 Markdown 标准格式，回退原始正文）。"""
    return (detail.body_md or detail.body or "").strip() or "(版本正文为空)"


def _format_version(version: YuqueDocVersion, detail: YuqueDocVersionDetail) -> str:
    """格式化为「修订时间 + 完整正文」。"""
    return f"修订时间: {version.created_at or '(未知)'}\n{_version_body(version, detail)}"


def make_newest_doc_tool(**extra: Any) -> BaseTool:
    """工具工厂：按 extra 配置创建绑定语雀令牌的 newest_doc 工具。

    Args:
        extra: config.yaml ``tools[].extra``：
            - token: 语雀个人令牌（必填）
            - login: 默认登录名（可选）

    Returns:
        绑定 token 的 StructuredTool（同名 newest_doc）。
    """
    token = str(extra.get("token") or "")
    if not token:
        raise ValueError("newest_doc 需要配置 extra.token（建议 .env 设置 YUQUE_TOKEN）")
    login = str(extra.get("login") or "")

    @tool("newest_doc", parse_docstring=True)
    async def newest_doc_tool(doc_id: int, target_date: str = "") -> str:
        """Get the latest published revision of a Yuque doc and the historical version before it.

        Use this tool to compare the most recent revision (e.g. edited yesterday) of a
        Yuque document with an earlier version, returning the full content of both, so
        you can tell what changed and when.

        Args:
            doc_id: The Yuque document ID (integer).
            target_date: Optional cutoff date "YYYY-MM-DD" (default today). Only revisions
                published strictly before this date are considered; the newest one and the
                one right before it are returned.
        """
        target = _parse_target_date(target_date)
        client = YuqueClient(token=token, login=login)
        try:
            versions = await client.list_doc_versions(doc_id)
            # 只保留 target_date 之前（不含当天）发布的版本（新版本在前）
            before_target = [v for v in versions if (d := _to_local_date(v.created_at)) is not None and d < target]
            if not before_target:
                return f"文档 doc_id={doc_id} 在 {target} 之前没有已发布的历史版本。"

            latest = before_target[0]
            prev = before_target[1] if len(before_target) > 1 else None

            latest_detail = await client.get_doc_version(latest.id)
            parts = [
                f"文档「{latest_detail.title or latest.title}」修订对比（doc_id={doc_id}，{target} 之前的最新修订）",
                "",
                f"【最新修订全部内容】\n{_format_version(latest, latest_detail)}",
            ]
            if latest_detail.diff:
                parts.append(f"\n【最新修订相对上一版的差异 diff】\n{latest_detail.diff}")

            if prev is None:
                parts.append(f"\n（{target} 之前仅有 1 个版本，无更早的历史版本可对比）")
                return "\n".join(parts)

            prev_detail = await client.get_doc_version(prev.id)
            parts.append(f"\n【历史版本全部内容】\n{_format_version(prev, prev_detail)}")
            return "\n".join(parts)
        finally:
            await client.aclose()

    return newest_doc_tool
