"""语义分块器单元测试。"""

from __future__ import annotations

from app.rag.splitter import _split_by_headings, split_markdown_document


def _doc(**overrides: object) -> dict:
    base: dict = {
        "doc_id": "org/repo/doc1",
        "title": "测试文档",
        "url": "https://www.yuque.com/org/repo/doc1",
        "namespace": "org/repo",
        "updated_at": "2026-01-01T00:00:00+08:00",
    }
    base.update(overrides)
    return base


def test_split_by_headings_keeps_title_path() -> None:
    content = "# 一级\n正文A\n## 二级\n正文B\n### 三级\n正文C\n"
    sections = _split_by_headings(content)
    paths = [s.title_path for s in sections]
    assert paths == ["一级", "一级/二级", "一级/二级/三级"]


def test_split_markdown_document_basic() -> None:
    content = "# 标题\n这是正文第一段。\n\n## 子标题\n这是子标题下的正文。\n"
    chunks, image_refs = split_markdown_document(doc=_doc(), content=content, max_chars=1000, overlap=50)
    assert len(chunks) == 2
    assert chunks[0].title_path == "标题"
    assert chunks[1].title_path == "标题/子标题"
    assert chunks[0].doc_id == "org/repo/doc1"
    assert image_refs == []


def test_split_markdown_document_long_content_creates_multiple_chunks() -> None:
    content = "# 标题\n" + "这是很长的一段正文内容。" * 200 + "\n"
    chunks, _ = split_markdown_document(doc=_doc(), content=content, max_chars=200, overlap=30)
    assert len(chunks) >= 2
    assert all(len(c.content) <= 250 for c in chunks)  # max_chars + 少量重叠


def test_split_markdown_extracts_images_and_keeps_placeholder() -> None:
    content = "# 标题\n正文\n![流程图](https://img.yuque.com/a.png)\n\n## 子标题\n![功能介绍图](https://img.yuque.com/b.png)\n"
    chunks, image_refs = split_markdown_document(doc=_doc(), content=content, max_chars=1000, overlap=50)
    assert len(image_refs) == 2
    assert image_refs[0].url == "https://img.yuque.com/a.png"
    assert image_refs[0].title_path == "标题"
    assert image_refs[1].url == "https://img.yuque.com/b.png"
    assert image_refs[1].title_path == "标题/子标题"
    # 正文中图片替换为占位
    assert "![image](https://img.yuque.com/a.png)" in chunks[0].content
    assert "![image](https://img.yuque.com/b.png)" in chunks[1].content


def test_split_without_heading_uses_empty_title_path() -> None:
    content = "无标题文档正文。\n第二行。\n"
    chunks, _ = split_markdown_document(doc=_doc(), content=content, max_chars=1000, overlap=50)
    assert len(chunks) == 1
    assert chunks[0].title_path == ""
