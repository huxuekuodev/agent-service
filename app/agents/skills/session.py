"""沙箱会话管理：把"创建沙箱"与"执行命令"拆成两个显式动作。

执行模型（与 `docs/SKILL_方案.md` 一致）：

    1. ``sandbox_create(skill_id)``  预先创建沙箱：环境校验 → 建沙箱 → 把技能目录整体
                                      （scripts/ data/ reference/ …）同步进去 → 报"环境就绪"
    2. ``sandbox_run(skill_id, cmd)`` 在同一沙箱里执行命令（脚本/查看输出/自检），
                                       一个技能的多次调用**复用同一个沙箱**
    3. ``sandbox_close(skill_id)``    显式销毁；未显式关闭时按空闲 TTL 自动回收

为什么要有会话层：一个 skill 往往要跑多步命令（先取编码再取数据），
每次命令都新建/销毁沙箱既慢又丢状态（临时文件、已下载数据都没了）。
会话按 ``skill_id`` 复用，并用 ``asyncio.Lock`` 串行化同一沙箱上的命令
（并发执行相互踩文件系统会得到难以复现的失败）。

安全与资源：
  - 只上传技能目录，且沿用 ``SkillSandbox`` 的敏感文件过滤（.env / 私钥 / .git / 缓存）；
  - 空闲超过 ``skills.sandbox.session_ttl_seconds`` 自动销毁；同时存活数受
    ``skills.sandbox.max_sessions`` 限制（超出回收最久未用者）；
  - 应用关闭时 ``close_all()`` 兜底清理。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agents.skills.loader import load_skill_context_by_id
from app.agents.skills.sandbox import ScriptResult, SkillSandbox, SkillSandboxError, sandbox_available
from app.core.log import logger

__all__ = ["SandboxSession", "SandboxSessionManager", "get_session_manager", "aclose_all_sessions"]


@dataclass
class SandboxSession:
    """一个已就绪的沙箱会话（按 skill_id 复用）。"""

    skill_id: str
    sandbox: Any
    """底层 ``SkillSandbox``。"""
    remote_dir: str
    """技能目录在沙箱内的绝对路径（如 /home/user/skills/query-weather）。"""
    prepared_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    commands: int = 0
    """已执行命令数。"""
    files: int = 0
    """已同步文件数。"""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def idle_seconds(self) -> float:
        return max(0.0, time.time() - self.last_used_at)

    def to_dict(self) -> dict[str, Any]:
        """对外摘要（工具返回值 / 日志）。"""
        return {
            "skill_id": self.skill_id,
            "remote_dir": self.remote_dir,
            "files": self.files,
            "commands": self.commands,
            "idle_seconds": int(self.idle_seconds),
        }


class SandboxSessionManager:
    """沙箱会话注册表：创建、复用、回收。

    Args:
        sandbox_factory: 构造底层沙箱的工厂（测试可注入假实现）；
            默认 ``SkillSandbox.acreate``（E2B 真实沙箱）。
        ttl_seconds: 空闲回收秒数；``None`` 时读 config（``skills.sandbox.session_ttl_seconds``）。
        max_sessions: 同时存活上限；``None`` 时读 config（``skills.sandbox.max_sessions``）。
    """

    def __init__(
        self,
        *,
        sandbox_factory: Any = None,
        ttl_seconds: int | None = None,
        max_sessions: int | None = None,
    ) -> None:
        self._factory = sandbox_factory or SkillSandbox.acreate
        self._sessions: dict[str, SandboxSession] = {}
        self._guard = asyncio.Lock()
        self._ttl_override = ttl_seconds
        self._max_override = max_sessions

    # ------------------------------------------------------------------ 配置

    def _config(self) -> tuple[int, int]:
        """(空闲 TTL 秒, 同时存活上限)。"""
        if self._ttl_override is not None and self._max_override is not None:
            return self._ttl_override, self._max_override
        try:
            from app.config import get_app_config

            sb = get_app_config().skills.sandbox
            ttl = self._ttl_override if self._ttl_override is not None else sb.session_ttl_seconds
            cap = self._max_override if self._max_override is not None else sb.max_sessions
            return int(ttl), int(cap)
        except Exception:
            return int(self._ttl_override or 1800), int(self._max_override or 4)

    # ------------------------------------------------------------------ 查询

    def list_sessions(self) -> list[dict[str, Any]]:
        """当前存活会话摘要（按最近使用时间倒序）。"""
        return [s.to_dict() for s in sorted(self._sessions.values(), key=lambda x: x.last_used_at, reverse=True)]

    def get(self, skill_id: str) -> SandboxSession | None:
        """取会话（不创建）。"""
        return self._sessions.get(skill_id)

    # ------------------------------------------------------------------ 生命周期

    async def open(self, skill_id: str, *, recreate: bool = False) -> SandboxSession:
        """创建（或复用）某技能的沙箱会话，并把技能目录同步进去。

        Args:
            skill_id: 技能 id。
            recreate: 为 True 时先销毁旧会话再新建（脚本/数据改动后强制刷新）。

        Raises:
            SkillSandboxError: 沙箱不可用（含修复指引）或技能不存在。
        """
        await self._reap_idle()
        async with self._guard:
            if recreate:
                await self._close_locked(skill_id)
            existing = self._sessions.get(skill_id)
            if existing is not None:
                existing.last_used_at = time.time()
                return existing

            ok, reason = sandbox_available()
            if not ok:
                raise SkillSandboxError(f"沙箱不可用：{reason}。请先完成沙箱配置（skills.sandbox / E2B_* 环境变量）再重试。")

            ctx = await load_skill_context_by_id(skill_id)
            if ctx is None:
                raise SkillSandboxError(f"技能不存在或缺少 SKILL.md: {skill_id}")

            await self._enforce_capacity()
            session = await self._create_session(ctx.meta.dir, skill_id)
            self._sessions[skill_id] = session
            logger.info("[skill-sandbox] 会话就绪: skill={} files={} dir={}", skill_id, session.files, session.remote_dir)
            return session

    async def _create_session(self, skill_dir: Path, skill_id: str) -> SandboxSession:
        """建沙箱 + 同步技能目录（不注册到表里，便于失败时直接销毁）。"""
        sandbox = await self._factory()
        try:
            remote_dir, uploaded = await asyncio.to_thread(sandbox.sync_dir, skill_dir)
        except Exception:
            await _safe_close(sandbox)
            raise
        return SandboxSession(skill_id=skill_id, sandbox=sandbox, remote_dir=remote_dir, files=uploaded)

    async def run(
        self,
        skill_id: str,
        command: str,
        *,
        cwd: str | None = None,
        timeout: float | None = None,
        auto_open: bool = True,
    ) -> tuple[SandboxSession, ScriptResult]:
        """在技能沙箱内执行一条命令（同一会话内串行，避免并发踩文件系统）。

        Args:
            command: 完整 shell 命令，如 ``python3 scripts/get_city_code.py --province 河北省``。
            cwd: 工作目录（默认技能目录，脚本里的相对路径按它解析）。
            timeout: 命令超时秒数（默认 config ``skills.sandbox.command_timeout``）。
            auto_open: 会话不存在时自动创建（错误提示里会说明，避免白跑一轮）。
        """
        session = self._sessions.get(skill_id)
        if session is None:
            if not auto_open:
                raise SkillSandboxError(f"技能 {skill_id} 尚未创建沙箱会话，请先调用 sandbox_create('{skill_id}') 准备环境。")
            session = await self.open(skill_id)

        await self._check_ttl(session)
        async with session.lock:
            result = await session.sandbox.arun_command(command, cwd=cwd or session.remote_dir, timeout=timeout)
            session.commands += 1
            session.last_used_at = time.time()
        if result.timed_out:
            # 超时的沙箱状态不可信（可能有残留进程），直接回收，下次命令重建
            logger.warning("[skill-sandbox] 命令超时，回收会话: skill={} cmd={}", skill_id, command[:120])
            await self.close(skill_id)
        return session, result

    async def close(self, skill_id: str) -> bool:
        """销毁某技能的沙箱会话；不存在返回 False。"""
        async with self._guard:
            return await self._close_locked(skill_id)

    async def _close_locked(self, skill_id: str) -> bool:
        session = self._sessions.pop(skill_id, None)
        if session is None:
            return False
        await _safe_close(session.sandbox)
        logger.info("[skill-sandbox] 会话已销毁: skill={} commands={}", skill_id, session.commands)
        return True

    async def close_all(self) -> int:
        """销毁全部会话（应用关闭 / 测试收尾）。"""
        async with self._guard:
            skill_ids = list(self._sessions)
            for skill_id in skill_ids:
                await self._close_locked(skill_id)
            return len(skill_ids)

    # ------------------------------------------------------------------ 回收策略

    async def _reap_idle(self) -> int:
        """回收空闲超时的会话。"""
        ttl, _ = self._config()
        expired = [sid for sid, s in self._sessions.items() if s.idle_seconds > ttl]
        for sid in expired:
            logger.info("[skill-sandbox] 空闲超时回收: skill={} idle={}s", sid, int(self._sessions[sid].idle_seconds))
            await self.close(sid)
        return len(expired)

    async def _check_ttl(self, session: SandboxSession) -> None:
        """使用前检查：若已超时空闲，则重建会话。"""
        ttl, _ = self._config()
        if session.idle_seconds > ttl:
            logger.info("[skill-sandbox] 会话空闲超时，重建: skill={}", session.skill_id)
            await self.close(session.skill_id)
            raise SkillSandboxError(f"技能 {session.skill_id} 的沙箱会话已因空闲超时销毁，请重新调用 sandbox_create 准备环境。")

    async def _enforce_capacity(self) -> None:
        """超出上限时回收最久未用的会话。"""
        _, cap = self._config()
        while len(self._sessions) >= cap:
            oldest = min(self._sessions.values(), key=lambda s: s.last_used_at)
            logger.info("[skill-sandbox] 会话数达上限 {}，回收最久未用: skill={}", cap, oldest.skill_id)
            await self._close_locked(oldest.skill_id)


async def _safe_close(sandbox: Any) -> None:
    """销毁底层沙箱，失败不抛出（回收是尽力而为）。"""
    try:
        await sandbox.aclose()
    except Exception as exc:  # pragma: no cover - 依赖远端行为
        logger.warning("[skill-sandbox] 沙箱销毁失败（忽略）: {}", exc)


_manager: SandboxSessionManager | None = None


def get_session_manager() -> SandboxSessionManager:
    """全局沙箱会话管理器（进程级单例）。"""
    global _manager
    if _manager is None:
        _manager = SandboxSessionManager()
    return _manager


async def aclose_all_sessions() -> int:
    """关闭全部沙箱会话（应用 shutdown 调用）。"""
    if _manager is None:
        return 0
    return await _manager.close_all()
