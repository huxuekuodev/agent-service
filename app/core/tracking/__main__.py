"""打点数据日志查看工具（解码 base64 行 → JSON）。

用法：
    uv run python -m app.core.tracking [数据日志文件]      # 逐行解码为 JSON
    uv run python -m app.core.tracking --payload <base64>  # 解码单条 base64
    cat 数据日志 | uv run python -m app.core.tracking      # 从 stdin 读取

示例：
    uv run python -m app.core.tracking logs/tracking.data
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import sys

from app.core.tracking.decoder import DecodeError, decode_tracking_content


def _content_to_dict(content) -> dict:
    return {
        "type": content.type,
        "page": content.page,
        "source": content.source,
        "ext": content.ext.to_dict(),
    }


def _decode_payload(b64: str) -> dict:
    """解码单条 base64(protobuf) 为 dict。"""
    payload = base64.b64decode(b64.strip(), validate=True)
    return _content_to_dict(decode_tracking_content(payload))


def _decode_line(line: str) -> dict:
    """解析一行数据日志：{ISO 时间} {base64(protobuf)}。"""
    line = line.strip()
    if not line:
        return {}
    parts = line.split(" ", 1)
    if len(parts) == 2:
        ts, b64 = parts
        return {"time": ts, **_decode_payload(b64)}
    return _decode_payload(parts[0])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="解码打点数据日志（每行：ISO 时间 + base64(protobuf)）为 JSON")
    parser.add_argument("file", nargs="?", help="数据日志文件；缺省从 stdin 读取")
    parser.add_argument("--payload", metavar="BASE64", help="直接解码单条 base64(protobuf)，不读取文件")
    args = parser.parse_args(argv)

    try:
        if args.payload is not None:
            records = [_decode_payload(args.payload)]
        elif args.file:
            with open(args.file, encoding="utf-8") as f:
                records = [_decode_line(line) for line in f]
        else:
            records = [_decode_line(line) for line in sys.stdin]
    except (ValueError, DecodeError, binascii.Error) as exc:
        print(f"解码失败: {exc}", file=sys.stderr)
        return 1

    for rec in records:
        if rec:
            print(json.dumps(rec, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
