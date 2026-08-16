"""ElasticSearch 向量库存储（基于 httpx，无需额外 SDK）。

按照 docs/RAG_方案.md 的 mapping 设计，使用 ES 8.x ``dense_vector`` + ``knn``：

  - ``create_index`` 建立索引（dense_vector + metadata）。
  - ``reindex`` 重建索引（删除旧索引后重建），用于全量同步。
  - ``bulk_write`` 批量写入 chunk（含 embedding 向量）。
  - ``search`` 做 knn 语义检索（供后续 rag_search 工具复用）。

说明：项目依赖已含 httpx，直接调用 ES REST API，避免引入 ``elasticsearch``
SDK 依赖；接口语义与官方客户端对齐，便于日后替换。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.config import ElasticsearchConfig
from app.core.log import logger

__all__ = ["ElasticsearchStore", "EsError"]


class EsError(Exception):
    """ES 操作异常。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class ElasticsearchStore:
    """ElasticSearch 向量库读写（异步）。

    Args:
        config: ElasticsearchConfig（url / index / dims / 鉴权）。
        timeout: 请求超时（秒）。
    """

    def __init__(self, config: ElasticsearchConfig, *, timeout: float = 30.0) -> None:
        self.config = config
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {"Content-Type": "application/json"}
            auth: httpx.BasicAuth | None = None
            if self.config.username:
                auth = httpx.BasicAuth(self.config.username, self.config.password or "")
            self._client = httpx.AsyncClient(
                base_url=self.config.url.rstrip("/"),
                headers=headers,
                auth=auth,
                timeout=self.timeout,
                verify=self.config.verify_certs,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # ------------------------------------------------------------------ 索引

    def _mapping(self) -> dict[str, Any]:
        """dense_vector 索引 mapping（维度取自配置）。"""
        dims = self.config.dims
        return {
            "mappings": {
                "properties": {
                    "content": {"type": "text"},
                    "content_vector": {
                        "type": "dense_vector",
                        "dims": dims,
                        "index": True,
                        "similarity": "cosine",
                    },
                    "metadata": {
                        "properties": {
                            "source": {"type": "keyword"},
                            "doc_id": {"type": "keyword"},
                            "doc_title": {"type": "keyword"},
                            "url": {"type": "keyword"},
                            "namespace": {"type": "keyword"},
                            "chunk_index": {"type": "integer"},
                            "title_path": {"type": "keyword"},
                            "images": {"type": "object"},
                            "updated_at": {"type": "keyword"},
                        }
                    },
                }
            }
        }

    async def index_exists(self, index: str | None = None) -> bool:
        client = await self._get_client()
        index = index or self.config.index
        resp = await client.head(f"/{index}")
        return resp.status_code == 200

    async def create_index(self, index: str | None = None) -> None:
        """创建索引（若已存在则跳过）。"""
        client = await self._get_client()
        index = index or self.config.index
        if await self.index_exists(index):
            return
        resp = await client.put(f"/{index}", content=json.dumps(self._mapping()))
        if resp.status_code not in (200, 201):
            raise EsError(resp.status_code, f"create index failed: {resp.text}")
        logger.info("ES 索引已创建: {}", index)

    async def delete_index(self, index: str | None = None) -> None:
        """删除索引（不存在时静默）。"""
        client = await self._get_client()
        index = index or self.config.index
        resp = await client.delete(f"/{index}")
        if resp.status_code not in (200, 404):
            raise EsError(resp.status_code, f"delete index failed: {resp.text}")

    async def reindex(self, index: str | None = None) -> None:
        """重建索引：删除旧索引后重新创建（全量同步用）。"""
        index = index or self.config.index
        await self.delete_index(index)
        await self.create_index(index)
        logger.info("ES 索引已重建: {}", index)

    # ------------------------------------------------------------------ 写入

    async def bulk_write(self, docs: list[dict[str, Any]], *, chunk_size: int = 500) -> int:
        """批量写入文档（含向量）。返回成功写入条数。

        Args:
            docs: 每个元素为 ``{content, content_vector, metadata}``。
            chunk_size: 每次 _bulk 的条数。
        """
        if not docs:
            return 0
        client = await self._get_client()
        index = self.config.index
        written = 0
        for start in range(0, len(docs), chunk_size):
            batch = docs[start : start + chunk_size]
            payload_lines: list[str] = []
            for doc in batch:
                payload_lines.append(json.dumps({"index": {"_index": index}}))
                payload_lines.append(json.dumps(doc, ensure_ascii=False, default=str))
            body = "\n".join(payload_lines) + "\n"
            resp = await client.post(
                "/_bulk",
                content=body,
                headers={"Content-Type": "application/x-ndjson"},
            )
            if resp.status_code >= 400:
                raise EsError(resp.status_code, f"bulk write failed: {resp.text}")
            data = resp.json()
            if data.get("errors"):
                errors = data.get("items") or []
                logger.warning("ES bulk 部分写入失败: {} 条", sum(1 for it in errors if it.get("index", {}).get("error")))
            written += len(batch)
        logger.info("ES 写入 {} 条 chunk", written)
        return written

    # ------------------------------------------------------------------ 检索

    async def search(self, query_vector: list[float], *, top_k: int = 5, namespace: str | None = None) -> list[dict[str, Any]]:
        """knn 语义检索。

        Args:
            query_vector: 查询向量（与 embedding 维度一致）。
            top_k: 返回条数。
            namespace: 可选，按知识库过滤。

        Returns:
            命中的 chunk 列表（含 metadata）。
        """
        client = await self._get_client()
        knn: dict[str, Any] = {
            "field": "content_vector",
            "query_vector": query_vector,
            "k": top_k,
            "num_candidates": max(top_k * 10, 50),
        }
        query: dict[str, Any] = {"knn": knn}
        if namespace:
            query["query"] = {"term": {"metadata.namespace": namespace}}
        resp = await client.post(f"/{self.config.index}/_search", content=json.dumps(query))
        if resp.status_code >= 400:
            raise EsError(resp.status_code, f"search failed: {resp.text}")
        hits = (resp.json().get("hits") or {}).get("hits") or []
        return [h.get("_source") for h in hits]
