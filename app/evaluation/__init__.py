"""评估闭环：身份 / 策略 / 存储 / 记录（见 docs/评估闭环方案.md）。

    context.py   一次运行的"身份"（RunMeta）：会话、消息、任务、技能、模型、prompt 版本、代码版本
    policy.py    "值得评才评"的判定（把评估成本花在有信息量的轮次上）
    store.py     业务库读写：evaluations（评估明细）+ eval_samples（低分样本/用户反馈）
    recorder.py  统一记录出口：落库 + 打点 + 后台异步执行

设计要点：评估结果必须能回答"**这条分属于哪次对话、哪个任务、哪个 prompt 版本、哪个模型**"，
否则只能看平均分，无法据此做针对性升级。
"""

from app.evaluation.context import RunMeta, current_meta, reset_meta, update_meta
from app.evaluation.policy import EvaluationDecision, decide
from app.evaluation.recorder import aclose_recorder, record_evaluation, register_sink

__all__ = [
    "EvaluationDecision",
    "RunMeta",
    "aclose_recorder",
    "current_meta",
    "decide",
    "record_evaluation",
    "register_sink",
    "reset_meta",
    "update_meta",
]
