# Deer Agent Service

## 项目描述

独立部署的 Agent 服务，从 DeerFlow 的 `agentsv2` 抽离并简化依赖。基于 LangGraph 的 planner-execute 模式：FastAPI 提供会话管理与流式对话接口，规划节点负责澄清/拆解任务，执行节点并行执行子任务，多轮循环直至完成。

核心架构：**无状态编译图 + 共享 checkpointer**。`GraphAgent` 全局单例、不绑定线程，状态通过共享 checkpointer 按 `thread_id` 持久化，集群多节点部署下用户请求发散到任意节点都不会丢失会话上下文。

主图流程：`plan_model_node`（澄清/规划/审查，可选 LLM-as-Judge 评估）→ `step_dispatch_node`（按 DAG 依赖筛选就绪任务）→ `step_fan_out_router`（`Send` 并行派发）→ `general_agent`（执行单个任务并写回结果）→ 循环直至 `END`。

## 技术栈

| 类别 | 技术 |
|------|------|
| 语言 | Python >= 3.12 |
| Web 框架 | FastAPI >= 0.115 + uvicorn + sse-starlette（SSE 流式响应） |
| Agent 编排 | LangGraph 1.x（StateGraph / Send 并行派发 / RetryPolicy / checkpointer） |
| LLM 接入 | langchain >= 1.3、langchain-deepseek、langchain-openai |
| 状态持久化 | langgraph-checkpoint-postgres（集群共享状态）+ psycopg / psycopg-pool；单机开发可用 InMemorySaver |
| 可观测 | Langfuse >= 4.0（追踪 / prompt 管理 / 评估落盘） |
| 配置 | config.yaml（YAML，支持 `$ENV` 变量引用）+ python-dotenv 加载 `.env` |
| 数据模型 | pydantic >= 2.12 |
| 日志 | loguru（trace_id 贯穿全链路） |
| 工具 | uv（依赖与脚本）、ruff（lint + format，line-length 240）、pre-commit、pytest + pytest-asyncio |

## 目录结构

