"""语雀（Yuque）文档拉取客户端（Open API v2）。

负责从语雀拉取知识库与文档（Markdown 原文），供知识库摄取服务（app.rag）使用。

端点约定（https://www.yuque.com/api/v2）：
  - GET /users/{login}/repos                  用户的知识库列表（分页）
  - GET /groups/{login}/repos                 组织的知识库列表（分页）
  - GET /repos/{namespace}/docs               知识库下的文档列表（分页）
  - GET /repos/{namespace}/docs/{slug}?raw=1  文档 Markdown 原文

鉴权：语雀个人令牌，通过 ``X-Auth-Token`` 请求头传递。
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

import httpx

__all__ = ["YuqueClient", "YuqueError", "YuqueRepo", "YuqueDoc"]


class YuqueError(Exception):
    """语雀 API 异常。"""

    def __init__(self, status: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.data = data


@dataclass
class YuqueRepo:
    """语雀知识库。"""

    id: int
    name: str
    namespace: str
    slug: str
    description: str = ""
    type: str = "Book"
    updated_at: str = ""
    user_id: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> YuqueRepo:
        return cls(
            id=int(d.get("id") or 0),
            name=str(d.get("name") or ""),
            namespace=str(d.get("namespace") or ""),
            slug=str(d.get("slug") or ""),
            description=str(d.get("description") or ""),
            type=str(d.get("type") or "Book"),
            updated_at=str(d.get("updated_at") or ""),
            user_id=int(d.get("user_id") or 0),
        )


@dataclass
class YuqueDoc:
    """语雀文档（含 Markdown 正文）。"""

    id: int
    slug: str
    title: str
    description: str = ""
    url: str = ""
    namespace: str = ""
    word_count: int = 0
    created_at: str = ""
    updated_at: str = ""
    content_md: str = ""
    body_html: str = ""

    @classmethod
    def from_dict(cls, d: dict, *, namespace: str = "", content_md: str = "", body_html: str = "") -> YuqueDoc:
        return cls(
            id=int(d.get("id") or 0),
            slug=str(d.get("slug") or ""),
            title=str(d.get("title") or ""),
            description=str(d.get("description") or ""),
            url=str(d.get("url") or ""),
            namespace=namespace or str(d.get("namespace") or ""),
            word_count=int(d.get("word_count") or 0),
            created_at=str(d.get("created_at") or ""),
            updated_at=str(d.get("updated_at") or ""),
            content_md=content_md,
            body_html=body_html,
        )


class YuqueClient:
    """语雀 Open API v2 客户端（异步）。

    Args:
        token: 语雀个人令牌（``X-Auth-Token``）。
        login: 用于列出用户知识库的用户登录名；为空时 list_repos 需要显式传入。
        base_url: API 基础地址，默认 https://www.yuque.com/api/v2。
        timeout: 请求超时（秒）。
        max_retries: 失败重试次数（指数退避；429 / 5xx 重试）。
        transport: httpx transport（测试注入用）。
    """

    def __init__(
        self,
        token: str,
        login: str = "",
        *,
        base_url: str = "https://www.yuque.com/api/v2",
        timeout: float = 30.0,
        max_retries: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not token:
            raise ValueError("Yuque token is required")
        self.token = token
        self.login = login
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """惰性创建共享 AsyncClient（带鉴权头）。"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers={
                    "X-Auth-Token": self.token,
                    "User-Agent": "deer-agent-kb-ingest/0.1",
                },
                timeout=self.timeout,
                transport=self._transport,
            )
        return self._client

    async def aclose(self) -> None:
        """释放底层连接。"""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def _get(self, path: str, params: dict | None = None) -> Any:
        """带重试与错误处理的 GET 请求，返回 ``data`` 字段。"""
        client = await self._get_client()
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = await client.get(f"{self.base_url}{path}", params=params)
                if resp.status_code == 429:
                    # 语雀限流：指数退避后重试
                    last_exc = YuqueError(resp.status_code, "yuque rate limited")
                    if attempt < self.max_retries - 1:
                        await asyncio.sleep(2**attempt)
                    continue
                resp.raise_for_status()
                payload = resp.json()
                if not isinstance(payload, dict) or "data" not in payload:
                    return payload
                return payload["data"] if payload["data"] is not None else {}
            except httpx.HTTPStatusError as exc:
                last_exc = YuqueError(exc.response.status_code, f"yuque api error: {exc}")
                # 4xx 不重试；429 / 5xx 重试
                if exc.response.status_code < 500:
                    break
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(2**attempt)
            except httpx.TransportError as exc:
                last_exc = exc
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(2**attempt)
        raise YuqueError(0, f"yuque request failed: {last_exc}") from last_exc

    # ------------------------------------------------------------------ 知识库

    async def list_repos(self, login: str = "", *, page: int = 1, page_size: int = 50) -> tuple[list[YuqueRepo], bool]:
        """列出用户的知识库。

        Returns:
            (repos, has_more)：has_more 表示可能还有下一页。
        """
        login = login or self.login
        if not login:
            raise YuqueError(0, "list_repos 需要 login（构造 YuqueClient 或调用时传入）")
        data = await self._get(f"/users/{login}/repos", params={"page": page, "page_size": page_size})
        repos = [YuqueRepo.from_dict(d) for d in data if isinstance(d, dict)]
        return repos, len(repos) >= page_size

    async def list_group_repos(self, login: str = "", *, page: int = 1, page_size: int = 50) -> tuple[list[YuqueRepo], bool]:
        """列出组织（团队）的知识库。"""
        login = login or self.login
        if not login:
            raise YuqueError(0, "list_group_repos 需要 login")
        data = await self._get(f"/groups/{login}/repos", params={"page": page, "page_size": page_size})
        repos = [YuqueRepo.from_dict(d) for d in data if isinstance(d, dict)]
        return repos, len(repos) >= page_size

    # ------------------------------------------------------------------ 文档

    async def list_docs(self, namespace: str, *, page: int = 1, page_size: int = 100) -> tuple[list[YuqueDoc], bool]:
        """列出知识库（namespace，如 ``org/repo``）下的文档摘要。

        Returns:
            (docs, has_more)。
        """
        data = await self._get(f"/repos/{namespace}/docs", params={"page": page, "page_size": page_size})
        docs = [YuqueDoc.from_dict(d, namespace=namespace) for d in data if isinstance(d, dict)]
        return docs, len(docs) >= page_size

    async def get_document(self, namespace: str, slug: str) -> YuqueDoc:
        """获取文档详情与 Markdown 原文（``raw=1``）。

        ``data.body`` 为 Markdown 原文，``data.body_html`` 为 HTML。
        """
        data = await self._get(f"/repos/{namespace}/docs/{slug}", params={"raw": 1})
        if not isinstance(data, dict):
            raise YuqueError(0, f"document not found: {namespace}/{slug}")
        return YuqueDoc.from_dict(
            data,
            namespace=namespace,
            content_md=str(data.get("body") or ""),
            body_html=str(data.get("body_html") or ""),
        )

    # ------------------------------------------------------------------ 图片

    async def download_image(self, url: str, *, timeout: float = 30.0) -> bytes:
        """下载文档图片字节（带鉴权头，兼容私域图片）。"""
        client = await self._get_client()
        resp = await client.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.content


async def main() -> None:
    """命令行演示：拉取用户第一个知识库的第一篇文档并打印标题与前 200 字。"""
    token = os.getenv("YUQUE_TOKEN", "")
    login = os.getenv("YUQUE_LOGIN", "")
    if not token:
        print("请先设置环境变量 YUQUE_TOKEN（语雀个人令牌）")
        return
    client = YuqueClient(token=token, login=login)
    try:
        repos, _ = await client.list_repos()
        print(f"共 {len(repos)} 个知识库")
        if not repos:
            return
        ns = repos[0].namespace
        docs, _ = await client.list_docs(ns)
        print(f"知识库 {ns} 共 {len(docs)} 篇文档")
        if not docs:
            return
        doc = await client.get_document(ns, docs[0].slug)
        print(f"标题：{doc.title}\n正文预览：{doc.content_md[:200]}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
