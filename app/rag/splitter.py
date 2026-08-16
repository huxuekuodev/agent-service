"""Markdown 语义分块器。

按标题层级切分文档，块内按字符上限二次切分（保留重叠），同时提取
文档内的图片引用（``![alt](url)``），供图片转文字阶段使用。

设计要点：
  - 以 ``# / ## / ###`` 标题作为语义边界，保持每个 chunk 内标题路径可追溯。
  - 标题行与其下属正文归属同一个 chunk；超过 ``max_chars`` 的段落做
    字符级切分，用 ``overlap`` 保留前后上下文。
  - 图片单独抽取为 ``ImageRef``（alt / url / 上下文标题路径），
    正文中保留占位标记，便于后续替换为转写文本。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.rag.models import DocChunk

__all__ = ["ImageRef", "split_markdown_document"]

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


@dataclass
class ImageRef:
    """文档内单个图片引用。"""

    alt: str
    url: str
    title_path: str
    index: int


@dataclass
class _Section:
    """按标题切分后的一个语义块。"""

    title_path: str
    lines: list[str]
    heading_level: int


def split_markdown_document(
    *,
    doc: object,
    content: str,
    max_chars: int = 2000,
    overlap: int = 200,
    image_placeholder: str = "![image]({url})",
) -> tuple[list[DocChunk], list[ImageRef]]:
    """将 Markdown 文档切分为语义块。

    Args:
        doc: 源文档对象（需含 doc_id / title / url / namespace / updated_at）。
        content: Markdown 原文。
        max_chars: 单个 chunk 最大字符数。
        overlap: 相邻 chunk 重叠字符数（仅对超长段落生效）。
        image_placeholder: 图片占位标记模板（``{url}`` 会被替换为图片 URL）。

    Returns:
        (chunks, image_refs)：chunks 为可入库的语义块；image_refs 为全文图片引用
        （含所在标题路径），供图片转文字阶段使用。
    """
    sections = _split_by_headings(content)
    image_refs: list[ImageRef] = []
    chunks: list[DocChunk] = []
    img_counter = 0

    for section in sections:
        text = _render_section(section)
        # 收集本 section 内的图片引用
        for m in _IMAGE_RE.finditer(text):
            ref = ImageRef(
                alt=m.group(1).strip(),
                url=m.group(2).strip(),
                title_path=section.title_path,
                index=img_counter,
            )
            image_refs.append(ref)
            img_counter += 1
        # 切分正文（图片位置保留占位）
        body = _IMAGE_RE.sub(image_placeholder.format(url="\\1"), text)
        for part_index, part in _split_text(body, max_chars, overlap):
            if not part.strip():
                continue
            chunks.append(
                DocChunk(
                    doc_id=_get(doc, "doc_id", ""),
                    doc_title=_get(doc, "title", ""),
                    url=_get(doc, "url", ""),
                    namespace=_get(doc, "namespace", ""),
                    chunk_index=len(chunks),
                    title_path=section.title_path,
                    content=part.strip(),
                    updated_at=_get(doc, "updated_at", ""),
                )
            )

    return chunks, image_refs


def _get(obj: object, key: str, default: str = "") -> str:
    """从对象（dataclass / dict / pydantic）取属性，兜底默认值。"""
    if isinstance(obj, dict):
        return str(obj.get(key) or default)
    return str(getattr(obj, key, None) or default)


def _split_by_headings(content: str) -> list[_Section]:
    """按标题层级切分为 section。"""
    sections: list[_Section] = []
    current: _Section | None = None
    title_stack: list[str] = []

    for raw_line in content.splitlines():
        heading = _HEADING_RE.match(raw_line)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            # 记录当前标题栈（同一层标题替换，更深层追加）
            while len(title_stack) >= level:
                title_stack.pop()
            title_stack.append(title)
            title_path = "/".join(title_stack)
            if current is not None:
                sections.append(current)
            current = _Section(title_path=title_path, lines=[raw_line], heading_level=level)
        else:
            if current is None:
                # 文档开头无标题的部分归到顶层
                current = _Section(title_path="", lines=[], heading_level=0)
            current.lines.append(raw_line)

    if current is not None:
        sections.append(current)
    return sections


def _render_section(section: _Section) -> str:
    return "\n".join(section.lines)


def _split_text(text: str, max_chars: int, overlap: int) -> list[str]:
    """按字符长度切分文本；单段超长时按行合并切，保留重叠。"""
    if max_chars <= 0:
        return [text]
    if len(text) <= max_chars:
        return [text]

    parts: list[str] = []
    # 优先按空行/行边界切，减少语义断裂
    lines = text.split("\n")
    current: list[str] = []
    current_len = 0
    for line in lines:
        line_len = len(line) + 1  # +1 换行
        if current and current_len + line_len > max_chars:
            parts.append("\n".join(current))
            current = _overlap_tail(current, overlap)
            current_len = sum(len(value) + 1 for value in current)
        current.append(line)
        current_len += line_len
    if current:
        parts.append("\n".join(current))

    # 若仍存在超长单行（如无换行的长文本），做硬切分
    final: list[str] = []
    for part in parts:
        if len(part) <= max_chars:
            final.append(part)
        else:
            final.extend(_hard_split(part, max_chars, overlap))
    return final


def _overlap_tail(lines: list[str], overlap: int) -> list[str]:
    """取当前块末尾若干行作为重叠（防止上下文断裂）。"""
    if overlap <= 0 or not lines:
        return []
    tail: list[str] = []
    tail_len = 0
    for line in reversed(lines):
        tail.insert(0, line)
        tail_len += len(line) + 1
        if tail_len >= overlap:
            break
    return tail


def _hard_split(text: str, max_chars: int, overlap: int) -> list[str]:
    """对超长单段做硬切分，切点尽量落在句子边界（。！？；）.。"""
    parts: list[str] = []
    i = 0
    n = len(text)
    sentence_boundary = re.compile(r"(?<=[。！？；.!?;])")
    while i < n:
        end = min(i + max_chars, n)
        if end < n:
            # 尽量回退到句子边界
            window = text[i + max_chars // 2 : end]
            m = list(sentence_boundary.finditer(window))
            if m:
                # 找最后一个边界点
                end = i + max_chars // 2 + m[-1].end()
            else:
                # 回退到最近空白
                last_space = text.rfind(" ", i + max_chars // 2, end)
                if last_space != -1:
                    end = last_space
        parts.append(text[i:end])
        i = max(end - overlap, i + 1)
        if i >= end:
            i = end
    return parts or [text]
