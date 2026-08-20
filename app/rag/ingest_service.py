"""知识库摄取服务：语雀拉取 → 分块 → 图片转文字/分类 → 向量化 → 入 ES。

独立于主对话 Agent 的知识库更新通道，支持：
  - 手动触发（HTTP 接口 / 直接调用）
  - 后台定时自动同步（FastAPI lifespan 启动的 asyncio 任务）

流水线（单个文档）：
  1. 拉取 Markdown 原文（YuqueClient）
  2. 语义分块（splitter），同时抽取全文图片引用
  3. 图片下载 → 视觉 LLM 转文字 + 分类（image_classifier），失败时保留占位
  4. 图片转写文本合并回对应 chunk
  5. 批量 embedding → bulk 写入 ES
"""

from __future__ import annotations

import asyncio
from datetime import UTC
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from app.config import AppConfig
from app.core.log import logger
from app.model.data.yuque.yuque import YuqueClient, YuqueDoc, YuqueError, YuqueRepo
from app.rag.embedding import EmbeddingClient
from app.rag.es_store import ElasticsearchStore
from app.rag.image_classifier import classify_image_description, describe_image_from_bytes
from app.rag.models import DocChunk, ImageDescription, IngestStats
from app.rag.splitter import ImageRef, split_markdown_document


