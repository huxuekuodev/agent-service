"""评估闭环：身份 / 策略 / 存储 / 记录（见 docs/评估闭环方案.md）。

    context.py   一次运行的"身份"（RunMeta）：会话、消息、任务、技能、模型、prompt 版本、代码版本
    policy.py    链级决策（调用 agent 时定阈值/开关）+ 三个触发点的单一判定点
    store.py     业务库读写：evaluations（评估明细）+ eval_samples（低分样本/用户反馈）
    recorder.py  统一记录出口：落库 + 打点 + 后台异步执行

设计要点：评估结果必须能回答"**这条分属于哪次对话、哪个任务、哪个 prompt 版本、哪个模型**"，
否则只能看平均分，无法据此做针对性升级。
"""

from app.evaluation.context import RunMeta, current_meta, new_run_id, reset_meta, update_meta
from app.evaluation.policy import RunDecision, TriggerDecision, begin_run, decide_trigger
from app.evaluation.recorder import aclose_recorder, record_evaluation, register_sink

__all__ = [
    "RunDecision",
    "RunMeta",
    "TriggerDecision",
    "aclose_recorder",
    "begin_run",
    "current_meta",
    "decide_trigger",
    "new_run_id",
    "record_evaluation",
    "register_sink",
    "reset_meta",
    "update_meta",
]
