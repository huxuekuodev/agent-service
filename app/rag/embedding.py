"""Embedding 客户端：将文本向量化。

通过 OpenAI 兼容的 ``/embeddings`` 接口调用，支持任意 provider
（OpenAI / DeepSeek / 本地 OpenAI 兼容服务）。api_key / base_url 从配置注入。
"""

from __future__ import annotations

import httpx

from app.config import EmbeddingConfig

__all__ = ["EmbeddingClient", "EmbeddingError"]


class EmbeddingError(Exception):
    """Embedding 调用异常。"""


class EmbeddingClient:
    """OpenAI 兼容 embedding 客户端（异步）。

    Args:
        config: EmbeddingConfig（model / api_key / base_url / dimensions）。
    """

    def __init__(self, config: EmbeddingConfig) -> None:
        self.config = config
        self.base_url = (config.base_url or "https://api.openai.com/v1").rstrip("/")
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {"Content-Type": "application/json"}
            if self.config.api_key:
                headers["Authorization"] = f"Bearer {self.config.api_key}"
            self._client = httpx.AsyncClient(headers=headers, timeout=30.0)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量向量化文本，返回每个文本的向量列表。"""
        if not texts:
            return []
        client = await self._get_client()
        resp = await client.post(
            f"{self.base_url}/embeddings",
            json={
                "model": self.config.model,
                "input": texts,
            },
        )
        if resp.status_code >= 400:
            raise EmbeddingError(f"embedding failed: {resp.status_code} {resp.text}")
        data = resp.json()
        items = data.get("data") or []
        items.sort(key=lambda d: d.get("index", 0))
        return [item.get("embedding") for item in items]

    async def embed_one(self, text: str) -> list[float]:
        """向量化单条文本。"""
        result = await self.embed([text])
        return result[0] if result else []
