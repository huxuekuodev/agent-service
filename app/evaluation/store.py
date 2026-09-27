"""评估记录存储：业务库 ``evaluations`` / ``eval_samples``（复用业务库连接池）。

只做 SQL；业务逻辑在 :mod:`app.evaluation.recorder`。表结构见 ``deploy/sql/business_schema.sql``：

    evaluations   每条指标一行：带会话/消息/任务/技能/模型/prompt 版本/代码版本
    eval_samples  改进原料：低分样本完整上下文（可回放）+ 用户显式反馈
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "insert_evaluations",
    "insert_sample",
    "list_bad_cases",
    "list_feedback",
    "summary_by_dimension",
    "trend_by_metric",
]

#: 允许的切片维度（防止把列名拼进 SQL 造成的注入）
_DIMENSIONS = {
    "evaluator": "evaluator",
    "metric": "metric",
    "run_prompt_version": "run_prompt_version",
    "judge_model": "judge_model",
    "run_model": "run_model",
    "skill_id": "skill_id",
    "node": "node",
    "channel": "channel",
    "git_sha": "git_sha",
}


async def _pool():
    from app.session import store as session_store

    return await session_store.get_pool()


async def insert_evaluations(rows: list[dict[str, Any]]) -> int:
    """批量写入评估明细（每条指标一行），返回写入行数。"""
    if not rows:
        return 0
    pool = await _pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            for row in rows:
                await cur.execute(
                    "INSERT INTO evaluations (trace_id, session_id, message_id, node, evaluator, metric, score, passed, rationale, "
                    "plan_id, task_id, skill_id, run_model, run_prompt_version, judge_model, judge_prompt_version, git_sha, channel, run_id, trigger, meta) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
                    (
                        row.get("trace_id", ""),
                        row.get("session_id") or None,
                        row.get("message_id"),
                        row.get("node", ""),
                        row.get("evaluator", ""),
                        row.get("metric", ""),
                        row.get("score"),
                        row.get("passed"),
                        row.get("rationale", "") or "",
                        row.get("plan_id", "") or "",
                        row.get("task_id", "") or "",
                        row.get("skill_id", "") or "",
                        row.get("run_model", "") or "",
                        row.get("run_prompt_version", "") or "",
                        row.get("judge_model", "") or "",
                        row.get("judge_prompt_version", "") or "",
                        row.get("git_sha", "") or "",
                        row.get("channel", "chat") or "chat",
                        row.get("run_id", "") or "",
                        row.get("trigger", "") or "",
                        json.dumps(row.get("meta") or {}, ensure_ascii=False),
                    ),
                )
    return len(rows)


async def insert_sample(
    *,
    kind: str,
    label: str = "",
    metric: str = "",
    evaluator: str = "",
    score: float | None = None,
    session_id: str = "",
    message_id: int | None = None,
    trace_id: str = "",
    evaluation_id: int | None = None,
    input_payload: dict[str, Any] | None = None,
    output_payload: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> int:
    """写入一条样本（低分样本 / 用户反馈 / 黄金样本），返回 id。"""
    pool = await _pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO eval_samples (evaluation_id, session_id, message_id, trace_id, kind, label, metric, evaluator, score, input, output, context) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb) RETURNING id",
            (
                evaluation_id,
                session_id or None,
                message_id,
                trace_id or "",
                kind,
                label or "",
                metric or "",
                evaluator or "",
                score,
                json.dumps(input_payload or {}, ensure_ascii=False),
                json.dumps(output_payload or {}, ensure_ascii=False),
                json.dumps(context or {}, ensure_ascii=False),
            ),
        )
        row = await cur.fetchone()
    return int(row["id"])


async def summary_by_dimension(
    *,
    dimension: str = "run_prompt_version",
    metric: str = "",
    evaluator: str = "",
    start: str | None = None,
    end: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """按维度聚合：均值 / 通过率 / 样本数（用于"哪个版本/模型/技能更好"的对比）。"""
    column = _DIMENSIONS.get(dimension)
    if column is None:
        raise ValueError(f"不支持的切片维度: {dimension}（可用: {sorted(_DIMENSIONS)}）")

    sql = f"SELECT {column} AS dimension_value, metric, count(*) AS samples, round(avg(score)::numeric, 3) AS avg_score, round(avg(case when passed then 1 else 0 end)::numeric, 3) AS pass_rate FROM evaluations WHERE score IS NOT NULL"
    params: list[Any] = []
    if metric:
        sql += " AND metric = %s"
        params.append(metric)
    if evaluator:
        sql += " AND evaluator = %s"
        params.append(evaluator)
    if start:
        sql += " AND created_at >= %s::timestamptz"
        params.append(start)
    if end:
        sql += " AND created_at <= %s::timestamptz"
        params.append(end)
    sql += f" GROUP BY {column}, metric ORDER BY metric, samples DESC LIMIT %s"
    params.append(max(1, min(limit, 500)))

    pool = await _pool()
    async with pool.connection() as conn:
        cur = await conn.execute(sql, tuple(params))
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def trend_by_metric(
    *,
    metric: str = "",
    evaluator: str = "",
    dimension: str = "run_prompt_version",
    start: str | None = None,
    end: str | None = None,
) -> list[dict[str, Any]]:
    """按天 × 维度看趋势（用于"改动上线后指标有没有变好"）。"""
    column = _DIMENSIONS.get(dimension, "run_prompt_version")
    sql = f"SELECT to_char(date_trunc('day', created_at), 'YYYY-MM-DD') AS day, {column} AS dimension_value, count(*) AS samples, round(avg(score)::numeric, 3) AS avg_score FROM evaluations WHERE score IS NOT NULL"
    params: list[Any] = []
    if metric:
        sql += " AND metric = %s"
        params.append(metric)
    if evaluator:
        sql += " AND evaluator = %s"
        params.append(evaluator)
    if start:
        sql += " AND created_at >= %s::timestamptz"
        params.append(start)
    if end:
        sql += " AND created_at <= %s::timestamptz"
        params.append(end)
    sql += f" GROUP BY day, {column} ORDER BY day"
    pool = await _pool()
    async with pool.connection() as conn:
        cur = await conn.execute(sql, tuple(params))
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def list_runs(*, session_id: str = "", limit: int = 20) -> list[dict[str, Any]]:
    """按评估链（run_id）汇总：一次用户回合触发了哪些评估点、各指标均分。

    用于验证"触发即完整链条"：正常一次回合应能看到 plan_done / task_done / final_answer。
    """
    pool = await _pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT run_id, session_id, count(*) AS rows, count(DISTINCT trigger) AS triggers, "
            "array_agg(DISTINCT trigger) AS trigger_list, round(avg(score)::numeric, 3) AS avg_score, "
            "to_char(min(created_at), 'YYYY-MM-DD\"T\"HH24:MI:SS') AS started_at "
            "FROM evaluations WHERE (%s = '' OR session_id = %s::uuid) AND run_id <> '' "
            "GROUP BY run_id, session_id ORDER BY min(created_at) DESC LIMIT %s",
            (session_id, session_id, max(1, min(limit, 100))),
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def list_bad_cases(
    *,
    metric: str = "",
    evaluator: str = "",
    max_score: float = 3.0,
    limit: int = 20,
    start: str | None = None,
    end: str | None = None,
) -> list[dict[str, Any]]:
    """低分案例列表（优先取归档了完整上下文的样本，没有则回退到评估明细）。"""
    pool = await _pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT s.id, s.kind, s.label, s.metric, s.evaluator, s.score, s.session_id, s.message_id, s.trace_id, "
            "s.input, s.output, s.context, to_char(s.created_at, 'YYYY-MM-DD\"T\"HH24:MI:SS') AS created_at, e.rationale "
            "FROM eval_samples s LEFT JOIN evaluations e ON e.id = s.evaluation_id "
            "WHERE s.kind IN ('bad_case', 'feedback') AND (%s = '' OR s.metric = %s) AND (%s = '' OR s.evaluator = %s) "
            "ORDER BY s.created_at DESC LIMIT %s",
            (metric, metric, evaluator, evaluator, max(1, min(limit, 200))),
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def list_feedback(*, limit: int = 50) -> list[dict[str, Any]]:
    """用户显式反馈列表（👍/👎，最便宜的在线质量信号）。"""
    pool = await _pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, label, session_id, message_id, score, context, to_char(created_at, 'YYYY-MM-DD\"T\"HH24:MI:SS') AS created_at FROM eval_samples WHERE kind = 'feedback' ORDER BY created_at DESC LIMIT %s",
            (max(1, min(limit, 200)),),
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]
