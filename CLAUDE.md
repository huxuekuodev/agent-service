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
│   └── RAG_方案.md           # RAG 方案设计文档
├── tests/                    # 测试（当前基本为空）
└── app/                      # 主代码
    ├── main.py               # FastAPI 入口；lifespan 管理 AgentService 生命周期
    ├── agent_service.py      # 会话/对话服务层；无状态图 + 共享 checkpointer
    ├── config/
    │   ├── __init__.py       # AppConfig 等配置数据类；从 config.yaml 加载（惰性单例）
    │   └── agents.py         # agent 名校验（validate_agent_name）
    ├── core/
    │   ├── checkpointer.py   # checkpointer 工厂（memory / postgres）
    │   ├── context.py        # 请求级 trace_id（ContextVar）
    │   ├── log.py            # loguru 配置（trace_id 注入日志格式）
    │   ├── reflection.py     # 动态模块/类加载（resolve_class / resolve_variable）
    │   └── runtime.py        # RunContext（注入 checkpointer / app_config 到图运行时）
    ├── agents/
    │   ├── models.py         # 模型工厂：从配置实例化 ChatModel（thinking/vision 标志）
    │   ├── thread_state.py   # LangGraph 线程状态定义（messages + plan_tasks，reducer）
    │   ├── subtask.py        # SubTask 数据模型（DAG 子任务）
    │   ├── plan_document.py  # Plan DAG 数据模型（v1 遗留，StepStatus 等）
    │   ├── plan_storage.py   # Plan 存储抽象（内存/Redis 后端，v1 遗留）
    │   ├── plan_toolkit.py   # Plan 工具集 v2：create/update/get_plan_status（ContextVar 桥接）
    │   ├── tools.py          # 工具注册表：ask_clarification、plan/execute 工具加载
    │   ├── errors.py         # 规划节点 LLM 错误分类（可重试 vs 不可恢复）
    │   ├── current_time.py   # <current_time> 注入辅助（避免重复注入，可单测）
    │   ├── lead_agent/
    │   │   ├── agent.py      # 主图 GraphAgent（无状态编译图 + Send 并行派发）
    │   │   ├── graph_context.py  # GraphContext（app_config / plan_llm / langfuse_client 注入）
    │   │   ├── llm.py        # LLM 构建（create_llm / create_llm_with_name / create_execution_llm）
    │   │   └── tools.py      # 步骤执行工具定义
    │   ├── nodes/
    │   │   ├── plan_model_node.py   # 规划节点：澄清 + 规划 + 审查（结构化输出 SubTask DAG）
    │   │   ├── step_dispatch_node.py # 派发节点：筛选就绪任务 + fan-out 路由（Send/END）
    │   │   └── constants.py  # 共享常量（thinkMessage 等）
    │   ├── subagent/
    │   │   └── general_agent.py  # 通用执行 agent：执行单任务并写回结果
    │   ├── middlewares/
    │   │   ├── clarification_middleware.py   # 拦截 ask_clarification 并呈现给用户
    │   │   └── dangling_tool_call_middleware.py # 修复历史中悬空的 tool_call
    │   └── evaluation/
    │       ├── base.py       # BaseEvaluator 抽象基类（指标开关/阈值/LLM 打分/JSON 解析）
    │       ├── registry.py   # 评估器工厂（按 config `evaluators` 列表实例化）
    │       └── plan_evaluator.py  # PlanEvaluator：澄清质量 / 任务原子性 / agent 选择合法性
    ├── routers/
    │   ├── sessions.py       # 会话与对话接口（创建/列表/删除/chat SSE/chat sync）
    │   └── health.py         # 健康检查
    └── prompts/
        ├── plan_system_prompt_v2.md      # 规划节点系统提示词
        └── general_agent_system_prompt.md # 通用执行 agent 系统提示词
```

## 配置入口

`config.yaml` 是唯一配置源（路径优先级：显式 `config_path` > `AGENT_CONFIG_PATH` > `./config.yaml`），支持 `$ENV` 变量引用 `.env` 中的密钥。关键段：`models`（模型列表，首个为默认）、`langfuse`（追踪开关）、`plan_evaluation`（旧评估配置，向后兼容）、`evaluators`（推荐的可插拔评估器列表）、`subagents`、`database`（`memory` / `postgres`）。


## 项目规则
- 当创建新的配置项时，确保`config.yaml` 和 `config.example.yaml` 都有对应的更新。
- 编写所有的类与函数都要使用 `asyncio` 异步化，避免阻塞主线程 和 类型提示。

## 测试指令
- 写完代码后，运行 `uv run ruff check` 与 `uv run ruff format --check` 检查 lint / 格式。
- 运行 `uv run pytest` 执行测试，确保全部通过。