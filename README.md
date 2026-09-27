# Deer Agent Service

独立部署的 Agent 服务：**LangGraph planner-execute 图 + FastAPI 会话/流式接口 + Vue 3 前端**。
支持任务规划与并行执行、技能（SKILL）沙箱执行、页内语音通话、计划确认中断与恢复、
打点监控平台，以及**评估闭环**（评估 → 归因 → 行动 → 验证）。

抽离自 DeerFlow 的 `agentsv2` 并简化依赖，可直接独立部署。

| | |
|---|---|
| 语言 / 依赖 | Python ≥ 3.12，`uv` 管理依赖 |
| 编排 | LangGraph 1.2（StateGraph / Send 并行派发 / RetryPolicy / checkpointer / interrupt） |
| 接入 | FastAPI + SSE（统一信封） |
| 存储 | PostgreSQL：**checkpointer 库**（运行态）+ **业务库**（用户/会话/消息/评估） |
| 前端 | Vue 3 + Vite（对话、确认卡片、通话面板、监控页） |
| 可观测 | Langfuse（trace / prompt 唯一来源 / 评估）+ 独立打点数据日志 + 监控平台 |
| 质量 | ruff（check + format）、pytest（104 passed） |

## 核心能力

| 能力 | 说明 | 设计文档 |
|------|------|---------|
| **规划-执行图** | 规划节点澄清/拆解 DAG → 派发节点按依赖筛选 → 执行节点并行执行 → 回到规划节点审查并给最终答复；无状态编译图 + 共享 checkpointer，多节点部署安全 | `CLAUDE.md` |
| **会话与鉴权** | 账号密码 + JWT（access/refresh 旋转），会话/消息落**业务库**（`messages` 按月分区），历史回显、逻辑删除并同步清理 checkpoint | [docs/会话持久化方案.md](docs/会话持久化方案.md) · [docs/业务库表结构设计.md](docs/业务库表结构设计.md) |
| **计划确认（中断/恢复）** | 执行前把计划交给用户确认：只展示步骤、可提意见（提意见则回规划重排）；`/chat` 挂起时返回 1103，恢复走 `/resume` | [docs/中断与恢复方案.md](docs/中断与恢复方案.md) |
| **SKILL 技能库** | 一个技能 = 一个执行单元：读整份 `SKILL.md` → 建沙箱 → 跑脚本 → 汇总；技能脚本在**一次性 Docker 容器**执行（宿主目录按天缓存、根文件系统只读、host 网络、超时 kill，可选回退 E2B） | [docs/SKILL_方案.md](docs/SKILL_方案.md) |
| **语音通话** | 页内「📞 语音通话」：浏览器原生识别 → 同一会话的 agent → 服务端**按句流式 TTS**（长文本不整段等）→ 波形动效 + 字幕 | [docs/语音通话方案.md](docs/语音通话方案.md) |
| **打点与监控** | 打点写入独立数据日志（protobuf 编码，不混执行日志）；监控页按业务名称/指标/模型聚合，可配置组件与字段含义 | [docs/打点设计.md](docs/打点设计.md) |
| **评估闭环** | 三个触发点（规划完成 / 每个任务完成 / 最终回复）**全异步**评估，带 prompt 版本与对象身份落库；低分样本自动归档；离线评测集 + 基线对比 | [docs/评估闭环方案.md](docs/评估闭环方案.md) |
| **LLM 渠道管理** | 渠道实例集中在 `app/llm/instances/`（每渠道一套参数），`config.yaml` 只做「角色 → 实例」映射；token 用量按问题/按用户计量 | `app/llm/` |

## 架构

```
┌────────────────────────────┐
│ Vue 3 前端 (5173)           │  对话 / 确认卡片 / 通话面板 / 监控页
└──────────┬─────────────────┘
           │ /auth /sessions /monitor /health /knowledge（JWT）
┌──────────▼─────────────────┐        ┌───────────────────────────┐
│ FastAPI (8001)             │        │ 业务库 agent_business      │
│  auth / sessions / monitor │◄──────►│ users/sessions/messages    │
│  knowledge / health        │        │ evaluations/eval_samples…  │
└──────────┬─────────────────┘        └───────────────────────────┘
           │ AgentService（会话编排 + 流事件翻译 + 评估链）
┌──────────▼─────────────────┐        ┌───────────────────────────┐
│ GraphAgent（无状态编译图）   │◄──────►│ checkpoint 库（运行态）     │
│  plan_model_node           │        └───────────────────────────┘
│    → step_dispatch_node    │
│    → general_agent ×N      │  技能脚本 → E2B 沙箱
│    → plan_model_node(审查)  │  注册工具 → 本地执行
└────────────────────────────┘
```

