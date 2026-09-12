## get_city_code
必须向外传递city_code字段，用于后续查询天气,不能只返回城市名称，如果用户同时查询多个城市，需要分批调用get_city_weather，每次只能查询一个城市的天气。
目前只支持查询：省份、具体城市/县。
### 调用方式
脚本以命令行参数运行（stdout 输出 JSON）：

```bash
python3 get_city_code.py --province 河北省   # 省份下的一级城市编码
python3 get_city_code.py --city 正定         # 具体城市/县的 city_code
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
