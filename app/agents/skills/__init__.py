"""SKILL 能力包：标准 SOP 技能库 + 沙箱执行。

    registry.py   扫描 skills/ 仓库，解析 SKILL.md frontmatter，校验工具依赖
    loader.py     加载 SOP.md / errors.md / cleanup.md → 数据模型
    sandbox.py    E2B 代码沙箱（skill 自带脚本的隔离执行环境）
    tools.py      执行 agent 侧技能工具链（list/load/run/query_error）

执行模型：skill 自带脚本 → E2B 沙箱；注册工具 → 本地执行（见 docs/SKILL_方案.md）。
"""

from app.agents.skills import registry
from app.agents.skills.loader import load_skill_context, load_skill_context_by_id
from app.agents.skills.models import CleanupRule, ErrorRule, SkillContext, SkillMeta, SopStep
from app.agents.skills.registry import find_skill, is_skills_enabled, scan_skills, skill_index_text, validate_skill
from app.agents.skills.sandbox import SandboxConfig, ScriptResult, SkillSandbox, SkillSandboxError, sandbox_available
from app.agents.skills.tools import make_skill_tools

__all__ = [
    # 数据模型
    "SkillMeta",
    "SopStep",
    "ErrorRule",
    "CleanupRule",
    "SkillContext",
    # 注册表 / 加载
    "scan_skills",
    "find_skill",
    "validate_skill",
    "skill_index_text",
    "is_skills_enabled",
    "load_skill_context",
    "load_skill_context_by_id",
    # 沙箱
    "SandboxConfig",
    "ScriptResult",
    "SkillSandbox",
    "SkillSandboxError",
    "sandbox_available",
    # 工具链
    "make_skill_tools",
    "registry",
]
