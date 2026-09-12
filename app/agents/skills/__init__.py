"""SKILL 能力包：标准技能库 + 沙箱环境编排。

    registry.py   扫描 skills/ 仓库，解析 SKILL.md frontmatter，校验工具依赖
    loader.py     加载整份 SKILL.md + 文件清单 + errors/cleanup 规则（不拆分步骤）
    sandbox.py    E2B 代码沙箱（一个沙箱：传文件 / 跑命令 / 销毁）
    session.py    沙箱会话管理（按 skill 复用、空闲 TTL 回收、并发串行化）
    tools.py      执行 agent 侧技能工具链（list/load/create/run/close/query_error）

执行模型：**一个 skill 就是一个完整执行单元**——执行 LLM 读完整份 SKILL.md，
再准备沙箱并按自己的判断依次执行脚本，最后汇总结果。技能自带脚本 → E2B 沙箱；
注册工具 → 本地执行（见 docs/SKILL_方案.md）。
"""

from app.agents.skills import registry
from app.agents.skills.loader import load_skill_context, load_skill_context_by_id, read_skill_file
from app.agents.skills.models import CleanupRule, ErrorRule, SkillContext, SkillFile, SkillMeta
from app.agents.skills.registry import find_skill, is_skills_enabled, scan_skills, skill_index_text, validate_skill
from app.agents.skills.sandbox import SandboxConfig, ScriptResult, SkillSandbox, SkillSandboxError, sandbox_available
from app.agents.skills.session import SandboxSession, aclose_all_sessions, get_session_manager
from app.agents.skills.tools import make_skill_tools

__all__ = [
    # 数据模型
    "SkillMeta",
    "SkillFile",
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
    "read_skill_file",
    # 沙箱
    "SandboxConfig",
    "ScriptResult",
    "SkillSandbox",
    "SkillSandboxError",
    "sandbox_available",
    "SandboxSession",
    "get_session_manager",
    "aclose_all_sessions",
    # 工具链
    "make_skill_tools",
    "registry",
]
