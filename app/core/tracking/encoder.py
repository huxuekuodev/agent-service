"""Protobuf wire-format 编码器（纯 Python，零新增依赖）。

按 docs/打点设计.md 定义的字段编号编码：

    message Content {          // 字段编号
        string type   = 1;
        string page   = 2;
        string source = 3;
        Ext    ext    = 4;     // 嵌套 message
    }
    message Ext {
        string p0 = 1;  string p1 = 2;  ...  string p14 = 15;
    }

编码规则（protobuf 标准 wire format）：
  - 所有字段均为 string / 嵌套 message → wire type 2（length-delimited）；
  - 标签 = (field_number << 3) | wire_type，按 varint 编码；
  - 空字符串省略（protobuf 默认值，解码端自动还原为 ""）；
  - Ext 为空时整体省略。
"""

from __future__ import annotations

from app.core.tracking.model import TrackingContent, TrackingExt

__all__ = ["encode_ext", "encode_tracking_content"]

_WIRE_TYPE_LENGTH_DELIMITED = 2
_EXT_SLOT_COUNT = 15

# Content 字段编号
_FIELD_TYPE = 1
_FIELD_PAGE = 2
_FIELD_SOURCE = 3
_FIELD_EXT = 4
_FIELD_MODEL = 5


def _encode_varint(value: int) -> bytes:
    """把无符号整数编码为 protobuf varint（小端 7-bit 分组）。"""
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _encode_tag(field_number: int) -> bytes:
    """编码字段标签（wire type 固定为 length-delimited）。"""
    return _encode_varint((field_number << 3) | _WIRE_TYPE_LENGTH_DELIMITED)


def _encode_bytes_field(field_number: int, data: bytes) -> bytes:
    """编码 length-delimited 字段：tag + len(varint) + payload。"""
    return _encode_tag(field_number) + _encode_varint(len(data)) + data


def _encode_string_field(field_number: int, value: str) -> bytes:
    """编码 string 字段（UTF-8）。"""
    return _encode_bytes_field(field_number, value.encode("utf-8"))


def encode_ext(ext: TrackingExt) -> bytes:
    """把点位信息编码为 Ext message bytes（空槽位省略）。"""
    out = bytearray()
    for i in range(_EXT_SLOT_COUNT):
        value = getattr(ext, f"p{i}")
        if value:
            out += _encode_string_field(i + 1, value)
    return bytes(out)


def encode_tracking_content(content: TrackingContent) -> bytes:
    """把打点内容编码为 Content message bytes。"""
    out = bytearray()
    if content.type:
        out += _encode_string_field(_FIELD_TYPE, content.type)
    if content.page:
        out += _encode_string_field(_FIELD_PAGE, content.page)
    if content.source:
        out += _encode_string_field(_FIELD_SOURCE, content.source)
    if content.model:
        out += _encode_string_field(_FIELD_MODEL, content.model)
    ext_bytes = encode_ext(content.ext)
    if ext_bytes:
        out += _encode_bytes_field(_FIELD_EXT, ext_bytes)
    return bytes(out)
