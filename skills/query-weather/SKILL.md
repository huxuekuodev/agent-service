---
name: query-weather
description: 查询中国城市、省份天气情况；需要清晰识别用户要查询的城市或省份，避免查询错误的城市或省份；查询天气、查询气候、查询气温、查询降雨、查询风力等天气信息时使用
requires_tools: []
errors: errors.yaml
cleanup: cleanup.yaml
---
# 查询天气技能（完整执行流程）

本技能是一个整体：按下面顺序在**同一个沙箱**里执行，最后汇总成一个完整的天气答案。
`reference/` 下有两份参数说明，已在 `load_skill` 时一并给出，**调用脚本前务必按它构造参数**。

## 执行流程

### 1. 取城市/省份编码（Location_ID）

用 `scripts/get_city_code.py` 把用户说的城市或省份换成编码：

- 已知城市：`python3 scripts/get_city_code.py --city 石家庄`
- 只知省份（拿该省全部一级城市）：`python3 scripts/get_city_code.py --province 河北省`
- 无参数运行会输出用法说明；省名/城市名支持全称或简称（如 河北 / 河北省）

参数细节见 `reference/get_city_code.md`。输出为 JSON：`count`、`cities[]`（含 `Location_ID`、`Location_Name_ZH`）。
`count=0` 时按 `hint` 换一种写法重试，仍失败则向用户澄清地名（错误码 `CITY_LOOKUP_FAILED`）。

### 2. 用编码查天气

把上一步拿到的 `Location_ID`（6~12 位数字）传给 `scripts/get_city_weather.py`：

- `python3 scripts/get_city_weather.py <Location_ID>`（可加日期/天数参数，见 `reference/get_city_weather.md`）

输出为 JSON 天气数据（逐日/逐时段天气现象、气温、降水、风等）。脚本需要环境变量 `QWEATHER_API_KEY`
（沙箱已按 config 白名单注入）；若报 401/403，按 `WEATHER_API_ERROR` 规则处理并如实报告，
**不要编造天气数据**。

### 3. 汇总结果

- 只回答用户问的时段/范围，字段要全（日期、天气现象、最高/最低温、降水/风等，按用户问题取舍）；
- 多城市/多天时用清晰的列表或分组呈现，不要堆原始 JSON；
- 用户问题里的时间范围与城市必须与脚本结果一一对应，不确定就问，不要猜。

## 产出要求

- 一个干净的最终答复（不是脚本原始输出），数据全部来自脚本结果；
- 如果中途失败，说明失败在哪一步、原因是什么（错误码/提示），以及已尝试的修正。

## 注意

- 脚本**只能**通过 `sandbox_run` 在沙箱内执行，禁止本地运行；
- 两步之间不要关闭沙箱（`sandbox_close` 只在全部完成后调用）。