```
agent-service/
├── pyproject.toml            # 项目元数据与依赖（uv 管理）
├── uv.lock                   # 锁文件
├── config.yaml               # 主配置（不入库；models / langfuse / evaluators / database 等）
├── config.example.yaml       # 配置模板（可提交）
├── .env.example              # 环境变量模板（API Key、DATABASE_URL 等）
├── .pre-commit-config.yaml   # pre-commit 钩子（ruff lint / format）
├── ruff.toml                 # ruff 配置
├── debug.py                  # 调试入口：直接运行可断点调试完整图流程
├── docs/
│   ├── RAG_方案.md           # RAG 方案设计文档
│   ├── SKILL_方案.md         # SKILL（标准 SOP + 沙箱执行 + 人工介入）设计文档
│   ├── 消息流转与展示方案.md  # Plan 节点 transcript / UI 双轨（append-only 保 KV 缓存）方案
│   ├── 会话持久化方案.md      # 会话列表/历史回放方案（已定：独立业务库 + 逻辑删除 + 真实用户）
│   └── 业务库表结构设计.md    # 业务库表结构说明（users/sessions/messages/session_events）
├── deploy/sql/business_schema.sql  # 业务库建表 DDL（幂等，可直接执行）
├── skills/                   # 技能仓库：每子目录一个 skill（SKILL.md + reference/ + data/ + scripts/ + errors/cleanup.yaml）
├── scripts/                  # 运维脚本：sync_langfuse_prompts.py（本地提示词 → Langfuse 推送）
├── tests/                    # 测试（当前基本为空）
└── app/                      # 主代码
    ├── main.py               # FastAPI 入口；lifespan 管理 AgentService 生命周期
    ├── agent_service.py      # 会话/对话服务层；无状态图 + 共享 checkpointer
    ├── config/
    │   ├── __init__.py       # AppConfig 等配置数据类；从 config.yaml 加载（惰性单例）
    │   └── agents.py         # agent 名校验（validate_agent_name）
    ├── auth/                 # 认证：账号密码 + JWT（全部标准库实现，零额外依赖）
    │   ├── password.py       # 密码哈希（scrypt$n$r$p$salt$hash，恒定时间校验 + 透明升级）
    │   ├── tokens.py         # JWT 签发/校验（HS256 白名单，access/refresh + jti）
    │   ├── service.py        # AuthService：注册/登录/刷新（旋转）/登出/改密
    │   └── deps.py           # FastAPI 依赖 current_user / optional_user
    ├── session/              # 会话持久化（业务库）
    │   ├── store.py          # 业务库连接池 + users/user_tokens/sessions/messages CRUD（唯一 SQL 出口）
    │   └── service.py        # SessionService（会话编排 + AssistantReplyCollector 回复收集）
    ├── core/
    │   ├── checkpointer.py   # checkpointer 工厂（memory / postgres）
    │   ├── context.py        # 请求级 trace_id（ContextVar）
    │   ├── log.py            # loguru 配置（trace_id 注入日志格式）
    │   ├── reflection.py     # 动态模块/类加载（resolve_class / resolve_variable）
    │   ├── runtime.py        # RunContext（注入 checkpointer / app_config 到图运行时）
    │   └── tracking/         # 打点工具包：protobuf 编/解码 + 独立数据日志（constants/model/encoder/decoder/tracker/tracking.proto/__main__，见 docs/打点设计.md）
    ├── llm/                  # 大模型渠道管理（每个渠道一个实例，只配置一套；所有 LLM 操作在此）
    │   ├── base.py           # LLMInstance 数据类 + 注册表（register / get / list）
    │   ├── factory.py        # create_chat_model：按角色名→实例构建 ChatModel
    │   ├── builders.py       # create_llm / create_llm_with_name / create_execution_llm（按运行配置构建）
    │   ├── usage.py          # UsageCollector：callback 采集一次请求全部模型调用的 token 用量（按问题/按用户计量的数据源）
    │   └── instances/        # 渠道实例：deepseek / deepseek_bak / openai / qwen（stream_usage=True 才能拿到用量）
    ├── agents/
    │   ├── thread_state.py   # LangGraph 线程状态定义（messages + plan_tasks，reducer）
    │   ├── subtask.py        # SubTask 数据模型（DAG 子任务）
    │   ├── plan_document.py  # Plan DAG 数据模型（v1 遗留，StepStatus 等）
    │   ├── plan_storage.py   # Plan 存储抽象（内存/Redis 后端，v1 遗留）
    │   ├── plan_toolkit.py   # Plan 工具集 v2：create/update/get_plan_status（ContextVar 桥接）
    │   ├── skills/           # SKILL 能力：技能库 + 沙箱会话（registry/loader/sandbox/session/tools，见 docs/SKILL_方案.md）
    │   │                     # 技能=整体执行单元（任务上标 skill_id）：load_skill 读整份 SKILL →
    │   │                     # sandbox_create → sandbox_run → sandbox_close；规划节点不再做技能错误恢复
    │   ├── tools/            # 工具注册表（包）：按业务分类组织第三方工具
    │   │   ├── __init__.py   # 对外 API：get_plan_tools / get_execute_tools（自动追加技能工具链）等
    │   │   ├── registry.py   # 从 config `tools` 段加载工具类，按 allowed_agents 过滤
    │   │   ├── builtin.py    # 内置工具：ask_clarification（澄清）
    │   │   ├── web/          # 联网类工具（web_search，基于 Tavily API）
    │   │   ├── knowledge/    # 知识库类工具（internal_kb 私有知识库示例）
    │   │   └── yuque/        # 语雀类工具（newest_doc 文档修订对比）
    │   ├── events.py         # 节点→前端统一事件输出层（EventType/Output/ToolCallAccumulator）
    │   ├── errors.py         # 规划节点 LLM 错误分类（可重试 vs 不可恢复）
    │   ├── current_time.py   # <current_time> 注入辅助（避免重复注入，可单测）
    │   ├── lead_agent/
    │   │   ├── agent.py      # 主图 GraphAgent（无状态编译图 + Send 并行派发）
    │   │   ├── graph_context.py  # GraphContext（app_config / plan_llm / langfuse_client 注入）
    │   │   └── tools.py      # 步骤执行工具定义
    │   ├── nodes/
    │   │   ├── plan_model_node.py   # 规划节点：澄清 + 规划 + 审查（结构化输出 SubTask DAG）
    │   │   ├── step_dispatch_node.py # 派发节点：筛选就绪任务 + fan-out 路由（Send/END）
    │   │   └── constants.py  # 共享常量（thinkMessage 等）
    │   ├── subagent/
    │   │   └── general_agent.py  # 通用执行 agent：执行单任务并写回结果（含执行评估触发）
    │   ├── middlewares/
    │   │   ├── clarification_middleware.py   # 拦截 ask_clarification 并呈现给用户
    │   │   └── dangling_tool_call_middleware.py # 修复历史中悬空的 tool_call
    │   └── evaluation/
    │       ├── base.py       # BaseEvaluator 抽象基类（指标开关/阈值/LLM 打分/JSON 解析）
    │       ├── registry.py   # 评估器工厂（按 config `evaluators` 列表实例化）
    │       ├── plan_evaluator.py  # PlanEvaluator：任务原子性/依赖正确性/决策准确性等（Langfuse plan_evaluator_prompt）
    │       └── general_evaluator.py # GeneralEvaluator：执行节点路径效率（1-5 分）
    ├── routers/
    │   ├── auth.py           # 认证接口（register/login/refresh/logout/me/password）
    │   ├── sessions.py       # 会话与对话接口（增删改查/历史/chat SSE/chat sync，JWT 鉴权 + 落库）
    │   └── health.py         # 健康检查
    ├── monitor/              # 监控平台后端（/monitor/*）：组件配置/字段含义/用户token（PG）+
    │   │                     # 打点数据聚合查询（store.py / query.py / router.py）
    │   │                     # usage.py：用量记账（user_token_usage 累计 + token_usage 打点）
    │   └── (web/src/components/MonitorView.vue + MonitorChart.vue 监控页)
    └── prompts/
        ├── plan_system_prompt_v2.md      # 规划节点系统提示词
        ├── general_agent_system_prompt.md # 通用执行 agent 系统提示词
        ├── plan_evaluator_prompt.md      # 规划评估提示词（本地副本，Langfuse 唯一来源）
        └── general_evaluator_prompt.md    # 执行节点评估提示词（本地副本，Langfuse 唯一来源）
```

