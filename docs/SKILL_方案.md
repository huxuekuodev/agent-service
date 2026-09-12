# SKILL 能力接入方案（langgraph v1.2.10 + langchain v1.3.14）

> 落地进度见文末「10. 落地进度」；本文为总体设计，细节以代码为准。

## 1. 目标与原则

- **不改变现有执行环**：summarization → plan(澄清/规划/审查) → dispatch(DAG+Send fan-out) → general_agent → … → plan(审查) 的闭环原样保留；
- skill 只解决两件事：**让规划节点知道"有哪些标准做法"**（拆得更准），**让失败有标准恢复路径**（含人工介入清理）；
- 现状中可直接复用的点：`app/agents/tools/registry`（工具注入）、`describe_execute_tools_v2`（能力描述进规划提示词）、`ask_clarification` 链路（澄清/中断）、打点监控（skill 使用率/失败率）、Postgres checkpointer（interrupt 恢复的基础）、`plan_tasks` merge reducer（update 追加恢复任务）。

## 2. Skill 标准定义（目录即技能，纯 Markdown）

```
skills/
└── <skill_id>/                    # 如 yuque-doc-review
    ├── SKILL.md                   # 唯一入口：frontmatter + 角色/能力/步骤大纲
    ├── reference/                 # 参考文档：脚本参数说明等（load_skill 内联给 LLM）
    ├── errors.yaml                # 错误处理（YAML）：错误码 → 处置(重试/恢复DAG/人工) → 恢复入口
    ├── cleanup.yaml               # 产物清单（YAML）：路径/类型/清理等级(auto|confirm|forbidden)
    └── assets/                    # 模板、脚本、示例（可选，执行时按需读取）
```

`SKILL.md` frontmatter（加载器校验的机器可读部分）：

```markdown
---
name: yuque-doc-review
description: 按标准 SOP 拉取并对比语雀文档历史修订
when_to_use: 用户要求查看/对比语雀文档某天的修订内容时
requires_tools: [yuque]            # 校验执行侧工具是否可用
errors: errors.yaml
cleanup: cleanup.yaml
allowed_agents: [general_agent]
---
```

**为什么技能是「整体执行单元」而不是步骤集合**：技能内部的脚本调用顺序、参数、错误处置都写在
SKILL.md 里（连同 reference/ 参考文档），框架不解析、不拆分——执行 agent 用自己的判断在**同一个
沙箱会话**里跑完整条流程，再由规划节点审查结果。这样既保留了标准做法（可信的脚本与参数），
又不会因为"一个步骤一个新沙箱"而丢中间状态、也不受框架步骤 schema 的限制。

## 3. Skill 注册表 / 加载层（`app/agents/skills/`）

```
app/agents/skills/
├── __init__.py        # 对外：match_skills / load_skill_context / get_error_rule / get_cleanup_rules
├── registry.py        # 启动时扫描 skills/：解析 frontmatter → SkillMeta[]；校验 requires_tools
├── loader.py          # 加载整份 SKILL.md + 文件清单 + errors.yaml/cleanup.yaml（不拆分步骤）
├── models.py          # SkillMeta / SkillFile / ErrorRule / CleanupRule / SkillContext
└── tools.py           # 内置 skill 工具：list_skills / load_skill / sandbox_create / sandbox_run /
                       #                  sandbox_close / sandbox_list / query_error
```

- 注册表在每次图构建/规划时刷新（开发期技能可热更）；
- 工具注册：在 `app/agents/tools/registry` 或内置工具清单中新增 `category: skill`，随 `get_execute_tools()` 注入执行 agent。

## 4. 流程改动（叠加层，原逻辑不动）

### 4.1 澄清后：skill 候选（top-3）
`plan_model_node` 的系统提示词新增 `<SkillsIndex>`（注册表导出：每个 skill 的 name + when_to_use + description，≤3 行/skill）。`PlanOutput` 增加：

```python
skills: list[SkillChoice] = []   # SkillChoice{skill_id, reason}，最多 3 个候选
```