图内流程：`plan_model_node`（澄清 / 规划 / 审查）→ `step_dispatch_node`（筛选就绪任务 + 计划确认中断）
→ `step_fan_out_router`（`Send` 并行派发）→ `general_agent`（执行单任务并写回）→ 循环直至 `END`。

## 快速开始

前置：PostgreSQL（checkpointer 库 + 业务库）、Python ≥ 3.12、`uv`、Node.js（前端）。

```bash
# 1) 安装依赖
uv sync                      # 技能沙箱走 Docker，docker+paramiko 已含在主依赖里

# 2) 配置
cp .env.example .env         # 填模型 Key、POSTGRES_URL、BUSINESS_DATABASE_URL、JWT_SECRET 等
vim config.yaml              # 角色→实例映射、评估策略、技能、语音等（见「配置」）

# 3) 初始化业务库（幂等，可重复执行）
createdb agent_business
psql -d agent_business -f deploy/sql/business_schema.sql

# 4) 一键启动前后端（Ctrl-C 全部停止）
make dev                     # API http://127.0.0.1:8001 + Web http://127.0.0.1:5173
```

首次访问前端会要求登录：注册第一个账号即自动成为管理员（`/auth/register`）。

### 常用命令

| 命令 | 说明 |
|------|------|
| `make dev` | 同时启动后端(8001) + 前端(5173)；`make dev API_PORT=9000` 可改端口 |
| `make dev-api` / `make dev-web` | 只启动后端 / 只启动前端 |
| `make lint` | ruff check + format --check |
| `make test` | pytest |
| `make build-web` | 前端生产构建 |
| `make clean` | 清理缓存与构建产物 |
| `uv run python scripts/sync_langfuse_prompts.py --list` | 查看本地提示词与 Langfuse 版本差异（`--all` 全量推送） |
| `uv run python scripts/run_eval.py --smoke` | 离线评测跑批（冒烟 2 条），产物在 `reports/` |

## API 速查

所有接口（含 SSE 事件）统一信封，**HTTP 恒为 200**，业务结果看 `status`：

```json
{ "data": {...}, "msg": "", "status": 200 }
```

| 分组 | 接口 | 说明 |
|------|------|------|
| 认证 | `POST /auth/register` · `/auth/login` · `/auth/refresh` · `/auth/logout` · `GET /auth/me` · `POST /auth/password` | 登录返回 access/refresh；refresh 旋转即失效 |
| 会话 | `POST /sessions` · `GET /sessions` · `GET /sessions/{id}` · `PATCH /sessions/{id}` · `DELETE /sessions/{id}` | 列表键集分页 + 搜索；详情带 `pending_interrupt` |
| 消息 | `GET /sessions/{id}/messages?limit=&before_seq=` | 倒序取页、正序返回 |
| 对话 | `POST /sessions/{id}/chat`（SSE） · `POST /sessions/{id}/chat/sync` | 请求体支持 `voice: true`（语音播报）与 `client_msg_id`（幂等） |
| 恢复 | `POST /sessions/{id}/resume` | 恢复计划确认中断：`{answers:[{id,selected,custom}], interrupt_id}` |
| 反馈 | `POST /sessions/{id}/feedback?message_id=` | 👍/👎 用户反馈（进评估闭环） |
| 监控 | `GET /monitor/pages` · `/models` · `/query` · `/token-usage` · `GET/PUT /monitor/field-meanings` · `GET/POST/PUT/DELETE /monitor/components` | 打点聚合、监控组件与字段含义配置、token 用量 |
| 评估 | `GET /monitor/evaluations/summary|trend|runs|bad-cases|feedback` | 按 prompt 版本/模型/技能/节点切片、评估链、低分案例 |
| 知识库 | `POST /knowledge/sync` · `GET /knowledge/sync/status` | 语雀 → 分块 → 向量库摄取 |
| 健康 | `GET /health` | 存活检查 |

**SSE 事件契约**（`data` 为事件对象）：

| 事件 | 载荷要点 |
|------|---------|
| `custom` | `thinkMessage` / `thinking` / `tool_call` / `tool_result` / `plan` / `step` / `clarify` / `answer` / `error`（嵌套在 `data.data`） |
| `interrupt` | 运行挂起待确认：`data.interrupt_id` + `data.payload`（计划卡片） |
| `voice_audio` | 一块语音：`audio`(base64 mp3) / `text` / `seq` / `final`（**扁平字段**） |
| `voice_unavailable` · `voice_end` | 语音不可用（前端退化为浏览器朗读） / 本轮播报结束 |
| `end` | 流结束 |

