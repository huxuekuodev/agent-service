"""知识库摄取数据模型。

描述语雀文档 → 分块 → 图片转文字 → 入库的中间数据结构。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

ImageKind = Literal["flowchart", "feature", "other"]
"""图片分类：流程图 / 功能介绍图 / 其他。"""


class ImageDescription(BaseModel):
    """文档内单张图片的分类与文字描述。"""

    url: str = Field(description="图片 URL")
    index: int = Field(default=0, description="文档内图片序号")
    title_path: str = Field(default="", description="图片所在标题层级路径")
    kind: ImageKind = Field(default="other", description="图片类型：flowchart 流程图 / feature 功能介绍图 / other 其他")
    description: str = Field(default="", description="图片内容的文字描述（LLM 转写）")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="分类置信度 0-1")
    error: str = Field(default="", description="识别失败时的错误信息")

    @property
    def succeeded(self) -> bool:
        return not bool(self.error)


class DocChunk(BaseModel):
    """单个语义分块。"""

    doc_id: str = Field(description="文档唯一标识（namespace/slug）")
    doc_title: str = Field(description="文档标题")
    url: str = Field(default="", description="文档原始 URL")
    namespace: str = Field(default="", description="知识库 namespace（org/repo）")
    chunk_index: int = Field(default=0, description="chunk 在文档内的序号")
    title_path: str = Field(default="", description="chunk 所在标题层级路径，如 '二级标题/三级标题'")
    content: str = Field(description="chunk 文本内容（含图片转写结果）")
    images: list[ImageDescription] = Field(default_factory=list, description="本 chunk 内的图片描述")
    updated_at: str = Field(default="", description="文档更新时间")

    def to_es_doc(self, vector: list[float]) -> dict[str, Any]:
        """转换为 ES 写入文档（含 embedding 向量）。"""
        return {
            "content": self.content,
            "content_vector": vector,
            "metadata": {
                "source": "yuque",
                "doc_id": self.doc_id,
                "doc_title": self.doc_title,
                "url": self.url,
                "namespace": self.namespace,
                "chunk_index": self.chunk_index,
                "title_path": self.title_path,
                "images": [img.model_dump() for img in self.images],
                "updated_at": self.updated_at,
            },
        }


class IngestStats(BaseModel):
    """一次摄取任务的统计信息。"""

    repos_processed: int = 0
    docs_fetched: int = 0
    docs_processed: int = 0
    chunks_created: int = 0
    images_detected: int = 0
    images_transcribed: int = 0
    es_written: int = 0
    es_failed: int = 0
    errors: list[str] = Field(default_factory=list)

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)
