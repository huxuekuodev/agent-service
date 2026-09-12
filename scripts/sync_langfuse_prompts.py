"""把 app/prompts/ 下的提示词同步到 Langfuse（运行时唯一来源）。

背景：节点运行时通过 ``langfuse.get_prompt(<name>)`` 取提示词，本地文件只是**备份副本**；
改了本地文件但不推送，线上行为不会变（这也是"改了提示词没生效"的常见原因）。

用法::

    uv run python scripts/sync_langfuse_prompts.py --list            # 列出映射与本地/线上差异
    uv run python scripts/sync_langfuse_prompts.py plan             # 只推送规划节点提示词
    uv run python scripts/sync_langfuse_prompts.py --all             # 推送全部
    uv run python scripts/sync_langfuse_prompts.py plan --dry-run     # 只看差异不推送
    uv run python scripts/sync_langfuse_prompts.py plan --label production

说明：
  - 每次推送在 Langfuse 生成**新版本**（历史版本保留，可回滚）；
  - ``{{变量}}`` 占位符由 Langfuse 自动识别为变量（与节点 compile 时的入参对应）；
  - 线上没有的提示词会新建；已存在则追加版本并（可选）打标签。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMPT_DIR = PROJECT_ROOT / "app" / "prompts"


@dataclass(frozen=True)
class PromptSpec:
    """本地文件 → Langfuse 提示词名的映射。"""

    key: str
    """命令行用的简称。"""
    file: str
    """本地备份文件名（app/prompts/ 下）。"""
    name: str
    """Langfuse 中的提示词名（节点代码里 get_prompt 用的名字）。"""
    used_by: str
    """使用方（便于判断改动影响面）。"""


SPECS: tuple[PromptSpec, ...] = (
    PromptSpec("plan", "plan_system_prompt_v2.md", "plan_node_system_prompt", "规划节点 plan_model_node"),
    PromptSpec("general", "general_agent_system_prompt.md", "general_agent_system_prompt", "执行节点 general_agent（当前回退本地文件）"),
    PromptSpec("plan_eval", "plan_evaluator_prompt.md", "plan_evaluator_prompt", "规划评估器 PlanEvaluator"),
    PromptSpec("general_eval", "general_evaluator_prompt.md", "general_evaluator_prompt", "执行评估器 GeneralEvaluator"),
)


def _read_local(spec: PromptSpec) -> str:
    path = PROMPT_DIR / spec.file
    if not path.exists():
        raise FileNotFoundError(f"本地提示词文件不存在: {path}")
    return path.read_text(encoding="utf-8")


def _fetch_remote(client, name: str) -> tuple[str, int, list[str]] | None:
    """取线上 production 版本内容；不存在返回 None。"""
    from langfuse.api import NotFoundError

    try:
        prompt = client.get_prompt(name, type="text", cache_ttl_seconds=0)
    except NotFoundError:
        return None
    return prompt.prompt, int(getattr(prompt, "version", 0) or 0), list(getattr(prompt, "labels", []) or [])


def _summarize_diff(local: str, remote: str) -> str:
    """粗略差异摘要（行数 + 首处不同行），足够判断是否需要推送。"""
    local_lines, remote_lines = local.splitlines(), remote.splitlines()
    for i, (a, b) in enumerate(zip(local_lines, remote_lines), start=1):
        if a != b:
            return f"第 {i} 行起不同（本地 {len(local_lines)} 行 / 线上 {len(remote_lines)} 行）"
    if len(local_lines) != len(remote_lines):
        return f"行数不同（本地 {len(local_lines)} 行 / 线上 {len(remote_lines)} 行）"
    return "内容一致"


def main() -> int:
    parser = argparse.ArgumentParser(description="同步本地提示词到 Langfuse")
    parser.add_argument("keys", nargs="*", help="要同步的提示词简称（plan/general/plan_eval/general_eval）")
    parser.add_argument("--all", action="store_true", help="同步全部提示词")
    parser.add_argument("--list", action="store_true", help="只列出映射与差异，不推送")
    parser.add_argument("--dry-run", action="store_true", help="只显示将要推送的内容摘要，不推送")
    parser.add_argument("--label", default="production", help="推送后打的标签（默认 production）")
    args = parser.parse_args()

    from langfuse import Langfuse

    client = Langfuse()
    specs = list(SPECS)
    if args.keys:
        wanted = {k.strip() for k in args.keys}
        specs = [s for s in specs if s.key in wanted]
        missing = wanted - {s.key for s in specs}
        if missing:
            print(f"未知提示词简称: {sorted(missing)}；可用: {[s.key for s in SPECS]}", file=sys.stderr)
            return 2
    elif not args.all and not args.list:
        parser.print_help()
        return 0

    exit_code = 0
    for spec in specs:
        local = _read_local(spec)
        remote = _fetch_remote(client, spec.name)
        if remote is None:
            status = "线上不存在（将新建）"
        else:
            status = f"{_summarize_diff(local, remote[0])}（线上 v{remote[1]} labels={remote[2]}）"
        print(f"[{spec.key}] {spec.name} <- {spec.file}\n    使用方: {spec.used_by}\n    状态: {status}")

        if args.list or args.dry_run:
            continue
        try:
            client.create_prompt(
                name=spec.name,
                prompt=local,
                labels=[args.label],
                type="text",
                tags=["deer-agent", spec.file],
                commit_message=f"sync from {spec.file}",
            )
            client.flush()
        except Exception as exc:
            print(f"    ❌ 推送失败: {type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
            exit_code = 1
            continue

        after = _fetch_remote(client, spec.name)
        version = after[1] if after else "?"
        labels = after[2] if after else []
        print(f"    ✅ 已推送新版本 v{version}（labels={labels}）")

    client.flush()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