澄清已满足、无需澄清时：模型产出 `action=create` + `skills=[...]`（候选）+ 业务任务大纲。

### 4.2 命中 skill：上下文任务 = DAG 的第 0 个任务（用户要求）
`plan_model_node` 在把模型输出的 tasks 转 `SubTask[]` 之前，**机器注入**一个固定首任务（不由模型编 plan_id）：

```python
SubTask(
  plan_id="skill_probe",                    # 固定前缀，用户任务依赖它
  name=f"校验并加载 skill「{skill_id}」上下文",
  desc=...,                                 # 指令：调 load_skill 校验可用性，产出 SOP/错误/清理索引摘要
  sort=0,
  deps=[], execution_agent="general_agent",
)
```

并把模型输出的每个业务任务 `deps` 自动加上 `"skill_probe"`（`{skill_probe}` 占位符会把上下文摘要注入后续任务 desc）。**调度/执行沿用现有 DAG+Send，零改动**。

### 4.3 执行：失败标准信号（不再裸抛中断全图）
现状 `general_agent` 异常直接 `raise` → 整图报错。改为：

```python
except Exception as exc:
    return {"plan_tasks": [SubTask(plan_id=..., step_statuses="failed",
        result="", blocked_message=错误摘要,
        error_code=classify...)]}   # 扩展 SubTask.error_code
```

- SubTask 扩展字段：`skill_id / error_code`（技能整体执行，**没有** sop_step）；
- ThreadState 扩展 `artifacts`（产物登记表）与 `skill_contexts`；
- 错误分类复用 `app/agents/errors.py` 的 classify + skill 自定义错误码。

### 4.4 审查分支：失败 → skill errors.yaml 驱动恢复
现有路由：全部任务终态 → 回 `plan_model_node`（review）。扩展 review 提示词：
- 若存在 `failed` 任务且其 skill 有 `errors.yaml`：
  1. `query_error(error_code)` 取处置规则；
  2. **自动可恢复** → 产出恢复任务 DAG（`action=update`，走既有 merge reducer 追加新任务，标注 `recovery_of=<plan_id>`），再调度；
  3. **需人工**（清理/回滚 confirm 级产物、或 LLM 判定无法自动恢复）→ 走第 5 节人工介入；
  4. 打点 `page=skill, type=step_failed / recovery_plan / cleanup_decision`，监控页可观测。

## 5. 人工介入（删除临时文件/数据的确认）

### 5.1 通道：LangGraph `interrupt()`（推荐）
graph 内（review 或执行节点）调用：

```python
from langgraph.types import interrupt

decision = interrupt({
  "type": "cleanup_confirm",
  "title": f"任务 {plan_id} 异常，是否删除已产生的临时产物？",
  "artifacts": [{"path": p, "kind": k, "size": n, "cleanup_level": "confirm"}],
})
```

- interrupt 依赖 **checkpointer**（当前已是 Postgres，天然支持）；图暂停并返回 `__interrupt__`，请求正常收尾；
- 前端 ChatArea 收到该事件渲染「确认卡片」：产物清单 + [全部删除] [保留] [仅列表]；
- 用户选择 → POST `/sessions/{id}/resume`（新接口，payload=决策）→ 同 thread_id 再次进图，`agent.astream(Command(resume=decision))` 恢复执行；
- 恢复后按决策清理并打点；确认保留/删除都写入 `artifacts` 状态与数据日志，可审计。

### 5.2 清理等级（由 skill cleanup.yaml 声明 + 全局默认兜底）
| 等级 | 语义 | 处理 |
|------|------|------|
| auto | 安全临时产物 | 执行节点 finally/成功路径自动删，无人工 |
| confirm | 删除/回滚需确认 | `interrupt` 人工确认后才删 |
| forbidden | 审计保留 | 不删，仅标记状态 |

会话级兜底：用户终止/图异常退出时，所有 `auto` 产物由会话清理任务回收；`confirm/forbidden` 进入待决清单供人工处理。

