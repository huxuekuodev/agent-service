"""配置解析（新增段）与摄取流水线数据模型单元测试。"""

from __future__ import annotations

import os

from app.config import AppConfig, YuqueConfig
from app.rag.ingest_service import _merge_image_descriptions
from app.rag.models import DocChunk, ImageDescription


def test_config_parses_new_sections() -> None:
    data = {
        "models": {"default": "deepseek", "vision_model": "qwen_vl"},
        "yuque": {"enabled": True, "token": "$YUQUE_TOKEN", "login": "huxuekuo", "group_repos": False, "namespaces": ["org/repo"]},
        "ingest": {"enabled": True, "vision_model": "vision_model", "chunk_size": 1500, "overlap": 100, "download_images": True, "auto_interval_seconds": 3600, "parallel": 3},
        "elasticsearch": {"url": "http://localhost:9200", "index": "rag_docs", "dims": 1024, "reindex_on_ingest": True},
        "embedding": {"model": "text-embedding-3-small", "api_key": "$EMBEDDING_API_KEY"},
    }
    cfg = AppConfig.from_dict(data)
    assert isinstance(cfg.yuque, YuqueConfig)
    assert cfg.models == {"default": "deepseek", "vision_model": "qwen_vl"}
    assert cfg.default_model_name == "default"
    assert cfg.get_model_config("plan_node_model") is None
    assert cfg.yuque.token == os.getenv("YUQUE_TOKEN", "")
    assert cfg.yuque.namespaces == ["org/repo"]
    assert cfg.ingest.chunk_size == 1500
    assert cfg.ingest.auto_interval_seconds == 3600
    assert cfg.ingest.parallel == 3
    assert cfg.elasticsearch.dims == 1024
    assert cfg.elasticsearch.index == "rag_docs"
    assert cfg.embedding.model == "text-embedding-3-small"


def test_merge_image_descriptions_replaces_placeholder() -> None:
    chunks = [
        DocChunk(
            doc_id="org/repo/d",
            doc_title="t",
            title_path="标题/子标题",
            content="正文 ![image](https://img.yuque.com/a.png) 结束",
            chunk_index=0,
        )
    ]
    desc = ImageDescription(url="https://img.yuque.com/a.png", index=0, title_path="标题/子标题", kind="flowchart", description="请求流程从开始到结束")
    merged = _merge_image_descriptions(chunks, [desc])
    assert len(merged[0].images) == 1
    assert merged[0].images[0].kind == "flowchart"
    assert "请求流程从开始到结束" in merged[0].content
    assert "![image](https://img.yuque.com/a.png)" not in merged[0].content


def test_merge_image_descriptions_failed_keeps_placeholder() -> None:
    chunks = [DocChunk(doc_id="org/repo/d", doc_title="t", title_path="", content="正文 ![image](https://img.yuque.com/a.png)", chunk_index=0)]
    desc = ImageDescription(url="https://img.yuque.com/a.png", index=0, error="timeout")
    merged = _merge_image_descriptions(chunks, [desc])
    assert merged[0].images == []
    assert "![image](https://img.yuque.com/a.png)" in merged[0].content
