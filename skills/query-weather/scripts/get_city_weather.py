"""
查询和风天气(QWeather) 城市逐日天气预报（v7 daily forecast API）

文档: https://dev.qweather.com/docs/api/weather/weather-daily-forecast-webapi-v7/

用法:
    python get_city_weather.py <city_code> [api_key] [--json]
    city_code: QWeather LocationID（如 101090101 石家庄，见 ../data/China-City-List-latest.csv 的 Location_ID 列）
    api_key  : 和风天气 API Key；也可通过环境变量 QWEATHER_API_KEY 提供

设计说明:
    - 仅用标准库(urllib/gzip/json)，不依赖 requests，保证在 E2B 沙箱/任何环境可直接运行
    - API host 优先取 consts.HF_PATH（账号专属 host），未设置时回退 devapi.qweather.com
    - QWeather 默认返回 gzip 内容，urllib 不会自动解压，这里显式处理
"""

from __future__ import annotations

import gzip
import importlib
import json
import os
import re
import sys
import urllib.error
import urllib.request

#: QWeather LocationID 为纯数字编码（见 China-City-List-latest.csv 的 Location_ID 列）
_CITY_CODE_RE = re.compile(r"^\d{6,12}$")


def _resolve_host() -> str:
    """API host 优先级: 环境变量 QWEATHER_HOST > 同目录 consts.py 的 HF_PATH > 默认值。

    consts 用动态导入：脚本以 python xxx.py 运行时 sys.path 含脚本目录，能拿到同目录的 consts.py。
    """
    env_host = os.environ.get("QWEATHER_HOST", "").strip()
    if env_host:
        return env_host
    try:
        consts = importlib.import_module("consts")
        host = getattr(consts, "HF_PATH", "").strip()
        if host:
            return host
    except Exception:
        pass
    return DEFAULT_HOST


DEFAULT_HOST = "devapi.qweather.com"
DAILY_TYPES = ("3d", "7d", "10d", "15d")  # 逐日预报支持的天数

# QWeather 业务错误码 -> 提示（HTTP 层错误在 except 中处理）
CODE_MESSAGES = {
    "400": "请求参数错误",
    "401": "API Key 无效或未授权",
    "402": "API Key 超出访问限制或余额不足",
    "403": "API Key 无访问权限",
    "404": "请求的接口/位置不存在（请确认城市编码是 QWeather LocationID）",
    "429": "请求过于频繁",
}


def _http_get_json(url: str, timeout: float = 15.0) -> dict:
    """发 GET 请求并解 gzip，返回解析后的 JSON。"""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "qweather-skill/1.0",
            "Accept-Encoding": "gzip",  # 明确要求 gzip，收到后手动解压
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                raw = gzip.decompress(raw)
            elif raw[:2] == b"\x1f\x8b":  # 个别网关不回 Content-Encoding，按魔数判断
                raw = gzip.decompress(raw)
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 部分错误信息在响应体里也是 gzip JSON
        raw = e.read()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        detail = raw.decode("utf-8", "replace")[:200]
        hint = ""
        if e.code in (401, 403):
            hint = "（提示：和风 Key 与访问域名需在控制台绑定一致。本地正常但沙箱内 401/403 时，请确认沙箱内 QWEATHER_HOST / consts.HF_PATH 与 Key 绑定的专属域名一致）"
        raise RuntimeError(f"HTTP {e.code}: {detail}{hint}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"网络请求失败: {e.reason}") from e


def fetch_daily_weather(city_code: str, api_key: str | None = None, days: str = "7d", host: str = "") -> dict:
    """查询某城市逐日预报，返回 QWeather 原始 JSON（调用方按需取 daily[]）。

    - city_code: QWeather LocationID，例如 "101090101"
    - api_key  : 未传则读环境变量 QWEATHER_API_KEY
    - days     : 3d/7d/10d/15d
    """
    if not api_key:
        api_key = os.environ.get("QWEATHER_API_KEY", "")
    if not api_key:
        raise RuntimeError("缺少和风天气 API Key：请设置环境变量 QWEATHER_API_KEY 或传入 api_key（不要在代码/脚本里硬编码）")
    if days not in DAILY_TYPES:
        raise ValueError(f"days 仅支持 {DAILY_TYPES}")
    if not _CITY_CODE_RE.fullmatch(city_code.strip()):
        raise ValueError(f"city_code 必须是纯数字的 QWeather LocationID（来自 get_city_code 输出的 city_code 字段，如 101090101），收到: {city_code!r}；不要传城市名")

    host = host or _resolve_host()
    url = f"https://{host}/v7/weather/{days}?location={city_code}&key={api_key}"
    data = _http_get_json(url)

    code = str(data.get("code", ""))
    if code != "200":
        hint = CODE_MESSAGES.get(code, "未知错误")
        raise RuntimeError(f"QWeather 返回错误 code={code}：{hint}")

    return data


def format_daily(data: dict) -> str:
    """把 QWeather 响应格式化成易读的多行文本（供 LLM / 控制台使用）。"""
    daily = data.get("daily", [])
    if not daily:
        return "（预报数据为空）"
    lines = [f"更新于 {data.get('updateTime', '?')}"]
    for d in daily:
        lines.append(f"{d['fxDate']} 白天:{d.get('textDay', '?')} 夜间:{d.get('textNight', '?')} {d.get('tempMin', '?')}~{d.get('tempMax', '?')}℃ {d.get('windDirDay', '?')} {d.get('windScaleDay', '?')}级")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2
    city_code = argv[0].strip()
    api_key = argv[1] if len(argv) > 1 and not argv[1].startswith("--") else None
    as_json = "--json" in argv

    if not _CITY_CODE_RE.fullmatch(city_code):
        print(
            f"城市编码格式错误：city_code 必须是纯数字 QWeather LocationID（来自 get_city_code 输出的 cities[].city_code 字段，如 101090101），收到: {city_code!r}。请先用 step-01 取码，不要传城市名,并且每次只能传递一个城市编码。",
            file=sys.stderr,
        )
        return 2

    try:
        data = fetch_daily_weather(city_code, api_key="de958b4e906a4fafa422e107aa552df6")
        if as_json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(format_daily(data))
        return 0
    except Exception as e:
        print(f"查询失败: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
