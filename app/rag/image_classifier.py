"""图片分类 Agent：判断文档图片是「流程图」还是「功能介绍图」。

图片通过多模态（vision）模型转成文字描述后，由本 Agent 结合上下文判定
图片类型，输出结构化 JSON：

    flowchart   — 流程图/架构图/时序图/拓扑图（表达流程、依赖、关系）
    feature     — 功能介绍图/产品截图/界面示意图（表达功能、形态、效果）
    other       — 其他（照片、图标、装饰图等）

设计要点：
  - 分类与转写合并在一个 LLM 调用里完成（视觉模型 + 结构化输出），避免两次推理。
  - 纯函数式，不依赖 LangGraph；可直接在摄取流水线中调用。
"""

from __future__ import annotations

import base64
import json
from typing import Any, cast

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage

from app.core.log import logger
from app.rag.models import ImageDescription, ImageKind

__all__ = ["describe_image_from_bytes", "classify_image_description"]

# 分类 prompt：期望 LLM 输出严格 JSON，包含 kind / description / confidence
_CLASSIFY_SYSTEM_PROMPT = """\
你是文档图片分析专家。给定一张文档图片，判断它的类型并转写其内容。

图片类型（kind）只取三种之一：
- flowchart：流程图 / 架构图 / 时序图 / 状态图 / 拓扑图 / 数据流图。特征是包含方框、箭头、
  连线、节点，表达流程步骤、模块关系、数据走向、依赖顺序。
- feature：功能介绍图 / 产品截图 / 界面示意图 / 效果展示图。特征是展示某个功能、页面、
  产品或操作界面，表达"长什么样、有什么功能、如何操作"。
- other：不属于以上两类，如照片、插图、装饰图、表情图、图表外的杂图。

请同时转写图片内容（description），用 1-3 句中文概括图中的关键信息。
如果图片是流程图，描述要覆盖节点与流程走向；如果是功能介绍图，描述要覆盖展示的功能与界面要素。

严格输出 JSON（不要 markdown 代码块、不要额外文字）：
{{"kind": "flowchart|feature|other", "description": "图片内容转写", "confidence": 0.0-1.0}}
"""


async def classify_image_description(llm: BaseChatModel, image: ImageDescription) -> ImageDescription:
    """用视觉 LLM 对单张图片分类 + 转写，返回更新后的 ImageDescription。

    失败时把错误写入 ``image.error``，不抛异常（调用方继续处理其余图片）。
    """
    if not image.description and image.url:
        # 未转写时直接提示模型看图；已有转写则基于文本分类
        prompt = _build_vision_prompt(image.url)
    else:
        prompt = _build_text_prompt(image)
    try:
        result = await llm.ainvoke([HumanMessage(content=prompt)])
        content = getattr(result, "content", "")
        data = _parse_json_output(str(content) if content else "")
        kind = str(data.get("kind", "other")).strip().lower()
        if kind not in ("flowchart", "feature", "other"):
            kind = "other"
        image.kind = cast(ImageKind, kind)
        image.description = str(data.get("description") or image.description)
        try:
            image.confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
        except (TypeError, ValueError):
            image.confidence = 0.0
    except Exception as exc:
        logger.warning("图片分类失败 {}: {}", image.url, exc)
        image.error = str(exc)
    return image


def _build_vision_prompt(image_url: str) -> str:
    """构造多模态 prompt（图片 URL + 分类指令）。"""
    return f"{_CLASSIFY_SYSTEM_PROMPT}\n\n请分析这张图片：{image_url}\n如果无法直接查看图片，请基于 URL 后缀（如流程图、架构图等文件名）尽量判断，否则输出 other 并说明无法识别。"


def _build_text_prompt(image: ImageDescription) -> str:
    """基于已有转写文本进行分类（无视觉能力时的文本兜底）。"""
    return f"{_CLASSIFY_SYSTEM_PROMPT}\n\n图片内容转写：{image.description}\n图片 URL：{image.url}\n基于上述转写判断类型并输出 JSON。"


def _parse_json_output(raw: str) -> dict[str, Any]:
    """解析 LLM 输出的严格 JSON（容忍 markdown 代码块包裹）。"""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    data = json.loads(text)
    return data if isinstance(data, dict) else {}


async def describe_image_from_bytes(llm: BaseChatModel, image_bytes: bytes, *, url: str = "", index: int = 0) -> ImageDescription:
    """用视觉 LLM 直接分析图片字节，返回 ImageDescription。

    用于图片字节已下载的场景（离线 / 私域图片）；由摄取流水线调用。

    Args:
        llm: 支持视觉的 chat model（langchain）。
        image_bytes: 图片原始字节。
        url: 图片来源 URL（用于占位与日志）。
        index: 文档内图片序号。

    Returns:
        ImageDescription（kind / description / confidence）。失败时 error 字段非空。
    """
    image = ImageDescription(url=url, index=index)
    try:
        data_url = f"data:{_guess_image_mime(image_bytes)};base64,{base64.b64encode(image_bytes).decode()}"
        result = await llm.ainvoke(
            [
                HumanMessage(
                    content=[
                        {"type": "text", "text": _CLASSIFY_SYSTEM_PROMPT},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ]
                )
            ]
        )
        content = getattr(result, "content", "")
        data = _parse_json_output(str(content) if content else "")
        kind = str(data.get("kind", "other")).strip().lower()
        if kind in ("flowchart", "feature", "other"):
            image.kind = cast(ImageKind, kind)
        image.description = str(data.get("description") or "")
        try:
            image.confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
        except (TypeError, ValueError):
            image.confidence = 0.0
    except Exception as exc:
        logger.warning("图片字节分析失败 {}: {}", url, exc)
        image.error = str(exc)
    return image


def _guess_image_mime(data: bytes) -> str:
    """根据图片魔数推断 MIME 类型（PNG / JPEG / GIF / WEBP，兜底 octet-stream）。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"