### 5.3 复用 vs 新增
- **不复用** `ask_clarification` 做确认（那是"信息澄清"，语义不同，避免前端混淆）；
- 新端点集中在 `routers/sessions`：`GET /sessions/{id}/pending`（当前待决确认/中断）、`POST /sessions/{id}/resume`（提交决策）。

## 6. ThreadState / SubTask 增量（数据契约）

```python
class ThreadState(TypedDict, total=False):
    ...
    skill_contexts: dict            # skill_id -> {meta, files, errors_index, cleanup}
    artifacts: Annotated[list[dict], merge_artifacts]   # 产物登记表
    active_skill: str               # 当前主 skill（一次一个主 skill）

class SubTask(BaseModel):
    ...
    skill_id: str = ""              # 命中 skill
    error_code: str = ""            # failed 原因
    recovery_of: str = ""           # 若为恢复任务，指向被恢复的 plan_id
```

## 7. 可观测 / 配置

- 打点新增 `TrackingPage.SKILL` + 类型 `skill_used / skill_probe / step_failed / recovery_plan / cleanup_decision`（沿用 protobuf schema，槽位含义按 page=skill 在监控平台配置）；
- config.yaml 新增 `skills:` 段：`dir: ./skills`、`cleanup_default: confirm`、`max_candidates: 3`；
- 监控页直接复用现有组件（page=skill 生成监控卡）。

## 8. 分阶段实施

**P0 · 主流程闭环（skill 正确性）**
1. `skills/` 目录规范 + 1 个示例 skill（含 SKILL.md / SOP / errors / cleanup）
2. `app/agents/skills/`：registry/loader/models/tools
3. 规划节点：注入 `<SkillsIndex>` + `PlanOutput.skills` + 机器注入 `skill_probe` 首任务 + 依赖前缀
4. general_agent：失败返回 `failed`（含 error_code），不再裸抛；产物登记
5. review 分支：失败 → errors.yaml 恢复 DAG（update 语义）

**P1 · 人工介入**
6. `interrupt()` 清理确认 + `/sessions/{id}/pending`、`/resume` + 前端确认卡片
7. cleanup 审计打点 + `artifacts` 终态回收

**P2 · 强化**
8. skill 匹配升级（embedding 检索/LLM 打分）、多 skill 串联、上下文压缩
9. 评估器增加 skill 维度；监控页 skill 面板（使用率/失败率/清理决策统计）

## 9. 待确认的设计决策

| # | 决策点 | 推荐 | 备选 |
|---|--------|------|------|
| D1 | 人工介入通道 | LangGraph `interrupt()` + resume 端点 | 复用澄清链路改造成确认 |
| D2 | skill 上下文任务载体 | DAG 首任务 `skill_probe`（general_agent + load_skill 工具） | 独立 `skill_resolver_node` |
| D3 | skill 候选匹配（P0） | 提示词注入索引 + LLM 候选（≤3） | registry 关键词/embedding 检索 |
| D4 | 一次执行的 skill 数 | 1 个主 skill（候选 3 个择一） | 支持多 skill 拼接 SOP |

> 建议先按 D1-D4 推荐项实现 P0+P1，跑通「语雀修订对比」作为示例 skill 后，再决定 P2 增强项。

## 10. 落地进度

### 已完成
- **框架包 `app/agents/skills/`**：models（SkillMeta/SkillFile/ErrorRule/CleanupRule/SkillContext）、
  registry（扫描 + frontmatter + 工具依赖校验 + `<SkillsIndex>` 导出）、
  loader（整份 SKILL.md + 文件清单 + errors/cleanup 解析，结构化数据一律 YAML）、
  sandbox（E2B：传目录 / 跑命令 / 销毁）、session（沙箱会话：按 skill 复用 + TTL 回收 + 并发串行）、tools。
- **E2B 沙箱（会话化）** 与 **技能工具链注入执行 agent**（list_skills / load_skill / sandbox_create /
  sandbox_run / sandbox_close / sandbox_list / query_error 自动追加进 `get_execute_tools()`）：
  技能脚本 → 沙箱内执行，注册工具 → 本地执行。
