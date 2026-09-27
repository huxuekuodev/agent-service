"""一次运行的"身份"：让每条评估结果可归因、可对比。

评估最常见的失效方式是"只有分数、没有身份"——不知道这条分属于哪次对话、哪个任务、
哪个 prompt 版本、哪个模型，于是既不能定位 bad case，也无法证明改动有效。

这里用 ``ContextVar`` 携带一次请求的身份（与 trace_id 同思路：贯穿整个异步调用链）：

    router（会话/消息/渠道）→ 节点（模型角色/prompt 版本/plan_id/task_id/skill_id）→ 评估器（judge 模型）

另外记录 **代码版本**（git short sha），这样"这次改动有没有用"可以按版本对比。
"""

from __future__ import annotations

import subprocess
from contextvars import ContextVar, Token
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from typing import Any

__all__ = ["RunMeta", "code_version", "current_meta", "new_run_id", "reset_meta", "update_meta"]

run_meta_ctx_var: ContextVar[RunMeta | None] = ContextVar("run_meta", default=None)


@lru_cache(maxsize=1)
def code_version() -> str:
    """当前代码版本（git short sha）；非 git 环境返回空串（不影响评估）。"""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=3)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


@dataclass
class RunMeta:
    """一次运行的完整身份（缺省值即"未知"，不影响评估执行）。"""

    # ---- 归属（由路由层写入）----
    user_id: str = ""
    session_id: str = ""
    thread_id: str = ""
    message_id: int | None = None
    """本轮用户消息在 messages 表里的 id（落库后回填）。"""
    channel: str = "chat"
    """chat / voice / api。"""

    # ---- 评估链（一次用户回合 = 一条链，三个触发点共享）----
    run_id: str = ""
    """评估链 id：规划完成 / 每种任务完成 / 最终回复 三次触发共享，便于看"完整链条"。"""
    eval_enabled: bool = True
    """本次运行是否纳入评估（在**调用 agent 的那一层**按阈值/采样一次性决定）。"""
    eval_reason: str = ""
    """为什么评/不评（写入记录，便于事后核对阈值是否合理）。"""
    triggers: dict[str, bool] = field(default_factory=lambda: {"plan_done": True, "task_done": True, "final_answer": True})
    """各触发点开关（测试阶段全开）。"""

    # ---- 被评对象（由节点写入）----
    node: str = ""
    """plan_node / general_agent。"""
    plan_id: str = ""
    task_id: str = ""
    skill_id: str = ""
    run_model: str = ""
    """被评产出所用的模型角色。"""
    run_prompt_version: str = ""
    """被评产出所用的 prompt 版本（langfuse:v3 / local:ab12cd34）。"""

    # ---- 评估器（由评估器写入）----
    judge_model: str = ""
    judge_prompt_version: str = ""

    # ---- 代码版本 ----
    git_sha: str = field(default_factory=code_version)

    # ---- 运行信号（策略判定用，见 policy.py）----
    signals: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def merged(self, **fields: Any) -> RunMeta:
        """返回叠加了字段的新 meta（不修改原对象）。"""
        data = self.to_dict()
        data.update({k: v for k, v in fields.items() if v is not None})
        return RunMeta(**data)


def new_run_id() -> str:
    """生成一条评估链的 id（一次用户回合一个）。"""
    import uuid

    return uuid.uuid4().hex[:16]


def current_meta() -> RunMeta:
    """取当前运行的身份（无上下文时返回空身份，方便脚本/单测直接调用）。"""
    return run_meta_ctx_var.get() or RunMeta()


def update_meta(**fields: Any) -> Token:
    """写入/更新当前身份；返回 token 以便 finally 里 reset。

    用法::

        token = update_meta(session_id=sid, node="plan_node", run_model="default")
        try:
            ...
        finally:
            reset_meta(token)
    """
    meta = run_meta_ctx_var.get() or RunMeta()
    return run_meta_ctx_var.set(meta.merged(**fields))


def reset_meta(token: Token) -> None:
    """恢复上一层的身份（与 update_meta 成对使用）。"""
    try:
        run_meta_ctx_var.reset(token)
    except ValueError:
        # 跨上下文 reset（如后台任务里）时忽略：不影响功能
        pass
