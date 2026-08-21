"""Protobuf wire-format 解码器（纯 Python，与 encoder.py 互逆）。

用于查看/校验打点数据：把 protobuf bytes 还原为 TrackingContent。
兼容未知字段（按 wire type 跳过），支持 UTF-8 中文内容。
"""

from __future__ import annotations

from app.core.tracking.model import TrackingContent, TrackingExt

__all__ = ["DecodeError", "decode_ext", "decode_tracking_content"]

_WIRE_TYPE_VARINT = 0
_WIRE_TYPE_FIXED64 = 1
_WIRE_TYPE_LENGTH_DELIMITED = 2
_WIRE_TYPE_FIXED32 = 5


class DecodeError(ValueError):
    """protobuf 解码错误。"""


def _decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    """解码 varint，返回 (值, 新位置)。"""
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise DecodeError("truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, pos
        shift += 7
        if shift >= 64:
            raise DecodeError("varint too long")


def _decode_length_delimited(data: bytes, pos: int) -> tuple[bytes, int]:
    """解码 length-delimited 字段，返回 (payload, 新位置)。"""
    length, pos = _decode_varint(data, pos)
    end = pos + length
    if end > len(data):
        raise DecodeError("truncated length-delimited field")
    return data[pos:end], end


def _skip_field(data: bytes, pos: int, wire_type: int) -> int:
    """跳过未知字段（按 wire type），返回新位置。"""
    if wire_type == _WIRE_TYPE_VARINT:
        return _decode_varint(data, pos)[1]
    if wire_type == _WIRE_TYPE_FIXED64:
        if pos + 8 > len(data):
            raise DecodeError("truncated fixed64")
        return pos + 8
    if wire_type == _WIRE_TYPE_LENGTH_DELIMITED:
        return _decode_length_delimited(data, pos)[1]
    if wire_type == _WIRE_TYPE_FIXED32:
        if pos + 4 > len(data):
            raise DecodeError("truncated fixed32")
        return pos + 4
    raise DecodeError(f"unsupported wire type: {wire_type}")


def decode_ext(data: bytes) -> TrackingExt:
    """把 Ext message bytes 还原为 TrackingExt。"""
    ext = TrackingExt()
    pos = 0
    while pos < len(data):
        tag, pos = _decode_varint(data, pos)
        field_number = tag >> 3
        wire_type = tag & 0x7
        if wire_type != _WIRE_TYPE_LENGTH_DELIMITED:
            pos = _skip_field(data, pos, wire_type)
            continue
        payload, pos = _decode_length_delimited(data, pos)
        if 1 <= field_number <= 15:
            setattr(ext, f"p{field_number - 1}", payload.decode("utf-8", errors="replace"))
    return ext


def decode_tracking_content(data: bytes) -> TrackingContent:
    """把 Content message bytes 还原为 TrackingContent。"""
    content = TrackingContent()
    pos = 0
    while pos < len(data):
        tag, pos = _decode_varint(data, pos)
        field_number = tag >> 3
        wire_type = tag & 0x7
        if wire_type != _WIRE_TYPE_LENGTH_DELIMITED:
            pos = _skip_field(data, pos, wire_type)
            continue
        payload, pos = _decode_length_delimited(data, pos)
        if field_number == 1:
            content.type = payload.decode("utf-8", errors="replace")
        elif field_number == 2:
            content.page = payload.decode("utf-8", errors="replace")
        elif field_number == 3:
            content.source = payload.decode("utf-8", errors="replace")
        elif field_number == 4:
            content.ext = decode_ext(payload)
        elif field_number == 5:
            content.model = payload.decode("utf-8", errors="replace")
        # 其它字段号：未知字段，跳过（前向兼容）
    return content
