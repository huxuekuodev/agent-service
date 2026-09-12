"""技能沙箱会话与整技能加载单元测试（离线：假沙箱，不连 E2B）。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.agents.skills import session as session_module
from app.agents.skills.loader import load_skill_context_by_id
from app.agents.skills.sandbox import ScriptResult, SkillSandboxError
from app.agents.skills.session import SandboxSessionManager
from app.agents.skills.tools import make_skill_tools

SKILL_ID = "query-weather"


class FakeSandbox:
    """假沙箱：记录同步/执行/销毁，命令结果可脚本化。"""

    created: list[FakeSandbox] = []

    def __init__(self, *, fail_command: str = "", timeout_command: str = "") -> None:
        self.synced: list[str] = []
        self.commands: list[tuple[str, str | None]] = []
        self.closed = False
        self._fail_command = fail_command
        self._timeout_command = timeout_command
        FakeSandbox.created.append(self)

    def sync_dir(self, local_dir: Any, remote_root: str = "/home/user/skills") -> tuple[str, int]:
        self.synced.append(str(local_dir))
        from pathlib import Path

        files = [p for p in Path(local_dir).rglob("*") if p.is_file() and "__pycache__" not in p.as_posix() and not p.name.endswith(".pyc")]
        return (f"{remote_root}/{Path(local_dir).name}", len(files))

    async def arun_command(self, command: str, *, cwd: str | None = None, timeout: float | None = None) -> ScriptResult:
        self.commands.append((command, cwd))
        if command == self._timeout_command and self._timeout_command:
            return ScriptResult(stdout="", stderr="timeout", exit_code=-1, timed_out=True)
        if command == self._fail_command and self._fail_command:
            return ScriptResult(stdout="", stderr="boom", exit_code=2)
        return ScriptResult(stdout=f"ok:{command}", stderr="", exit_code=0)

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_fakes() -> None:
    FakeSandbox.created = []


def _factory(**kwargs: Any):
    async def _create() -> FakeSandbox:
        return FakeSandbox(**kwargs)

    return _create


# --------------------------------------------------------------------------- 会话管理


def test_session_reuses_sandbox_across_runs() -> None:
    async def scenario() -> None:
        manager = SandboxSessionManager(sandbox_factory=_factory(), ttl_seconds=900, max_sessions=4)
        session = await manager.open(SKILL_ID)
        assert session.remote_dir.endswith(SKILL_ID)
        assert session.files > 0

        first = await manager.open(SKILL_ID)
        assert first is session, "同一技能必须复用同一个沙箱会话"
        assert len(FakeSandbox.created) == 1

        _, r1 = await manager.run(SKILL_ID, "python3 scripts/get_city_code.py --city 石家庄")
        _, r2 = await manager.run(SKILL_ID, "python3 scripts/get_city_weather.py 101090101")
        assert r1.ok and r2.ok
        assert session.commands == 2
        assert [c[0] for c in session.sandbox.commands][0].endswith("--city 石家庄")
        # 命令默认在技能目录里执行
        assert all(cwd == session.remote_dir for _, cwd in session.sandbox.commands)

        await manager.close(SKILL_ID)
        assert session.sandbox.closed is True
        assert manager.list_sessions() == []

    asyncio.run(scenario())


def test_session_close_all_and_list() -> None:
    async def scenario() -> None:
        manager = SandboxSessionManager(sandbox_factory=_factory(), ttl_seconds=900, max_sessions=4)
        await manager.open(SKILL_ID)
        listed = manager.list_sessions()
        assert listed and listed[0]["skill_id"] == SKILL_ID and listed[0]["commands"] == 0
        assert await manager.close_all() == 1
        assert await manager.close_all() == 0

    asyncio.run(scenario())


def test_session_recreate_rebuilds_sandbox() -> None:
    async def scenario() -> None:
        manager = SandboxSessionManager(sandbox_factory=_factory(), ttl_seconds=900, max_sessions=4)
        first = await manager.open(SKILL_ID)
        second = await manager.open(SKILL_ID, recreate=True)
        assert first is not second
        assert first.sandbox.closed is True
        assert len(FakeSandbox.created) == 2

    asyncio.run(scenario())


def test_session_capacity_evicts_oldest_when_full(monkeypatch: pytest.MonkeyPatch) -> None:
    """容量满时（cap=1）开新技能会话，应回收最久未用的那个。"""

    async def scenario() -> None:
        real_loader = session_module.load_skill_context_by_id
        ctx = await real_loader(SKILL_ID)
        assert ctx is not None

        async def fake_loader(skill_id: str):
            return ctx if skill_id == "other-skill" else await real_loader(skill_id)

        monkeypatch.setattr(session_module, "load_skill_context_by_id", fake_loader)

        manager = SandboxSessionManager(sandbox_factory=_factory(), ttl_seconds=900, max_sessions=1)
        first = await manager.open(SKILL_ID)
        first.last_used_at = 0  # 让它成为最久未用者
        second = await manager.open("other-skill")

        assert first.sandbox.closed is True
        assert second.skill_id == "other-skill"
        assert [s["skill_id"] for s in manager.list_sessions()] == ["other-skill"]

    asyncio.run(scenario())


def test_session_ttl_expiry_rebuilds_on_next_use() -> None:
    async def scenario() -> None:
        manager = SandboxSessionManager(sandbox_factory=_factory(), ttl_seconds=60, max_sessions=4)
        session = await manager.open(SKILL_ID)
        session.last_used_at -= 3600  # 模拟长时间空闲
        with pytest.raises(SkillSandboxError) as exc:
            await manager.run(SKILL_ID, "python3 x.py")
        assert "空闲超时" in str(exc.value)
        assert session.sandbox.closed is True
        assert manager.list_sessions() == []

    asyncio.run(scenario())


def test_timeout_command_recycles_session() -> None:
    async def scenario() -> None:
        manager = SandboxSessionManager(sandbox_factory=_factory(timeout_command="sleep 999"), ttl_seconds=900, max_sessions=4)
        session, result = await manager.run(SKILL_ID, "sleep 999")
        assert result.timed_out is True
        assert session.sandbox.closed is True, "超时的沙箱状态不可信，必须回收"
        assert manager.list_sessions() == []

    asyncio.run(scenario())


def test_run_without_auto_open_is_rejected() -> None:
    async def scenario() -> None:
        manager = SandboxSessionManager(sandbox_factory=_factory(), ttl_seconds=900, max_sessions=4)
        with pytest.raises(SkillSandboxError) as exc:
            await manager.run(SKILL_ID, "ls", auto_open=False)
        assert "sandbox_create" in str(exc.value)

    asyncio.run(scenario())


def test_unknown_skill_rejected() -> None:
    async def scenario() -> None:
        manager = SandboxSessionManager(sandbox_factory=_factory(), ttl_seconds=900, max_sessions=4)
        with pytest.raises(SkillSandboxError):
            await manager.open("no-such-skill")

    asyncio.run(scenario())


# --------------------------------------------------------------------------- 整技能加载


def test_load_skill_returns_whole_bundle_without_steps() -> None:
    async def scenario() -> None:
        ctx = await load_skill_context_by_id(SKILL_ID)
        assert ctx is not None
        # 整份 SKILL.md（不是步骤列表）
        assert "执行流程" in ctx.skill_md
        assert not hasattr(ctx, "sop_steps"), "技能不再拆分为步骤"
        # 脚本与参考文档都在清单里；参考文档内容已内联
        assert "scripts/get_city_code.py" in ctx.scripts
        assert "reference/get_city_code.md" in [f.path for f in ctx.files]
        bundle = ctx.bundle()
        assert "SKILL.md" in bundle
        assert "reference/get_city_weather.md" in bundle
        # 规则来自 YAML
        assert any(r.code == "SANDBOX_UNAVAILABLE" for r in ctx.error_rules)
        assert ctx.cleanup_rules

    asyncio.run(scenario())


def test_load_skill_unknown_returns_none() -> None:
    assert asyncio.run(load_skill_context_by_id("no-such-skill")) is None


def test_tool_chain_exposes_sandbox_lifecycle_tools() -> None:
    names = {t.name for t in make_skill_tools()}
    assert names == {"list_skills", "load_skill", "sandbox_create", "sandbox_run", "sandbox_close", "sandbox_list", "query_error"}
    # 步骤级工具已移除
    assert "run_skill_step" not in names
    assert "skill_step_detail" not in names
