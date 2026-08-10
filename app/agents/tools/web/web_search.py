"""联网类工具：web_search（基于 Tavily Search API）。

业务分类：web。

直接调用 Tavily Search API（``POST https://api.tavily.com/search``），
依赖项目已有的 httpx，无需额外 SDK。鉴权用 ``Authorization: Bearer <key>``。

``make_web_search_tool`` 从 config.yaml ``tools[].extra`` 读取配置：

    extra:
      api_key: $TAVILY_API_KEY        # 必填，Tavily API Key
      base_url: https://api.tavily.com  # 可选，默认官方端点

返回结果格式化为「标题 / URL / 摘要」列表，供 LLM 直接使用。
"""

from __future__ import annotations

from typing import Any

import httpx
from langchain.tools import BaseTool, tool

_TAVILY_DEFAULT_BASE_URL = "https://api.tavily.com"


def _format_results(data: dict[str, Any], max_results: int) -> str:
    """把 Tavily 响应 results[] 格式化为可读文本。"""
    results = data.get("results") or []
    if not results:
        return "未找到相关结果。"

    lines: list[str] = []
    for item in results[:max_results]:
        title = item.get("title") or "(无标题)"
        url = item.get("url") or ""
        content = (item.get("content") or "").strip()
        lines.append(f"- {title}\n  URL: {url}\n  {content}")
    return "\n\n".join(lines)


def make_web_search_tool(**extra: Any) -> BaseTool:
    """工具工厂：按 extra 配置创建绑定 Tavily 的 web_search 工具。

    Args:
        extra: config.yaml ``tools[].extra``：
            - api_key: Tavily API Key（必填）
            - base_url: 自定义端点（可选）

    Returns:
        绑定 api_key 的 StructuredTool（同名 web_search）。
    """
    api_key = str(extra.get("api_key") or "")
    if not api_key:
        raise ValueError("web_search 需要配置 extra.api_key（建议 .env 设置 TAVILY_API_KEY）")
    base_url = str(extra.get("base_url") or _TAVILY_DEFAULT_BASE_URL)

    @tool("web_search", parse_docstring=True)
    async def web_search_tool(query: str, max_results: int = 5) -> str:
        """Search the web and return the top results for a query.

        Use this tool when the user asks for information that is not available in
        local context, such as current events, real-time data, or external sources.

        Args:
            query: The search query string.
            max_results: Maximum number of results to return (default 5).
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/search",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"query": query, "max_results": max_results},
            )
            resp.raise_for_status()
            return _format_results(resp.json(), max_results)

    return web_search_tool