class KnowledgeIngestService:
    """知识库摄取服务（异步）。

    Args:
        app_config: 全局配置（yuque / ingest / elasticsearch / embedding 段）。
        yuque: 已注入的 YuqueClient；None 时按配置惰性创建。
        vision_llm: 视觉 LLM（图片转文字）；None 时按配置惰性创建。
        es_store: ES 写入器；None 时按配置惰性创建。
        embedding: embedding 客户端；None 时按配置惰性创建。
    """

    def __init__(
        self,
        *,
        app_config: AppConfig,
        yuque: YuqueClient | None = None,
        vision_llm: BaseChatModel | None = None,
        es_store: ElasticsearchStore | None = None,
        embedding: EmbeddingClient | None = None,
    ) -> None:
        self.config = app_config
        self.yuque = yuque
        self._vision_llm = vision_llm
        self._es_store = es_store
        self._embedding = embedding
        self._owns_yuque = yuque is None
        self._owns_vision = vision_llm is None
        self._owns_es = es_store is None
        self._owns_embedding = embedding is None

        # 定时任务状态
        self._auto_task: asyncio.Task | None = None
        self._last_run: dict[str, Any] | None = None

    # ------------------------------------------------------------------ 懒加载

    def _get_yuque(self) -> YuqueClient:
        if self.yuque is None:
            cfg = self.config.yuque
            if not cfg.token:
                raise RuntimeError("yuque.token 未配置（config.yaml / YUQUE_TOKEN）")
            self.yuque = YuqueClient(token=cfg.token, login=cfg.login)
        return self.yuque

    def _get_vision_llm(self) -> BaseChatModel:
        """创建视觉 LLM（基于 config.ingest.vision_model / models 中的视觉实例）。"""
        if self._vision_llm is None:
            vision_role = self.config.get_vision_model()
            if vision_role is None:
                raise RuntimeError("未配置视觉模型：请在 models 中配置指向 supports_vision 实例的角色，或在 ingest.vision_model 指定角色名")
            from app.llm import create_chat_model

            self._vision_llm = create_chat_model(name=vision_role, app_config=self.config)
        return self._vision_llm

    def _get_es(self) -> ElasticsearchStore:
        if self._es_store is None:
            self._es_store = ElasticsearchStore(self.config.elasticsearch)
        return self._es_store

    def _get_embedding(self) -> EmbeddingClient:
        if self._embedding is None:
            self._embedding = EmbeddingClient(self.config.embedding)
        return self._embedding

    # ------------------------------------------------------------------ 公开入口

    async def ingest_all(self, *, namespaces: list[str] | None = None, reindex: bool | None = None) -> IngestStats:
        """执行一次完整摄取：拉取全部知识库 → 分块 → 图片转文字 → 入 ES。

        Args:
            namespaces: 需要同步的 namespace 白名单；None 用 config.yuque.namespaces。
            reindex: 是否重建 ES 索引；None 用 config.elasticsearch.reindex_on_ingest。

        Returns:
            IngestStats 统计信息。
        """
        stats = IngestStats()
        yuque = self._get_yuque()
        es = self._get_es()

        repos = await self._list_repos(yuque, stats)
        if not repos:
            logger.warning("语雀未拉取到任何知识库")
            return stats

        if reindex is None:
            reindex = self.config.elasticsearch.reindex_on_ingest
        if reindex:
            await es.reindex()
        else:
            await es.create_index()

        # 并行处理各知识库文档（每库一个并发任务）
        sem = asyncio.Semaphore(self.config.ingest.parallel)
        tasks = [self._ingest_repo(yuque, es, repo, stats, sem) for repo in repos]
        await asyncio.gather(*tasks, return_exceptions=True)

        self._last_run = {"stats": stats.model_dump(), "finished_at": _now_iso()}
        return stats

    async def _list_repos(self, yuque: YuqueClient, stats: IngestStats) -> list[YuqueRepo]:
        """按配置拉取知识库列表（用户/组织 + 白名单过滤）。"""
        cfg = self.config.yuque
        namespaces = set(cfg.namespaces)
        repos: list[YuqueRepo] = []
        try:
            if cfg.group_repos:
                fetched, _ = await yuque.list_group_repos()
            else:
                fetched, _ = await yuque.list_repos()
            for repo in fetched:
                if namespaces and repo.namespace not in namespaces:
                    continue
                repos.append(repo)
        except YuqueError as exc:
            stats.add_error(f"拉取知识库列表失败: {exc.message}")
            logger.error("拉取知识库列表失败: {}", exc.message)
        return repos

    async def _ingest_repo(
        self,
        yuque: YuqueClient,
        es: ElasticsearchStore,
        repo: YuqueRepo,
        stats: IngestStats,
        sem: asyncio.Semaphore,
    ) -> None:
        """摄取单个知识库（串行拉取文档，逐篇处理）。"""
        stats.repos_processed += 1
        logger.info("开始处理知识库: {}", repo.namespace)
        try:
            page = 1
            while True:
                docs, has_more = await yuque.list_docs(repo.namespace, page=page)
                for doc in docs:
                    async with sem:
                        await self._ingest_doc(yuque, es, repo, doc, stats)
                stats.docs_fetched += len(docs)
                if not has_more:
                    break
                page += 1
        except YuqueError as exc:
            stats.add_error(f"知识库 {repo.namespace} 拉取失败: {exc.message}")
            logger.error("知识库 {} 拉取失败: {}", repo.namespace, exc.message)

    async def _ingest_doc(
        self,
        yuque: YuqueClient,
        es: ElasticsearchStore,
        repo: YuqueRepo,
        doc: YuqueDoc,
        stats: IngestStats,
    ) -> None:
        """处理单篇文档：拉原文 → 分块 → 图片转文字 → 向量化 → 写 ES。"""
        try:
            full = await yuque.get_document(repo.namespace, doc.slug)
            content = full.content_md or ""
            if not content.strip():
                logger.debug("文档为空跳过: {}/{}", repo.namespace, doc.slug)
                return

            doc_obj = {
                "doc_id": f"{repo.namespace}/{doc.slug}",
                "title": full.title,
                "url": full.url or f"https://www.yuque.com/{repo.namespace}/{doc.slug}",
                "namespace": repo.namespace,
                "updated_at": full.updated_at,
            }
            chunks, image_refs = split_markdown_document(
                doc=doc_obj,
                content=content,
                max_chars=self.config.ingest.chunk_size,
                overlap=self.config.ingest.overlap,
                image_placeholder=self.config.ingest.image_placeholder,
            )
            stats.chunks_created += len(chunks)
            stats.images_detected += len(image_refs)

            # 图片转文字 + 分类（失败不中断主流程）
            descriptions: list[ImageDescription] = []
            if image_refs and self.config.ingest.download_images:
                try:
                    descriptions = await self._transcribe_images(yuque, image_refs)
                except Exception as exc:
                    logger.warning("文档 {} 图片处理失败: {}", doc_obj["doc_id"], exc)
                    stats.add_error(f"{doc_obj['doc_id']} 图片处理失败: {exc}")

            # 把图片转写合并回对应 chunk（按标题路径匹配）
            chunks = _merge_image_descriptions(chunks, descriptions)
            stats.images_transcribed += sum(1 for d in descriptions if d.succeeded)

            # 向量化 + 入 ES
            es_docs = await self._vectorize_and_write(es, chunks, stats)
            stats.es_written += es_docs
            stats.docs_processed += 1
        except Exception as exc:
            logger.error("文档 {} 处理失败: {}", doc.slug, exc)
            stats.add_error(f"{doc.slug} 处理失败: {exc}")

    async def _transcribe_images(self, yuque: YuqueClient, image_refs: list[ImageRef]) -> list[ImageDescription]:
        """下载图片并逐张转文字 + 分类（并发限流）。"""
        llm = self._get_vision_llm()
        sem = asyncio.Semaphore(4)

        async def _one(ref: ImageRef) -> ImageDescription:
            async with sem:
                try:
                    img_bytes = await yuque.download_image(ref.url)
                    # 图片字节已下载，直接走字节分析（可正确识别私域/相对路径图片）
                    desc = await describe_image_from_bytes(llm, img_bytes, url=ref.url, index=ref.index)
                    if not desc.description:
                        desc = await classify_image_description(llm, desc)
                    desc.title_path = ref.title_path
                    return desc
                except Exception as exc:
                    logger.warning("图片 {} 处理失败: {}", ref.url, exc)
                    return ImageDescription(url=ref.url, index=ref.index, title_path=ref.title_path, error=str(exc))

        return await asyncio.gather(*[_one(ref) for ref in image_refs])

    async def _vectorize_and_write(self, es: ElasticsearchStore, chunks: list[DocChunk], stats: IngestStats) -> int:
        """对 chunk 批量 embedding 并写入 ES。"""
        if not chunks:
            return 0
        try:
            embedding = self._get_embedding()
            texts = [c.content for c in chunks]
            vectors = await embedding.embed(texts)
            es_docs = [c.to_es_doc(v) for c, v in zip(chunks, vectors)]
            return await es.bulk_write(es_docs)
        except Exception as exc:
            logger.error("向量化/写入 ES 失败: {}", exc)
            stats.add_error(f"ES 写入失败: {exc}")
            return 0

    # ------------------------------------------------------------------ 定时任务

    async def start_auto_sync(self) -> None:
        """启动后台自动同步（间隔取自 config.ingest.auto_interval_seconds）。"""
        interval = self.config.ingest.auto_interval_seconds
        if interval <= 0 or self._auto_task is not None:
            return
        if not self.config.yuque.enabled:
            logger.warning("yuque.enabled=false，自动同步未启动")
            return

        async def _loop() -> None:
            logger.info("知识库自动同步启动，间隔 {}s", interval)
            while True:
                try:
                    stats = await self.ingest_all()
                    logger.info("自动同步完成: {} 文档 / {} chunk", stats.docs_processed, stats.chunks_created)
                except Exception as exc:
                    logger.error("自动同步失败: {}", exc)
                await asyncio.sleep(interval)

        self._auto_task = asyncio.create_task(_loop())
        logger.info("知识库自动同步已启动")

    async def stop_auto_sync(self) -> None:
        """停止后台自动同步。"""
        if self._auto_task is not None:
            self._auto_task.cancel()
            try:
                await self._auto_task
            except asyncio.CancelledError:
                pass
            self._auto_task = None

    @property
    def last_run(self) -> dict[str, Any] | None:
        return self._last_run

    @property
    def auto_task_running(self) -> bool:
        return self._auto_task is not None

    async def aclose(self) -> None:
        """释放所有底层资源。"""
        await self.stop_auto_sync()
        if self._owns_yuque and self.yuque is not None:
            await self.yuque.aclose()
        if self._owns_es and self._es_store is not None:
            await self._es_store.aclose()
        if self._owns_embedding and self._embedding is not None:
            await self._embedding.aclose()