## 配置入口

`config.yaml` 是唯一配置源（路径优先级：显式 `config_path` > `AGENT_CONFIG_PATH` > `./config.yaml`），支持 `$ENV` 变量引用 `.env` 中的密钥。关键段：`models`（模型角色 → LLM 实例名映射，实例见 `app/llm/instances/`，每个实例只配置一套）、`langfuse`（追踪开关）、`tracking`（打点独立数据日志，默认 `logs/tracking.data`，不写入 app.log）、`token_pricing`（Token 计费：模型角色 → 输入/输出单价，元 / 1K tokens）、`skills`（技能库目录 + `enabled` 总开关 + E2B 沙箱执行配置，见 docs/SKILL_方案.md）、`plan_evaluation`（旧评估配置，向后兼容）、`evaluators`（推荐的可插拔评估器列表）、`subagents`、`database`（checkpointer：`memory` / `postgres`）、`business_database`（**业务库**：用户/会话/消息/监控配置，`$BUSINESS_DATABASE_URL`，建表 `deploy/sql/business_schema.sql`，见 docs/业务库表结构设计.md）、`auth`（账号密码 + JWT：`jwt_secret` / `access_ttl_minutes` / `refresh_ttl_days`）。


## 快速启动（Makefile）

- `make dev`：同时启动后端（http://127.0.0.1:8001，uvicorn --reload）与 Web（http://127.0.0.1:5173，vite dev，`/auth /sessions /monitor /health /knowledge` 代理到后端），Ctrl-C 一键全部停止
- `make dev-api` / `make dev-web`：分别启动后端 / Web
- `make lint`（ruff check + format --check）/ `make test`（pytest）/ `make build-web`（vite build）/ `make clean`（清理缓存）
- 启用 SKILL 沙箱前需在宿主机安装可选依赖并配置（见 docs/SKILL_方案.md）：`uv sync --extra sandbox`

## 项目规则
- 当创建新的配置项时，确保`config.yaml` 和 `config.example.yaml` 都有对应的更新。
- 编写所有的类与函数都要使用 `asyncio` 异步化，避免阻塞主线程 和 类型提示。
- 修复问题是不应该已越狱的方式，而是应该直面问题找到生产级的解决方案。
- **不要动宿主机的 venv / 解释器 / 依赖环境**：`.venv`、`uv sync`、`pip install`、重建软链接等环境级操作只在宿主机（macOS 终端）执行；沙箱（Linux VM）内禁止环境重建，只做只读诊断，修复指令交给用户。

## 测试指令
- 写完代码后，运行 `uv run ruff check` 与 `uv run ruff format --check` 检查 lint / 格式。
- 运行 `uv run pytest` 执行测试，确保全部通过。