**主要业务码**：`1000` 参数错误 · `1001` 资源不存在 · `1002` 内部错误 ·
`1100` 会话不存在 · `1103` 有挂起中断（改走 `/resume`）· `1104` 无挂起中断 · `1105` 卡片过期 ·
`1200` 未登录 · `1201` 令牌过期 · `1202` 令牌非法 · `1203` 用户已存在 · `1204` 密码强度不足 ·
`1206` 账号或密码错误 · `1207` 存储不可用。

### 最小调用示例

```bash
# 登录拿 token
TOKEN=$(curl -s -X POST http://127.0.0.1:8001/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"你的密码"}' | python -c 'import sys,json;print(json.load(sys.stdin)["data"]["access_token"])')

# 建会话 + 提问（SSE）
SID=$(curl -s -X POST http://127.0.0.1:8001/sessions -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{}' | python -c 'import sys,json;print(json.load(sys.stdin)["data"]["session_id"])')
curl -N -X POST "http://127.0.0.1:8001/sessions/$SID/chat" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"message":"查一下北京今天的天气"}'
```

## 配置

**`config.yaml` 是唯一配置源**（支持 `$ENV` 引用 `.env`）；两份模板需同步维护：
`config.yaml`（本地）+ `config.example.yaml`（可提交）。

| 配置段 | 作用 |
|--------|------|
| `models` | 模型**角色 → LLM 实例名**映射（`default` / `plan_node_model` / `general_node_model` / `evaluate_model`） |
| `tracking` | 打点数据日志路径（默认 `logs/tracking.data`，与执行日志分离） |
| `token_pricing` | 各角色输入/输出单价（元 / 1K tokens），用于费用统计 |
| `skills` | 技能库目录、总开关、E2B 沙箱（模板/超时/会话 TTL/并发上限） |
| `evaluators` | 三个评估器（plan / general / final_answer）的开关、采样、指标与阈值 |
| `evaluation_policy` | 评估策略：`mode=always｜worth_it｜off`、**三个触发点开关**、阈值、异步与并发 |
| `approval` | 计划确认中断：`plan_review` 总开关、是否展示内部任务、是否允许只提意见 |
| `voice` | 语音合成：接口地址、Key 环境变量名、模型/音色、分句切块参数 |
| `database` / `business_database` | checkpointer 库 / 业务库连接（`$DATABASE_URL` / `$BUSINESS_DATABASE_URL`） |
| `auth` | JWT 密钥（`$JWT_SECRET`）、access/refresh 有效期、密码最小长度 |
| `tools` | 第三方工具注册（`web_search`、`newest_doc` 等，可指向工厂函数或类） |

`.env` 关键变量：模型 Key（`DEEPSEEK_API_KEY` / `OPENAI_API_KEY` / `SILICONFLOW_KEY` / `DASHSCOPE_API_KEY`）、
`POSTGRES_URL`、`BUSINESS_DATABASE_URL`、`JWT_SECRET`、`LANGFUSE_*`、`TAVILY_API_KEY`、
`YUQUE_TOKEN`/`YUQUE_LOGIN`、`EMBEDDING_API_KEY`、技能沙箱的 `DOCKER_HOST_URL`（默认后端，形如 `ssh://root@host:22`，
要求免密登录；本机可用 `unix:///var/run/docker.sock`）、回退 E2B 时的 `E2B_API_KEY`/`E2B_TEMPLATE`/`E2B_DOMAIN`。

> **提示词的唯一来源是 Langfuse**（`plan_node_system_prompt`、`general_agent_system_prompt`、
> `plan_evaluator_prompt`、`general_evaluator_prompt`、`final_answer_evaluator_prompt`），
> `app/prompts/*.md` 只是本地副本；改完本地文件务必用 `scripts/sync_langfuse_prompts.py` 推送后才生效。

## 目录结构（关键部分）

