---
name: query-weather
description: 查询中国城市天气（和风天气 QWeather），输入城市或省份返回天气信息
when_to_use: 用户询问天气、预报、气温、降雨、风力等天气信息时使用
requires_tools: []
sop: SOP.md
errors: errors.yaml
cleanup: cleanup.yaml
---

# 天气查询 Skill（QWeather）

内置两个脚本（均在 E2B 沙箱内运行，不落地本地）：
- `scripts/get_city_code.py`：城市编码查询（省份 → 地级城市列表；具体城市 → Location_ID）
- `scripts/get_city_weather.py`：按城市编码查询逐日天气

数据源：`data/China-City-List-latest.csv`（QWeather 中国城市列表）。
和风天气 API Key 通过环境变量 `QWEATHER_API_KEY` 提供（沙箱注入白名单 `env_keys: [QWEATHER_API_KEY]`）。

## 执行方式（框架）

SOP 步骤（见 `SOP.md`）：
- `step-01` → `run_skill_step("query-weather", "step-01", arguments=[...])`：城市编码
- `step-02` → `run_skill_step("query-weather", "step-02", arguments=["<city_code>"])`：逐日天气，每次只能传递一个城市编码

`arguments` 是传给脚本的命令行参数（对应下文各脚本的 CLI 约定）；下面的 CLI 文档是脚本的底层契约。

## 执行步骤

1. 当用户查询天气时，先判断输入是「具体城市/县」还是「省份」：
   - 都不是（如只给了模糊信息）→ 先向用户澄清要查的城市或省份。
2. 如果是省份：`step-01` 用 `--province <省名>` 取该省一级城市列表，让用户确认具体城市
   （或直接进入下一步的城市编码查询）。
3. 如果是具体城市：`step-01` 用 `--city <城市/县名>` 直接拿 Location_ID。
4. **记录每个城市的 `city_code`（cities[].city_code，纯数字，如 101090101）**；`step-02` 用该编码查逐日天气,每次只能传递一个城市编码。
5. 把天气信息整理后返回给用户。

> 城市数据来源：`get_city_code.py` 读取 `data/China-City-List-latest.csv`
> （`Location_Name_ZH`=城市名、`Location_ID`=城市编码）。结果必须保留编码，禁止只回城市名。

---

## get_city_code

获取城市编码（省份 → 一级城市列表；具体城市 → Location_ID）。

### 调用方式

脚本以命令行参数运行（stdout 输出 JSON）：

```bash
python3 get_city_code.py --province 河北省   # 省份下的一级城市编码
python3 get_city_code.py --city 正定         # 具体城市/县的 Location_ID
```

参数：

- `--province`（string，与 `--city` 二选一）：省份名，如 `河北省`（兼容 `河北`、`广西` 等简称）。
- `--city`（string，与 `--province` 二选一）：城市/县名，如 `石家庄`、`正定`；同名候选会全部返回。

### 输出内容

stdout 输出一个 JSON 对象（退出码 0 = 成功）：

```json
{
  "query_type": "province",
  "province": "河北省",
  "count": 12,
  "cities": [
    { "city": "石家庄", "city_code": "101090101", "province": "河北省", "adm2": "石家庄市" }
  ],
  "hint": "可选：无命中或多候选时的提示"
}
```

字段说明：

- `query_type`（string）：`province` 或 `city`，标识本次查询类型。
- `province`（string）：`--province` 的入参（city 模式无此字段）。
- `query`（string）：`--city` 的入参（province 模式无此字段）。
- `count`（integer）：命中城市数。
- `cities`（array）：城市列表，元素为 City 对象：
  - `city`（string）：城市/县名（QWeather 短名，如 `石家庄`、`正定`）。
  - `city_code`（string）：QWeather LocationID（如 `101090101`），传给 get_city_weather 用。
  - `province`（string）：所属省/直辖市（如 `河北省`）。
  - `adm2`（string）：隶属地级市（如 `石家庄市`）。
- `hint`（string）：仅当 count=0（未找到）或 count>1（同名候选）时出现，提示下一步动作。

城市查询示例（`--city 正定`）：

```json
{
  "query_type": "city",
  "query": "正定",
  "count": 1,
  "cities": [
    { "city": "正定", "city_code": "101090103", "province": "河北省", "adm2": "石家庄市" }
  ]
}
```

同名候选（count>1）时须向用户确认具体城市，再用确认城市的 `city_code` 继续。

---

## get_city_weather

按城市编码查询逐日天气预报（QWeather weather-daily v7）。

### 调用方式

```bash
python3 get_city_weather.py <city_code> [api_key] [--json]
```

参数：

- `city_code`（string，必填）：QWeather **纯数字 LocationID**（如 `101090101`，来自 get_city_code 输出的 `cities[].city_code`），禁止传城市名/中文。
- `api_key`（string，可选）：和风 API Key；缺省读环境变量 `QWEATHER_API_KEY`（沙箱模式推荐用环境变量，禁止硬编码进脚本）。
- `--json`（flag，可选）：输出 QWeather 原始 JSON；缺省输出易读文本。

### 输出内容

默认（不带 `--json`）—— stdout 为易读文本，每行一天预报，如：

```
2026-08-21 白天:晴 夜间:多云 25~33℃ 东南风 3级
```

带 `--json` —— stdout 为 QWeather 原始 JSON 对象，核心结构：

```json
{
  "code": "200",
  "updateTime": "2026-08-21T10:00+08:00",
  "daily": [
    {
      "fxDate": "2026-08-21",
      "textDay": "晴",
      "textNight": "多云",
      "tempMin": "25",
      "tempMax": "33",
      "windDirDay": "东南风",
      "windScaleDay": "3"
    }
  ]
}
```

字段说明：

- `code`（string）：`200` 表示成功；其余为业务错误码（401 无 Key、404 城市编码不存在等）。
- `updateTime`（string）：数据更新时间。
- `daily`（array）：逐日预报数组（默认 7 天），元素为 Day 对象：
  - `fxDate`（string）：预报日期，`YYYY-MM-DD`。
  - `textDay` / `textNight`（string）：白天/夜间天气现象（晴/多云/雨…）。
  - `tempMin` / `tempMax`（string）：最低/最高气温，单位 ℃。
  - `windDirDay`（string）：白天风向。
  - `windScaleDay`（string）：白天风力等级。

失败时：stderr 输出 `查询失败: <原因>`，退出码非 0。
