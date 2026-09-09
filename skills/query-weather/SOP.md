# SOP：天气查询（query-weather）

## step-01 获取城市编码

```yaml
script: scripts/get_city_code.py
args_required: true
args_hint: 依据用户输入二选一：--province <省名>（如 河北省）或 --city <城市/县名>（如 北京市）
```

依据用户输入决定参数形态并在沙箱内运行脚本：
- 用户给了省份 → `arguments = ["--province", "<省名>"]`（如 河北省），输出该省一级城市列表；
- 用户给了具体城市/县 → `arguments = ["--city", "<城市名>"]`（如 北京市），输出该城市 Location_ID；
- 城市与省份都没有（输入模糊）→ 不要跑脚本，回到规划节点要求澄清。

**产物要求**：本步骤的最终结果必须保留脚本输出的 JSON 语义——对每个城市给出 `city` 与 `city_code`（
`cities[].city_code`，如 101090101），**禁止只输出城市名列表而丢弃编码**（后续 step-02 依赖编码查询）。
脚本 stdout 即上述 JSON（字段见 SKILL.md「get_city_code」契约）。本步骤产物：城市及编码。

## step-02 查询逐日天气

```yaml
script: scripts/get_city_weather.py
args_required: true
args_hint: arguments 仅一项纯数字城市编码：["<city_code>"]（如 ["101090101"]）；city_code 必须是 step-01 输出 JSON 中 cities[].city_code 字段的数值，禁止传城市名/中文
```

把上一步（或用户直接给出）的 `city_code`（**纯数字 LocationID**，来自 step-01 输出的
`cities[].city_code` 字段，如 `101090101`）作为 `arguments[0]` 传入沙箱运行，每次只能传递一个城市编码：
`arguments = ["101090101"]`。和风 API Key 走沙箱环境变量 `QWEATHER_API_KEY`，无需作为参数。
脚本 stdout 为易读天气文本（多行，每行一天）。本步骤产物：天气信息（最终输出）。
