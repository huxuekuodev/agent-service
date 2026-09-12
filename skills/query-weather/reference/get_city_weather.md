
## get_city_weather

根据城市编码city_code查询天气，返回详细的天气情况，如果需要查询多个城市编码，需要分批调用，每次只能查询一个城市的天气。
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