"""图片分类 Agent 单元测试（mock 视觉 LLM）。"""

from __future__ import annotations

from typing import Any

import pytest

from app.rag.image_classifier import (
    _guess_image_mime,
    _parse_json_output,
    classify_image_description,
    describe_image_from_bytes,
)
from app.rag.models import ImageDescription


class _FakeLLM:
    """返回预设 JSON 的假 LLM（模拟 langchain ChatModel.ainvoke）。"""

    def __init__(self, payload: str) -> None:
        self._payload = payload

    async def ainvoke(self, messages: list[Any], **kwargs: Any) -> Any:
        class _Msg:
            content = self._payload

        return _Msg()


@pytest.mark.asyncio
async def test_classify_flowchart() -> None:
    llm = _FakeLLM('{"kind": "flowchart", "description": "包含开始/结束节点与箭头，展示请求流程", "confidence": 0.9}')
    img = ImageDescription(url="https://img.yuque.com/a.png", index=0)
    result = await classify_image_description(llm, img)  # type: ignore[arg-type]
    assert result.succeeded
    assert result.kind == "flowchart"
    assert "流程" in result.description
    assert result.confidence == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_classify_feature() -> None:
    llm = _FakeLLM('{"kind": "feature", "description": "展示设置页面的功能开关界面", "confidence": 0.85}')
    img = ImageDescription(url="https://img.yuque.com/b.png", index=1)
    result = await classify_image_description(llm, img)  # type: ignore[arg-type]
    assert result.kind == "feature"


@pytest.mark.asyncio
async def test_classify_invalid_kind_falls_back_to_other() -> None:
    llm = _FakeLLM('{"kind": "bogus", "description": "无法识别的图", "confidence": 0.1}')
    img = ImageDescription(url="https://img.yuque.com/c.png", index=2)
    result = await classify_image_description(llm, img)  # type: ignore[arg-type]
    assert result.kind == "other"


@pytest.mark.asyncio
async def test_classify_failure_sets_error() -> None:
    class _BrokenLLM:
        async def ainvoke(self, messages: list[Any], **kwargs: Any) -> Any:
            raise RuntimeError("network down")

    img = ImageDescription(url="https://img.yuque.com/d.png", index=3)
    result = await classify_image_description(_BrokenLLM(), img)  # type: ignore[arg-type]
    assert not result.succeeded
    assert "network down" in result.error


@pytest.mark.asyncio
async def test_describe_image_from_bytes_uses_vision() -> None:
    llm = _FakeLLM('{"kind": "feature", "description": "登录页截图，包含账号与密码输入框", "confidence": 0.7}')
    result = await describe_image_from_bytes(llm, b"fake-image-bytes", url="https://img.yuque.com/e.png", index=4)  # type: ignore[arg-type]
    assert result.succeeded
    assert result.kind == "feature"
    assert "登录" in result.description


def test_parse_json_output_tolerates_code_fence() -> None:
    raw = '```json\n{"kind": "flowchart", "description": "x", "confidence": 0.5}\n```'
    assert _parse_json_output(raw)["kind"] == "flowchart"


def test_guess_image_mime() -> None:
    assert _guess_image_mime(b"\x89PNG\r\n\x1a\n...") == "image/png"
    assert _guess_image_mime(b"\xff\xd8\xff\xe0...") == "image/jpeg"
    assert _guess_image_mime(b"GIF89a...") == "image/gif"
    assert _guess_image_mime(b"RIFFxxxxWEBP...") == "image/webp"
    assert _guess_image_mime(b"unknown") == "application/octet-stream"
