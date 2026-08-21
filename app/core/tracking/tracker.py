"""打点工具：组装事件 → protobuf 编码 → 独立数据日志 / 自定义 sink。

打点输出与执行日志（logs/app.log）**分离**：默认写入独立的**数据日志文件**
（config.yaml ``tracking.output``，默认 logs/tracking.data），每行一条打点：

    {ISO 时间} {base64(protobuf)}

行内不带可读文本，保持纯数据，便于后续接入 Kafka / 数仓等数据链路；
需要自定义输出时传入 sink（同步回调或协程，例如直接投递到消息队列）。

用法：

    from app.core.tracking import TrackingPage, TrackingType, Tracker

    tracker = Tracker()  # 默认 sink：写入数据日志文件
    await tracker.track(TrackingType.TOOL_CALL, TrackingPage.EXECUTE, p0="web_search", p3="120")
"""

from __future__ import annotations

import asyncio
import base64
import inspect
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import TextIO

from app.core.tracking.constants import TrackingSource
from app.core.tracking.encoder import encode_tracking_content
from app.core.tracking.model import TrackingContent, TrackingExt

__all__ = ["Tracker", "track"]

#: sink 签名：接收编码后的 protobuf bytes，可同步返回或返回协程
Sink = Callable[[bytes], "None | Awaitable[None]"]

_DEFAULT_OUTPUT = "logs/tracking.data"


class Tracker:
    """打点客户端。

    Args:
        source: 默认来源（TrackingSource）；track 时可用 source 参数覆盖。
        sink: 输出回调，接收 protobuf bytes（同步或协程）；缺省写入数据日志文件
            （config.yaml tracking.output，默认 logs/tracking.data）。
        output: 显式指定数据日志文件路径，覆盖 config 配置。
    """

    def __init__(
        self,
        *,
        source: str = TrackingSource.SERVER,
        sink: Sink | None = None,
        output: str | None = None,
    ) -> None:
        self._source = source
        self._sink = sink
        self._output = output
        self._file: TextIO | None = None

    async def track(
        self,
        type_: str,
        page: str,
        *,
        source: str | None = None,
        model: str = "",
        ext: TrackingExt | None = None,
        **ext_fields: str,
    ) -> bytes:
        """组装并输出一条打点，返回编码后的 protobuf bytes。

        Args:
            type_: 打点类型（TrackingType）。
            page: 业务名称（TrackingPage）。
            source: 来源；缺省用构造时的默认来源。
            model: 模型角色名（config.yaml models 的 key），监控平台按它分组/对比。
            ext: 点位信息；不传时用 ext_fields 构建（键必须为 p0..p14）。

        Examples:
            await tracker.track(TrackingType.TOOL_CALL, TrackingPage.EXECUTE, model="plan_node_model", p0="web_search", p3="120")
        """
        content = TrackingContent(
            type=type_,
            page=page,
            source=source or self._source,
            model=model,
            ext=ext if ext is not None else TrackingExt(**ext_fields),
        )
        payload = encode_tracking_content(content)

        if self._sink is not None:
            result = self._sink(payload)
            if inspect.isawaitable(result):
                await result
        else:
            await self._write_data_line(payload)
        return payload

    async def aclose(self) -> None:
        """关闭数据日志文件（如有）。"""
        if self._file is not None:
            await asyncio.to_thread(self._file.close)
            self._file = None

    # ------------------------------------------------------------------
    # 默认输出：独立数据日志（不经过 loguru / app.log）
    # ------------------------------------------------------------------

    async def _write_data_line(self, payload: bytes) -> None:
        """追加一行数据日志：ISO 时间 + base64(protobuf)。"""
        line = f"{datetime.now().astimezone().isoformat(timespec='milliseconds')} {base64.b64encode(payload).decode('ascii')}\n"
        await asyncio.to_thread(self._append_line, line)

    def _append_line(self, line: str) -> None:
        """同步追加（在事件循环外执行）；懒打开文件并即时落盘。"""
        if self._file is None:
            path = self._output or self._resolve_output()
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._file = open(path, "a", encoding="utf-8")
        self._file.write(line)
        self._file.flush()

    @staticmethod
    def _resolve_output() -> str:
        """从 config.yaml 读 tracking.output；失败时回退默认路径。"""
        try:
            from app.config import get_app_config

            return get_app_config().tracking.output or _DEFAULT_OUTPUT
        except Exception:
            return _DEFAULT_OUTPUT


_default_tracker = Tracker()


async def track(
    type_: str,
    page: str,
    *,
    source: str | None = None,
    model: str = "",
    ext: TrackingExt | None = None,
    **ext_fields: str,
) -> bytes:
    """便捷入口：使用全局默认 Tracker 打一条点（默认写入数据日志文件）。"""
    return await _default_tracker.track(type_, page, source=source, model=model, ext=ext, **ext_fields)
