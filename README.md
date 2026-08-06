# Deer Agent Service

独立 Agent 服务：基于 LangGraph planner-execute 模式，FastAPI 提供会话管理与流式对话接口。

从 DeerFlow 的 `agentsv2` 抽离，简化依赖，独立部署。

## 架构

```
┌─────────────┐   POST /sessions/{id}/chat (SSE)
│  FastAPI    │ ───────────────────────────────►
│  app/main   │
└──────┬──────┘
       │ AgentService (会话管理)
       ▼
┌─────────────────────────────────────────────┐
│  GraphAgent (LangGraph 图)                  │
│  plan_model_node → step_dispatch → general  │
└─────────────────────────────────────────────┘
```

- **plan_model_node**：澄清 / 规划 / 反思（LLM-as-Judge 评估可选）
- **step_dispatch_node**：按 DAG 依赖筛选就绪任务，Send 派发
- **general_agent**：执行单个任务，写回结果

## 快速开始

```bash
# 1. 安装依赖
uv sync

# 2. 配置环境
cp .env.example .env
# 编辑 .env 填入模型 API Key

# 3. 启动服务
uv run uvicorn app.main:app --reload --port 8001
```

## API

### 创建会话

```bash
curl -X POST http://localhost:8001/sessions \
  -H "Content-Type: application/json" \
  -d '{"model_name": "default"}'
```

响应：
```json
{"session_id": "abc123...", "thread_id": "abc123..."}
```

### 对话（SSE 流式）

```bash
curl -N -X POST http://localhost:8001/sessions/abc123/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "河北最凉爽的城市是哪个？"}'
```

SSE 事件：
- `custom`：规划/执行状态（thinkMessage）
- `values`：完整 state 快照（含 AI 回复）
- `end`：流结束

### 对话（同步等待）

```bash
curl -X POST http://localhost:8001/sessions/abc123/chat/sync \
  -H "Content-Type: application/json" \
  -d '{"message": "河北最凉爽的城市是哪个？", "stream": false}'
```

### 会话管理

```bash
# 列出会话
curl http://localhost:8001/sessions

# 删除会话
curl -X DELETE http://localhost:8001/sessions/abc123
```

### 健康检查

```bash
curl http://localhost:8001/health
```

## 配置

通过环境变量（`.env`）配置，见 `.env.example`。

| 变量 | 说明 |
|------|------|
| `MODEL_NAME` / `MODEL_API_KEY` / ... | 主模型配置 |
| `LANGFUSE_ENABLED` | 是否启用 Langfuse 追踪/评估 |
| `PLAN_EVALUATION_ENABLED` | 是否启用规划节点 LLM-as-Judge 评估 |
| `LOG_LEVEL` | 日志级别 |

## 目录结构

```
agent-service/
├── pyproject.toml
├── .env.example
├── app/
│   ├── main.py              # FastAPI 入口
│   ├── agent_service.py     # 会话/对话服务
│   ├── config/              # 简化配置（环境变量）
│   ├── core/                # context/log/runtime/reflection
│   ├── agents/              # agentsv2 核心（抽离自 deerflow）
│   │   ├── nodes/           # plan_model/step_dispatch
│   │   ├── lead_agent/      # GraphAgent
│   │   ├── evaluation/      # LLM-as-Judge 评估
│   │   └── middlewares/     # clarification/dangling_tool_call
│   └── routers/             # FastAPI 路由
└── tests/
```
