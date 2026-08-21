"""打点工具包（app/core/tracking）。

负责组装打点事件并按 protobuf wire format 输出：

    constants.py     TrackingType / TrackingPage / TrackingSource 常量
    model.py         TrackingContent（Content）与 TrackingExt（Ext P0-P14）
    encoder.py       protobuf 编码器（零依赖，纯 Python）
    decoder.py       protobuf 解码器（还原为 TrackingContent，查看/校验用）
    tracking.proto   protoc 可用的规范 schema（供 protobuf 工具解码）
    tracker.py       打点工具 Tracker（组装 + 编码 + 数据日志/自定义 sink 输出）
    __main__.py      命令行查看工具：python -m app.core.tracking <数据日志>

用法：

    from app.core.tracking import TrackingPage, TrackingType, track

    await track(TrackingType.TOOL_CALL, TrackingPage.EXECUTE, p0="web_search", p3="120")

    # 查看数据日志
    uv run python -m app.core.tracking logs/tracking.data

字段编号与槽位含义见 docs/打点设计.md。
"""

from app.core.tracking.constants import TrackingPage, TrackingSource, TrackingType
from app.core.tracking.decoder import DecodeError, decode_ext, decode_tracking_content
from app.core.tracking.encoder import encode_ext, encode_tracking_content
from app.core.tracking.model import TrackingContent, TrackingExt
from app.core.tracking.tracker import Tracker, track

__all__ = [
    "TrackingType",
    "TrackingPage",
    "TrackingSource",
    "TrackingExt",
    "TrackingContent",
    "encode_ext",
    "encode_tracking_content",
    "decode_ext",
    "decode_tracking_content",
    "DecodeError",
    "Tracker",
    "track",
]
