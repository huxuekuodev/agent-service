"""知识库类工具：internal_kb（对接私有/外部知识库检索）。

业务分类：knowledge。

工具的 API 地址、鉴权等敏感配置通过 config.yaml ``tools[].extra`` 注入，
与工具实现解耦（参考 n8n/Dify 的凭证分离思路）。

``make_internal_kb_tool`` 从 config.yaml ``tools[].extra`` 读取配置：

    extra:
      endpoint: $KB_ENDPOINT     # 必填，知识库检索接口地址
      api_key: $KB_API_KEY       # 必填，鉴权 key
"""

from __future__ import annotations

from typing import Any

import httpx
from langchain.tools import BaseTool, tool


def make_internal_kb_tool(**extra: Any) -> BaseTool:
    """工具工厂：按 extra 配置创建知识库检索工具。

    Args:
        extra: config.yaml ``tools[].extra``：
            - endpoint: 知识库检索接口地址（必填）
            - api_key: 鉴权 key（必填）

    Returns:
        绑定 endpoint/api_key 的 StructuredTool（同名 internal_kb）。
    """
    endpoint = str(extra.get("endpoint") or "")
    api_key = str(extra.get("api_key") or "")
    if not endpoint or not api_key:
        raise ValueError("internal_kb 需要配置 extra.endpoint 与 extra.api_key （建议 .env 设置 KB_ENDPOINT / KB_API_KEY）")

    @tool("internal_kb", parse_docstring=True)
    async def internal_kb_tool(query: str, top_k: int = 3) -> str:
        """Search the internal knowledge base and return relevant excerpts.

        Use this tool when the answer may live in the company's private knowledge
        base, such as internal docs, SOPs, or product manuals.

        Args:
            query: The search query string.
            top_k: Number of top excerpts to return (default 3).
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{endpoint.rstrip('/')}/search",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"query": query, "top_k": top_k},
            )
            resp.raise_for_status()
            data = resp.json()
            hits = data.get("results") or data.get("hits") or []
            if not hits:
                return "未找到相关知识库内容。"
            lines = [f"- {h.get('title') or '(无标题)'}\n  URL: {h.get('url') or ''}\n  {(h.get('content') or h.get('text') or '').strip()}" for h in hits[:top_k]]
            return "\n\n".join(lines)

    return internal_kb_tool
