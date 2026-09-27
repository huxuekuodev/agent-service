"""沙箱工厂：按 ``skills.sandbox.backend`` 选择 Docker（默认）或 E2B。

统一接口（session.py 只认这三个）：

    await sandbox.aprepare(skill_dir, skill_id) -> (容器内技能目录, 上传文件数)
    await sandbox.arun_command(cmd, cwd=..., timeout=...) -> ScriptResult
    await sandbox.aclose()

- **docker**（默认）：一次性容器，宿主机缓存技能目录，根只读 + host 网络 + 超时 kill；
- **e2b**（可选回退）：云端沙箱，需装 ``e2b-code-interpreter`` 并配 ``E2B_*`` 环境变量。
"""

from __future__ import annotations

from typing import Any

from app.agents.skills.sandbox import SandboxConfig, SkillSandbox, SkillSandboxError, sandbox_available

__all__ = ["create_skill_sandbox", "sandbox_backend"]


def sandbox_backend() -> str:
    """当前沙箱后端名（docker / e2b）。"""
    return str(SandboxConfig.from_app_config().backend or "docker").lower()


async def create_skill_sandbox(**kwargs: Any) -> Any:
    """按配置创建沙箱（返回实现了统一接口的对象）。"""
    config = kwargs.pop("config", None) or SandboxConfig.from_app_config()
    ok, reason = sandbox_available()
    if not ok:
        raise SkillSandboxError(f"沙箱不可用：{reason}")
    if str(config.backend or "docker").lower() == "e2b":
        return await SkillSandbox.acreate(config=config, **kwargs)
    from app.agents.skills.docker_sandbox import DockerSkillSandbox

    return await DockerSkillSandbox.acreate(config=config, **kwargs)