```
agent-service/
├── app/
│   ├── main.py            # FastAPI 入口（lifespan 管理各服务生命周期）
│   ├── agent_service.py   # 会话编排 / 流事件 / resume / 评估链开链
│   ├── auth/              # 账号密码 + JWT（scrypt + HS256，标准库实现）
│   ├── session/           # 业务库存储与编排（用户/会话/消息）
│   ├── evaluation/        # 评估闭环：身份 / 策略 / 存储 / 记录器
│   ├── voice/             # 语音：播报文本分句 + 服务端 TTS
│   ├── monitor/           # 监控平台后端（打点聚合 + 评估查询）
│   ├── llm/               # 渠道实例、工厂、用量采集（token 计量）
│   ├── agents/            # 图与节点、工具、技能、评估器、事件层、中间件
│   │   ├── graph/         # 主图 GraphAgent + GraphContext
│   │   ├── plan/          # 规划节点：schema / dag / protocol / prompt
│   │   ├── dispatch/      # 派发节点：筛选就绪任务 + fan-out 路由
│   │   ├── execute/       # 执行节点：执行单任务并写回结果
│   │   ├── state/         # thread_state / subtask
│   │   ├── common/        # events / errors / interrupts / current_time
│   │   ├── skills/        # SKILL：registry/loader/sandbox/session/tools
│   │   ├── tools/         # 工具注册表（web / knowledge / yuque）
│   │   ├── middlewares/   # 澄清拦截 + 悬空 tool_call 修复
│   │   └── evaluators/    # 三个评估器 + BaseEvaluator
│   ├── routers/           # auth / sessions / knowledge / health
│   ├── core/              # checkpointer / log / context / tracking
│   └── prompts/           # 提示词本地副本（Langfuse 为唯一来源）
├── web/                   # Vue 3 前端（src/components：ChatArea / ConfirmCard / CallPanel / MonitorView…）
├── skills/                # 技能仓库（query-weather 等：SKILL.md + scripts/ + data/ + reference/）
├── evalsets/              # 离线评测集（YAML）
├── deploy/sql/            # 业务库 DDL（幂等，可重复执行）
├── scripts/               # sync_langfuse_prompts.py / run_eval.py
├── docs/                  # 设计文档（见下表）
└── tests/                 # pytest（协议/策略/分句/沙箱/评估等离线单测）
```

完整的模块级说明与开发约定见 **[CLAUDE.md](CLAUDE.md)**。

## 测试与质量

```bash
make lint                                  # ruff check + format --check
make test                                  # pytest（当前 104 passed；test_rag_splitter 4 项为既有失败）
make build-web                             # 前端构建
uv run python scripts/run_eval.py --limit 8 --label baseline-1   # 离线评测（含基线对比）
```

评估跑批会在 `reports/eval-<label>.json` 生成每指标「基线 vs 候选」对比；
`evalsets/core.yaml` 是评测集（问题 + 标签 + 期望要点），建议补到 ≥30 条再用于发布前门禁。

## 文档索引

| 文档 | 内容 |
|------|------|
| [docs/会话持久化方案.md](docs/会话持久化方案.md) | 会话列表/历史回放、业务库与逻辑删除、实施状态 |
| [docs/业务库表结构设计.md](docs/业务库表结构设计.md) | `users/sessions/messages/evaluations/eval_samples` 等表设计与运维 SQL |
| [docs/中断与恢复方案.md](docs/中断与恢复方案.md) | 计划确认中断协议、`/resume`、为什么不能在 `/chat` 里兼容 |
| [docs/消息流转与展示方案.md](docs/消息流转与展示方案.md) | Plan 节点 transcript / UI 双轨（append-only 保 KV 缓存） |
| [docs/SKILL_方案.md](docs/SKILL_方案.md) | 技能定义、沙箱会话、执行模型 v2、错误与清理规则 |
| [docs/语音通话方案.md](docs/语音通话方案.md) | 语音链路选型与实测、分句切块、通话界面动效与打断 |
| [docs/打点设计.md](docs/打点设计.md) | 打点模型（type/page/source/ext）、protobuf 编码与解码 CLI |
| [docs/评估闭环方案.md](docs/评估闭环方案.md) | 评估 → 归因 → 行动 → 验证：三触发点、全异步、评测集跑批 |
| [docs/RAG_方案.md](docs/RAG_方案.md) | 知识库摄取与检索（语雀 → 分块 → 向量库） |

## 开发约定

- 新增配置项必须同时更新 `config.yaml` 与 `config.example.yaml`；
- 类与函数一律 `async`（不阻塞事件循环）+ 完整类型提示；
- 提示词改动走 Langfuse（本地副本 + 同步脚本），评估/打点要带**版本与身份**，便于对比与归因；
- 修复问题要走生产级方案，而不是绕过（如中断恢复必须显式 `/resume`，不在 `/chat` 里静默兼容）；
- **不要动宿主机环境**：`.venv`、`uv sync`、`pip install` 等只在宿主机执行。
