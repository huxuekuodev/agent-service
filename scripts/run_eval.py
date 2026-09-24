"""离线评测跑批：在固定样本上跑出"比基线好还是差"（见 docs/评估闭环方案.md）。

为什么需要它：在线评估只能告诉你"现在多少分"，改 prompt / 换模型后**无法证明变好了**。
跑批把评估变成可重复实验：

    基线（上次跑的 JSON）  vs  候选（这次跑的 JSON）  →  每指标均值/通过率/胜出样本

用法::

    # 正式模式：打开在线评估（同步），跑评测集，存报告
    uv run python scripts/run_eval.py --label candidate-2026-09-13 --limit 8

    # 与基线对比（自动挑最近一次同评测集的报告，或用 --baseline 指定）
    uv run python scripts/run_eval.py --label candidate-2 --baseline reports/eval-baseline-2026-09-13.json

    # 只跑 2 条做冒烟
    uv run python scripts/run_eval.py --smoke

说明：
  - 单进程内直接驱动图（不起服务、不需要登录），每个用例用独立 thread，互不污染；
  - 通过 ``app.evaluation.register_sink`` 直接收集评估记录，不依赖数据库；
  - 跑批期间强制 ``evaluation_policy.async_enabled=false`` + ``mode=always``，
    保证每条用例都被评估（否则会被"值得评才评"策略跳过，样本不足）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

REPORTS_DIR = PROJECT_ROOT / "reports"
DEFAULT_EVALSET = PROJECT_ROOT / "evalsets" / "core.yaml"


@dataclass
class CaseResult:
    """单条用例的结果。"""

    case_id: str
    tags: list[str] = field(default_factory=list)
    answer: str = ""
    latency_s: float = 0.0
    metrics: dict[str, float] = field(default_factory=dict)
    rationales: dict[str, str] = field(default_factory=dict)
    passed: bool | None = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "tags": self.tags,
            "answer": self.answer[:500],
            "latency_s": round(self.latency_s, 2),
            "metrics": self.metrics,
            "rationales": self.rationales,
            "passed": self.passed,
            "error": self.error,
        }


def _load_evalset(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cases = data.get("cases") or []
    if not isinstance(cases, list) or not cases:
        raise SystemExit(f"评测集为空或格式错误: {path}")
    return cases


def _force_always_policy() -> None:
    """跑批期间强制：全量评估 + 同步等待（保证样本完整）。"""
    from app.config import get_app_config
    from app.evaluation import recorder

    policy = get_app_config().evaluation_policy
    policy.mode = "always"
    policy.async_enabled = False
    recorder._semaphore = None  # 并发限制与同步模式无关，重置以便按新配置初始化


async def _run_case(case: dict[str, Any], collected: list[dict[str, Any]], *, turn_timeout: float) -> CaseResult:
    """跑一条用例：新 thread → 发问题（含 follow_ups）→ 收集评估记录。"""
    from app.agent_service import AgentService

    result = CaseResult(case_id=str(case.get("id", "?")), tags=list(case.get("tags") or []))
    questions = [str(case.get("question", ""))] + [str(q) for q in (case.get("follow_ups") or [])]
    started = time.perf_counter()

    async with AgentService() as svc:
        thread_id = f"eval-{result.case_id}-{int(time.time())}"
        answer = ""
        for question in questions:
            events: list[dict[str, Any]] = []
            try:
                async with asyncio.timeout(turn_timeout):
                    async for event in svc.stream(thread_id, question):
                        events.append(event)
                        data = event.get("data") or {}
                        if event.get("type") == "custom" and data.get("type") == "answer":
                            answer = str(data.get("content") or answer)
                        if event.get("type") == "interrupt":
                            # 计划确认中断：跑批时视为"直接同意"，继续把这一轮跑完
                            payload = data.get("payload") or {}
                            async for ev2 in svc.stream(thread_id, resume={"answers": []}):
                                d2 = ev2.get("data") or {}
                                events.append(ev2)
                                if ev2.get("type") == "custom" and d2.get("type") == "answer":
                                    answer = str(d2.get("content") or answer)
                            del payload
            except TimeoutError:
                result.error = f"超时（>{turn_timeout}s）"
                break
            except Exception as exc:
                result.error = f"{type(exc).__name__}: {exc}"[:200]
                break
        await svc.delete_thread(thread_id)

    result.answer = answer
    result.latency_s = time.perf_counter() - started

    # 收集本条用例的评估记录（合并同一指标多次评估的均值）
    per_metric: dict[str, list[float]] = {}
    for record in collected:
        for metric, score in (record.get("metric_scores") or {}).items():
            per_metric.setdefault(metric, []).append(float(score))
        if record.get("rationales"):
            result.rationales.update({k: str(v)[:300] for k, v in record["rationales"].items()})
        if record.get("passed") is not None:
            result.passed = bool(record["passed"]) if result.passed is None else (result.passed and bool(record["passed"]))
    result.metrics = {metric: round(statistics.fmean(scores), 3) for metric, scores in per_metric.items()}
    return result


def _summarize(results: list[CaseResult]) -> dict[str, Any]:
    """按指标汇总（均值 / 通过率 / 样本数），并按标签分组。"""
    metrics: dict[str, list[float]] = {}
    for r in results:
        for metric, score in r.metrics.items():
            metrics.setdefault(metric, []).append(score)

    summary = {
        "cases": len(results),
        "errors": sum(1 for r in results if r.error),
        "avg_latency_s": round(statistics.fmean([r.latency_s for r in results]), 2) if results else 0,
        "metrics": {metric: {"mean": round(statistics.fmean(scores), 3), "samples": len(scores)} for metric, scores in sorted(metrics.items())},
    }
    by_tag: dict[str, list[float]] = {}
    for r in results:
        for metric, score in r.metrics.items():
            for tag in r.tags or ["untagged"]:
                by_tag.setdefault(tag, []).append(score)
    summary["by_tag"] = {tag: round(statistics.fmean(scores), 3) for tag, scores in sorted(by_tag.items())}
    return summary


def _compare(baseline: dict[str, Any], candidate: dict[str, Any]) -> str:
    """生成基线 vs 候选的对比表（Markdown）。"""
    lines = ["| 指标 | 基线 | 候选 | 变化 |", "|------|------|------|------|"]
    base_metrics = (baseline.get("summary") or {}).get("metrics") or {}
    cand_metrics = (candidate.get("summary") or {}).get("metrics") or {}
    for metric in sorted(set(base_metrics) | set(cand_metrics)):
        b = base_metrics.get(metric, {}).get("mean")
        c = cand_metrics.get(metric, {}).get("mean")
        if b is None or c is None:
            lines.append(f"| {metric} | {b if b is not None else '-'} | {c if c is not None else '-'} | 新增/缺失 |")
            continue
        delta = round(float(c) - float(b), 3)
        mark = "🟢" if delta > 0.05 else ("🔴" if delta < -0.05 else "⚪")
        lines.append(f"| {metric} | {b} | {c} | {mark} {delta:+} |")
    return "\n".join(lines)


def _write_report(payload: dict[str, Any], label: str) -> Path:
    REPORTS_DIR.mkdir(exist_ok=True)
    path = REPORTS_DIR / f"eval-{label}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


async def _main_async(args: argparse.Namespace) -> int:
    from app.evaluation import register_sink

    cases = _load_evalset(Path(args.evalset))
    if args.smoke:
        cases = cases[:2]
    elif args.limit:
        cases = cases[: args.limit]

    _force_always_policy()

    # 收集器：按用例隔离（每条用例跑之前清空）
    collected: list[dict[str, Any]] = []
    unregister = register_sink(collected.append)

    print(f"评测集: {args.evalset}｜用例数: {len(cases)}｜标签: {args.label}")
    results: list[CaseResult] = []
    try:
        for i, case in enumerate(cases, 1):
            collected.clear()
            print(f"[{i}/{len(cases)}] {case.get('id')} …", end="", flush=True)
            result = await _run_case(case, collected, turn_timeout=args.timeout)
            results.append(result)
            scores = ", ".join(f"{k}={v}" for k, v in list(result.metrics.items())[:3]) or "无评估"
            print(f" {result.latency_s:.1f}s {scores}{' ⚠' + result.error if result.error else ''}")
    finally:
        unregister()

    payload = {
        "label": args.label,
        "evalset": str(Path(args.evalset).name),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "cases": [r.to_dict() for r in results],
        "summary": _summarize(results),
    }

    baseline_path = Path(args.baseline) if args.baseline else _latest_baseline(Path(args.evalset).name, exclude=args.label)
    if baseline_path and baseline_path.exists():
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        payload["baseline"] = {"file": baseline_path.name, "summary": baseline.get("summary")}
        payload["comparison_md"] = _compare(baseline, payload)
        print(f"\n对比基线 {baseline_path.name}:\n{payload['comparison_md']}")

    report = _write_report(payload, args.label)
    print(f"\n报告: {report}")
    print("汇总:", json.dumps(payload["summary"], ensure_ascii=False))
    return 0


def _latest_baseline(evalset_name: str, *, exclude: str) -> Path | None:
    """找同评测集最近一次报告作为基线。"""
    if not REPORTS_DIR.exists():
        return None
    candidates = sorted(REPORTS_DIR.glob("eval-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in candidates:
        if path.stem == f"eval-{exclude}":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("evalset") == evalset_name:
            return path
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="离线评测跑批（基线 vs 候选对比）")
    parser.add_argument("--evalset", default=str(DEFAULT_EVALSET), help="评测集 YAML 路径")
    parser.add_argument("--label", default=time.strftime("run-%Y%m%d-%H%M%S"), help="本次报告标签")
    parser.add_argument("--baseline", default="", help="基线报告 JSON（默认自动挑最近一次同评测集报告）")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条")
    parser.add_argument("--smoke", action="store_true", help="只跑 2 条（冒烟）")
    parser.add_argument("--timeout", type=float, default=300.0, help="单轮超时秒数")
    args = parser.parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
