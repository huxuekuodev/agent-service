"""知识库摄取（RAG 数据流水线）。

负责：语雀拉取 → 语义分块 → 文档内图片转文字/分类 → 向量化 → 写入 ElasticSearch。
独立于主对话 Agent（planner-execute），由后台任务 / HTTP 接口触发。
"""

from app.rag.embedding import EmbeddingClient
from app.rag.es_store import ElasticsearchStore
from app.rag.image_classifier import classify_image_description, describe_image_from_bytes
from app.rag.ingest_service import KnowledgeIngestService
from app.rag.models import DocChunk, ImageDescription, IngestStats
from app.rag.splitter import split_markdown_document

__all__ = [
    "KnowledgeIngestService",
    "ElasticsearchStore",
    "EmbeddingClient",
    "split_markdown_document",
    "classify_image_description",
    "describe_image_from_bytes",
    "DocChunk",
    "ImageDescription",
    "IngestStats",
]
