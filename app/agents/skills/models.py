"""Skill 数据模型：技能元信息 / 文件清单 / 错误规则 / 清理规则。

对应技能仓库（config.yaml ``skills.dir``）下每个技能目录：

    skills/<skill_id>/
    ├── SKILL.md      # 入口文档：frontmatter(name/description/when_to_use/requires_tools/errors/cleanup)
    │                 #   正文 = 完整执行流程（怎么跑、跑什么脚本、产出什么），由执行 LLM 整份读取
    ├── errors.yaml   # 错误规则列表（YAML）：code/condition/action/recovery/note
    ├── cleanup.yaml  # 清理规则列表（YAML）：path/kind/level/note
    ├── reference/    # 参考文档（脚本参数说明等，随技能一起进入沙箱）
    ├── data/         # 技能自带数据（随技能一起进入沙箱）
    └── scripts/      # 技能自带脚本（**只在 E2B 沙箱内运行**，不落地本地环境）

设计取向（**不拆分步骤**）：一个 skill 就是一个完整的执行单元——框架不再把技能解析成
step 列表，也不再有"执行某一步"的工具；执行 LLM 读完整份 ``SKILL.md`` 后，自行决定
在沙箱里依次运行哪些脚本/命令，并把结果汇总成任务结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "SkillMeta",
    "SkillFile",
    "ErrorRule",
    "CleanupRule",
    "SkillContext",
]

#: 清理等级：auto=自动删 / confirm=人工确认后删 / forbidden=审计保留不删
CLEANUP_AUTO = "auto"
CLEANUP_CONFIRM = "confirm"
CLEANUP_FORBIDDEN = "forbidden"

#: 可整体注入 LLM 上下文的文本文件后缀（脚本/数据文件只列清单，不进上下文）
_TEXT_SUFFIXES = frozenset({".md", ".yaml", ".yml", ".txt", ".json", ".csv"})


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
    """依赖的工具名（注册表校验执行侧是否可用）；这些工具在**本地**执行，不进沙箱。"""
    errors_file: str = "errors.yaml"
    cleanup_file: str = "cleanup.yaml"
    has_scripts: bool = False
    """是否自带 scripts/（决定是否需要 E2B 沙箱）。"""

    @property
    def scripts_dir(self) -> Path:
        return self.dir / "scripts"


@dataclass(frozen=True)
class SkillFile:
    """技能目录内的一个文件（清单项）。"""

    path: str
    """相对技能目录的路径（posix 风格），如 scripts/get_city_code.py。"""
    size: int = 0
    """字节数。"""
    text: str | None = None
    """文本内容（仅小体积文本文件内联；脚本/大文件为 None，只给清单）。"""

    @property
    def is_text(self) -> bool:
        return self.text is not None


@dataclass(frozen=True)
class ErrorRule:
    """errors.yaml 中的一条错误规则。"""

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
    """cleanup.yaml 中的一条清理规则。"""

    path: str
    """产物路径（相对技能/执行上下文）。"""
    kind: str = "file"
    """file / dir / data / state。"""
    level: str = CLEANUP_AUTO
    """auto / confirm / forbidden。"""
    note: str = ""


@dataclass
class SkillContext:
    """加载后的完整技能上下文（执行节点使用：整份 SKILL + 文件清单 + 规则）。"""

    meta: SkillMeta
    skill_md: str = ""
    """SKILL.md 正文（不含 frontmatter）：完整执行流程，交给 LLM 整份阅读。"""
    files: list[SkillFile] = field(default_factory=list)
    error_rules: list[ErrorRule] = field(default_factory=list)
    cleanup_rules: list[CleanupRule] = field(default_factory=list)

    @property
    def scripts(self) -> list[str]:
        """自带脚本相对路径列表（供提示词/沙箱准备展示）。"""
        return [f.path for f in self.files if f.path.startswith("scripts/") and f.path.endswith(".py")]

    def file_index(self) -> str:
        """文件清单文本（哪些文件随技能进沙箱、哪些是脚本）。"""
        if not self.files:
            return "（技能目录为空）"
        return "\n".join(f"- {f.path} ({f.size} B)" for f in self.files)

    def error_index(self) -> str:
        """错误码索引文本（供失败恢复 / 人工介入决策）。"""
        return "\n".join(f"- {r.code}: {r.condition} -> {r.action}" for r in self.error_rules)

    def cleanup_summary(self) -> str:
        """产物清理清单文本。"""
        return "\n".join(f"- {r.path} [{r.kind}] 等级={r.level} {r.note}".rstrip() for r in self.cleanup_rules)

    def bundle(self, *, max_chars: int = 0) -> str:
        """整份技能文本（SKILL.md + 内联文本文件），供 `load_skill` 一次性读取。

        Args:
            max_chars: 总字符上限（0 = 不截断）；超限时按文件顺序截断并标注。
        """
        parts: list[str] = [f"# SKILL.md（技能 {self.meta.id} 的完整执行流程）\n{self.skill_md.strip()}"]
        used = len(parts[0])
        for f in self.files:
            if f.text is None or f.path in ("SKILL.md",):
                continue
            block = f"\n\n# 文件 {f.path}\n{f.text.strip()}"
            if max_chars and used + len(block) > max_chars:
                parts.append(f"\n\n# 文件 {f.path}\n（内容较长已省略，已在沙箱内可用：cat {f.path}）")
                continue
            used += len(block)
            parts.append(block)
        return "".join(parts)


def is_text_path(path: str) -> bool:
    """是否适合内联进 LLM 上下文的文本文件。"""
    return Path(path).suffix.lower() in _TEXT_SUFFIXES