- **配置**：`skills:` 段（dir/cleanup_default/max_candidates/sandbox）；可选依赖 `sandbox = [e2b-code-interpreter]`。
- **P0 代码接线（本轮）**：
  - `SubTask` 增加 `skill_id / error_code`（一个技能一个任务）；
  - `plan_model_node`：能力描述末尾拼接 `<SkillsIndex>`（registry.skill_index_text，带工具可用性标注，
    走既有 `capability_descriptions` 编译变量、无新增占位符）；技能**只由任务上的 `skill_id` 表达**
    （`PlanOutput` 已无 `skills` 候选字段：技能与任务是同一件事，不再有第二个声明通道）；
    计划里出现技能任务时机器注入 `plan_id="skill_probe"` 前置校验任务（create 必注入 /
    update 在无校验任务时注入），业务任务 deps 自动前缀；
    **不再注入 `<SelectedSkill>` / `<SkillErrors>`**：技能流程由执行侧 `load_skill` 读整份 SKILL.md 获得，
    错误规则由执行侧 `query_error` + 自行修正处理，规划节点只对"最终 failed"按通用规则决定重试或如实收尾；
  - `general_agent`：`skill_probe` 由节点确定性执行（**执行前唯一验证点**：技能可用性 + 沙箱环境 +
    技能文件清单/错误规则，不走 LLM）；技能任务注入整技能执行指引
    （load_skill → sandbox_create → sandbox_run → 汇总 → sandbox_close）；
    执行异常不再裸抛中断全图 → 返回 `step_statuses="failed"`；
  - `step_fan_out_router`：存在 `failed` 任务时回 `plan_model_node`（恢复 DAG 入口）；
  - 提示词（运行时唯一来源是 Langfuse；本地文件为备份副本）：plan 提示词新增 SkillsIndex / 技能识别 /
    失败反思章节与输出 schema（skills/skill_id 字段，**一个技能一个任务，不拆步骤**）；
    general_agent 提示词新增「技能任务」整技能执行指引（读整份 SKILL → 准备沙箱 → 执行 → 汇总 → 关沙箱）。
    同步命令：`uv run python scripts/sync_langfuse_prompts.py --all`（`--list` 看差异，`--dry-run` 只预览）。

### 技能执行模型（v2，2026-09 重构）

```
规划: 命中技能 → 该技能只生成 1 个任务(标注 skill_id) + 系统注入 skill_probe(前置校验)
执行: load_skill(整份 SKILL.md + reference 文档 + 文件清单 + 错误规则)
      → sandbox_create(环境校验 + 建沙箱 + 整目录同步)   ← 环境就绪后才有后续
      → sandbox_run(命令1: 取编码) → sandbox_run(命令2: 查数据) → …（同一沙箱，状态连续）
      → 汇总结果写回任务 → sandbox_close(显式销毁；未关则由空闲 TTL 回收)
```

- 为什么去掉 `run_skill_step` / `skill_step_detail`：步骤级工具把技能锁死成框架 schema
  （args_required/args_hint/step 编号），参数与顺序一旦偏离就空跑或漏传；改为"读文档 + 自己决定命令序列"后，
  技能作者只要把流程写清楚，模型就能按实际情况调整参数与重试；
- 沙箱会话为什么按 skill 复用：多步脚本之间有中间状态（编码、下载的数据），一步一沙箱会全部丢失；
- 资源边界：`skills.sandbox.session_ttl_seconds`（空闲回收，默认 1800s）、`max_sessions`（并发上限，默认 4）、
  应用关闭时 `aclose_all_sessions()` 兜底；命令超时会直接回收该会话（状态不可信）。

### 待接入（P0 剩余 → P1）
- 执行节点产物登记：失败时将产生的临时产物登记进 `ThreadState.artifacts`（SubTask/ThreadState 扩展字段）。
- P1 人工介入：`langgraph.interrupt()` 清理确认（产物清单 + 全部删除/保留）+ `POST /sessions/{id}/resume`
  + 前端确认卡片 + cleanup 决策打点审计。
- skill 使用打点（page=skill）+ 监控页 skill 面板（P2 候选匹配升级）。