def _merge_image_descriptions(chunks: list[DocChunk], descriptions: list[ImageDescription]) -> list[DocChunk]:
    """把图片转写结果合并回对应 chunk。

    匹配策略：
      1. 图片优先归入 title_path 完全相同的 chunk；
      2. 无完全匹配时，归入该路径下的第一个子 chunk（前缀匹配，容忍标题切分）；
      3. 图片无路径时归入第一个 chunk（整篇兜底）。
    归入后把正文中的图片占位替换为转写文本，并把图片写入 chunk.images。
    若占位不在第一个匹配 chunk（长 section 被切成多块），退化为记录 images 即可。
    """
    if not descriptions:
        return chunks

    succeeded = [d for d in descriptions if d.succeeded]
    if not succeeded:
        return chunks

    # 预分组：title_path -> chunks
    by_path: dict[str, list[DocChunk]] = {}
    for c in chunks:
        by_path.setdefault(c.title_path, []).append(c)

    for d in succeeded:
        candidates = _match_candidates(d.title_path, by_path, chunks)
        if not candidates:
            continue
        # 优先替换真正包含占位符的 chunk
        target: DocChunk | None = None
        for cand in candidates:
            if d.description and f"![image]({d.url})" in cand.content:
                target = cand
                break
        if target is None:
            target = candidates[0]
        if d not in target.images:
            target.images.append(d)
        if d.description:
            target.content = target.content.replace(f"![image]({d.url})", f"[图片描述：{d.description}]")
    return chunks


def _match_candidates(
    title_path: str,
    by_path: dict[str, list[DocChunk]],
    chunks: list[DocChunk],
) -> list[DocChunk]:
    """按标题路径返回候选 chunk 列表（精确 > 前缀 > 整篇兜底）。"""
    exact = by_path.get(title_path)
    if exact:
        return exact
    if title_path:
        for path in sorted(by_path):
            if path.startswith(title_path):
                return by_path[path]
    return chunks[:1]


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat()
