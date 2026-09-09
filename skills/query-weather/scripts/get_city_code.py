"""
根据省份 / 城市名查询 QWeather LocationID（天气编码）。

数据: ../data/China-City-List-latest.csv（QWeather 中国城市列表）
列说明:
  - Location_ID        QWeather LocationID（天气接口只认此列，如 101090101）
  - Location_Name_ZH   城市/区/县中文名（如 石家庄 / 正定）
  - Adm1_Name_ZH       省级（省/自治区/直辖市，如 河北省、北京市）
  - Adm2_Name_ZH       隶属的地级市（如 石家庄市；直辖市下等于市名）

用法（stdout 一律输出 JSON，工具调用链路中 stdout 即给模型的 tool 结果）:
    python get_city_code.py --province 河北省   # 省份下的一级（地级）城市编码
    python get_city_code.py --city 正定         # 直接查具体城市/县编码（可能有多条跨省候选）
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

# 以"脚本自身位置"为基准推导 data 目录，不依赖运行时 CWD（沙箱内同步后结构不变）
CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "China-City-List-latest.csv"

#: 省级行政区名后缀（按长度降序匹配，避免「自治区」被误当「区」）
_PROVINCE_SUFFIXES = ("特别行政区", "自治区", "自治州", "地区", "省", "市", "盟")
#: 用户输入城市名时可能携带的后缀（区/县用于查询归一化，不用于城市识别）
_CITY_SUFFIXES = ("市", "县", "区", "旗", "盟", "地区")


def _load_rows() -> list[dict]:
    """读取城市列表。

    该 CSV 第 1 行可能是版本标题行（如 "China-City-List v2025..."），真正表头在第 2 行；
    也可能首行就是表头。DictReader 把首行当列名，因此：首行是版本行则丢弃它再交给 DictReader；
    首行已是表头则回退到文件头。
    """
    with open(CSV_PATH, encoding="utf-8") as f:
        first_line = f.readline()
        if first_line.startswith("Location_ID"):
            f.seek(0)
        reader = csv.DictReader(f)
        return [dict(row) for row in reader]


def _strip_suffix(name: str, suffixes: tuple[str, ...]) -> str:
    name = name.strip()
    for suffix in suffixes:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _province_key(name: str) -> str:
    """省名归一化：河北省->河北，北京市->北京，广西壮族自治区->广西壮族。"""
    return _strip_suffix(name, _PROVINCE_SUFFIXES)


def _short(row: dict) -> dict:
    """把一行压缩成输出用的精简 dict。"""
    return {
        "city": row["Location_Name_ZH"],
        "city_code": row["Location_ID"],
        "province": row["Adm1_Name_ZH"],
        "adm2": row["Adm2_Name_ZH"],
    }


def get_cities_by_province(province: str) -> list[dict]:
    """按省名取该省**一级（地级）城市**的编码列表。

    识别规则：
      1. 行属于该省：Adm1_Name_ZH 归一化后与输入相等，或以其为前缀
         （兼容「河北」/「河北省」、「广西」/「广西壮族自治区」）。
      2. 该行是地级市本级条目：Adm2_Name_ZH 以 Location_Name_ZH 为前缀
         （石家庄市->石家庄、大兴安岭地区->大兴安岭、北京市->北京、
         雄安新区->雄安新区），区/县行（Adm2=石家庄市 而 Location=正定）不命中。
      3. 每个 Adm2（地级市）只保留一个本级条目。
    """
    key = _province_key(province)
    if not key:
        return []

    by_adm2: dict[str, dict] = {}
    for row in _load_rows():
        adm1 = row["Adm1_Name_ZH"]
        adm1_key = _province_key(adm1)
        if adm1_key != key and not (len(key) >= 2 and adm1_key.startswith(key)):
            continue
        adm2 = row["Adm2_Name_ZH"]
        location = row["Location_Name_ZH"]
        if adm2 in by_adm2:
            continue  # 保留首个命中
        if adm2.startswith(location) and location:
            by_adm2[adm2] = row
    return [_short(row) for row in sorted(by_adm2.values(), key=lambda r: r["Adm2_Name_ZH"])]


def find_city_codes(city: str) -> list[dict]:
    """直接按城市/县名查编码（可能有跨省同名候选，交由调用方/模型判定）。

    匹配顺序: 全名精确 / 去「市 县 区」后缀后匹配 / 名称互为包含；
    命中结果附带 province + adm2，便于同名候选消歧。
    """
    rows = _load_rows()
    query = city.strip()
    normalized = _strip_suffix(query, _CITY_SUFFIXES)
    hits: list[dict] = []
    for row in rows:
        name = row["Location_Name_ZH"]
        if name == query or name == normalized or query in name or name in query:
            hits.append(_short(row))
    return hits


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]

    if not argv:
        print(__doc__)
        return 2

    if argv[0] == "--province" and len(argv) >= 2:
        province = argv[1]
        cities = get_cities_by_province(province)
        out = {
            "query_type": "province",
            "province": province,
            "count": len(cities),
            "cities": cities,
        }
        if not cities:
            out["hint"] = "未找到该省的一级城市，请检查省名（如 河北省 / 广东）"
    elif argv[0] == "--city" and len(argv) >= 2:
        city = argv[1]
        cities = find_city_codes(city)
        out = {
            "query_type": "city",
            "query": city,
            "count": len(cities),
            "cities": cities,
        }
        if not cities:
            out["hint"] = f"未找到「{city}」，请尝试输入完整名称（如 石家庄 / 正定）或改用省份查询"
        elif len(cities) > 1:
            out["hint"] = "存在同名候选（跨省/区县），请用户确认具体地区"
    else:
        print(__doc__)
        return 2

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
