"""Skill 数据模型：元信息 / SOP 步骤 / 错误规则 / 清理规则。

对应技能仓库（config.yaml ``skills.dir``）下每个技能目录：

    skills/<skill_id>/
    ├── SKILL.md      # frontmatter(name/description/when_to_use/requires_tools/sop/errors/cleanup)
    ├── SOP.md        # 标准作业流程：## step-xx + YAML 元数据块，正文即步骤说明
    ├── errors.yaml   # 错误规则列表（YAML）：code/condition/action/recovery/note
    ├── cleanup.yaml  # 清理规则列表（YAML）：path/kind/level/note
    └── scripts/      # skill 自带的脚本（沙箱内运行，不落地本地环境）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "SkillMeta",
    "SopStep",
    "ErrorRule",
    "CleanupRule",
    "SkillContext",
]

#: 清理等级：auto=自动删 / confirm=人工确认后删 / forbidden=审计保留不删
CLEANUP_AUTO = "auto"
CLEANUP_CONFIRM = "confirm"
CLEANUP_FORBIDDEN = "forbidden"


@dataclass(frozen=True)
class SkillMeta:
    """SKILL.md frontmatter 解析结果 + 目录定位。"""

    id: str
    """技能唯一名（= 目录名）。"""
    dir: Path
    """技能目录（绝对路径）。"""
    name: str = ""
    """展示名。"""
    description: str = ""
    """能力说明（注册表索引用）。"""
    when_to_use: str = ""
    """何时使用（注册表索引 / 规划节点判定用）。"""
    requires_tools: list[str] = field(default_factory=list)
    """依赖的工具名（注册表校验执行侧是否可用）。"""
    sop_file: str = "SOP.md"
    errors_file: str = "errors.yaml"
    cleanup_file: str = "cleanup.yaml"
    has_scripts: bool = False
    """是否自带 scripts/（决定脚本需沙箱执行）。"""

    @property
    def scripts_dir(self) -> Path:
        return self.dir / "scripts"


@dataclass(frozen=True)
class SopStep:
    """SOP 中的一步（对应一个原子子任务）。"""

    step: str
    """步骤编号（如 step-01）。"""
    title: str = ""
    description: str = ""
    script: str = ""
    """若本步骤需要运行自带脚本：相对 scripts/ 的路径；脚本一律沙箱执行。"""
    tool: str = ""
    """若本步骤调用注册工具：工具名；工具调用在本地执行。"""
    args_required: bool = False
    """脚本是否必需命令行参数（run_skill_step 缺参时先提示再执行，避免空跑出 usage）。"""
    args_hint: str = ""
    """参数说明（供执行 agent 构造 arguments，如 `--province <省名> 或 --city <城市名>`）。"""


@dataclass(frozen=True)
class ErrorRule:
    """errors.md 中的一条错误规则。"""

    code: str
    """错误码。"""
    condition: str = ""
    """检测条件。"""
    action: str = "retry"
    """处置：retry / recovery / human。"""
    recovery: str = ""
    """恢复指引（action=recovery 时的恢复流程描述）。"""
    note: str = ""


@dataclass(frozen=True)
class CleanupRule:
    """cleanup.md 中的一条清理规则。"""

    path: str
    """产物路径（相对技能/执行上下文）。"""
    kind: str = "file"
    """file / dir / data / state。"""
    level: str = CLEANUP_AUTO
    """auto / confirm / forbidden。"""
    note: str = ""


@dataclass
class SkillContext:
    """加载后的完整 skill 上下文（执行节点/恢复流程使用）。"""

    meta: SkillMeta
    sop_steps: list[SopStep] = field(default_factory=list)
    error_rules: list[ErrorRule] = field(default_factory=list)
    cleanup_rules: list[CleanupRule] = field(default_factory=list)

    def sop_summary(self, max_steps: int = 12) -> str:
        """把 SOP 步骤大纲渲染成文本（供规划节点 / {skill_probe} 注入）。"""
        lines = [f"skill: {self.meta.id} - {self.meta.name}", self.meta.description]
        for step in self.sop_steps[:max_steps]:
            mode = f"[脚本:{step.script}]" if step.script else f"[工具:{step.tool}]" if step.tool else ""
            lines.append(f"- {step.step} {step.title} {mode}".rstrip())
        return "\n".join(lines)

    def error_index(self) -> str:
        """错误码索引文本（供 review 分支按错误码查询规则）。"""
        return "\n".join(f"- {r.code}: {r.condition} -> {r.action}" for r in self.error_rules)

    def cleanup_summary(self) -> str:
        """产物清理清单文本。"""
        return "\n".join(f"- {r.path} [{r.kind}] 等级={r.level} {r.note}".rstrip() for r in self.cleanup_rules)
