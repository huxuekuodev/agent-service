"""E2B 代码沙箱：skill 自带脚本的隔离执行环境（升级版）。

设计规则（与 main.py 约定一致）：
  - **skill 自带的脚本**一律在 E2B 沙箱内运行（本地环境不落盘、不执行）；
  - **调用注册工具的步骤**仍在本地执行（工具由执行 agent 注入）；
  - 沙箱临时、隔离、按需销毁：脚本产生的临时文件随沙箱销毁自动回收，
    需要保留给用户检查的产物用 ``download_files`` 拉回本地再展示。

API 备忘（e2b_code_interpreter v1.x，继承自 e2b.Sandbox）:
    - Sandbox.create(template=..., timeout=..., envs={...})
    - sandbox.files.write(path, data) / sandbox.commands.run(cmd, cwd=..., envs=..., timeout=...)
    - sandbox.kill()

升级点（相对草稿）：
  - 配置驱动：template / 超时 / 输出上限 / 注入 env 白名单 / 跳过模式 来自 config.yaml skills.sandbox；
  - E2B_API_KEY / e2b 依赖缺失时给出明确错误而非模糊异常；
  - 同步目录自动过滤敏感文件（.env / 私钥 / .git / 缓存），控制文件数；
  - 脚本路径限制在已同步的远端根目录内（防越界）；输出截断 + 超时标记；
  - async 门面（asyncio.to_thread）与一次性执行便捷入口；
  - 产物拉回（download_files）供人工介入时检查。
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "SkillSandboxError",
    "ScriptResult",
    "SandboxConfig",
    "SkillSandbox",
    "sandbox_available",
    "run_skill_script_once",
]

_DEFAULT_REMOTE_ROOT = "/home/user/skills"


class SkillSandboxError(RuntimeError):
    """沙箱不可用 / 执行失败。"""


@dataclass(frozen=True)
class SandboxConfig:
    """沙箱运行参数（未显式传入时从 config.yaml skills.sandbox 读取）。"""

    template: str = ""
    timeout: int = 3600
    command_timeout: int = 120
    max_output_chars: int = 990000
    """单次执行输出回传 LLM 的最大字符数（0 = 不截断）。"""
    env_keys: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=lambda: [".env*", "*.pem", "*.key", "*.p12", ".git/*", "__pycache__/*", "*.pyc", ".venv/*", "node_modules/*"])
    max_files: int = 2000
    """单次同步文件数上限（防误传整棵树）。"""

    @classmethod
    def from_app_config(cls) -> SandboxConfig:
        try:
            from app.config import get_app_config

            sb = get_app_config().skills.sandbox
            return SandboxConfig(
                template=sb.template,
                timeout=sb.timeout,
                command_timeout=sb.command_timeout,
                max_output_chars=sb.max_output_chars,
                env_keys=list(sb.env_keys),
                exclude_patterns=list(sb.exclude_patterns),
            )
        except Exception:
            return cls()

    def resolved_envs(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """按白名单从宿主环境取 env + 合并调用方显式 env。"""
        envs = {k: os.getenv(k, "") for k in self.env_keys if os.getenv(k)}
        if extra:
            envs.update({k: v for k, v in extra.items() if v is not None})
        return envs


@dataclass
class ScriptResult:
    """一次脚本/命令执行的结果（供 LLM 读取为文本）。"""

    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int = 0
    timed_out: bool = False
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def text(self) -> str:
        """stdout/stderr 合并文本（失败时附退出码/超时信息）。"""
        parts = [self.stdout]
        if self.stderr:
            parts.append(f"[stderr]\n{self.stderr}")
        if not self.ok:
            parts.append(f"[exit_code={self.exit_code}{', timeout' if self.timed_out else ''}]")
        return "\n".join(parts)


def _import_e2b() -> Any:
    """惰性导入 e2b_code_interpreter；缺失时抛明确错误。"""
    try:
        from e2b_code_interpreter import Sandbox  # type: ignore
    except ImportError as exc:  # pragma: no cover - 环境依赖分支
        raise SkillSandboxError("E2B 沙箱不可用：缺少可选依赖 e2b-code-interpreter。请在宿主机执行 `uv sync --extra sandbox`（或 uv pip install 'e2b-code-interpreter>=1,<2'）后重试。") from exc
    return Sandbox


def sandbox_available() -> tuple[bool, str]:
    """沙箱是否可用：(可用?, 不可用原因，含修复指引)。"""
    from app.config import get_app_config

    sb = get_app_config().skills.sandbox
    if not sb.enabled:
        return False, "skills.sandbox.enabled=false（config.yaml，需改为 true）"
    if not sb.template:
        return False, "skills.sandbox.template 未配置（config.yaml，或设置环境变量 E2B_TEMPLATE）"
    if not os.getenv("E2B_API_KEY"):
        return False, "缺少 E2B_API_KEY 环境变量（写入 .env）"
    try:
        _import_e2b()
    except SkillSandboxError as exc:
        return False, str(exc)
    return True, ""


def _classify_sandbox_error(exc: Exception) -> str:
    """把 E2B SDK 原始异常分类为带修复指引的中文错误。"""
    text = f"{type(exc).__name__}: {exc}"
    low = str(exc).lower()
    if any(k in low for k in ("authentication", "unauthorized", "invalid api key", "api key", "401", "403", "forbidden", "permission")):
        return f"E2B 鉴权/权限失败（检查 .env 的 E2B_API_KEY 是否正确/未过期，或对模板无权限）: {text[:300]}"
    if any(k in low for k in ("domain", "proxy", "dns", "connect", "network", "ssl", "host", "connection")):
        return f"无法连接 E2B 沙箱服务（检查 .env 的 E2B_DOMAIN、网络/代理/防火墙是否可达）: {text[:300]}"
    if any(k in low for k in ("template", "not found", "404", "400", "bad request")):
        return f"E2B 沙箱模板无效（检查 skills.sandbox.template / E2B_TEMPLATE 是否存在于控制台且可用）: {text[:300]}"
    if any(k in low for k in ("timeout", "timed out")):
        return f"E2B 沙箱操作超时（可调大 skills.sandbox.timeout / command_timeout）: {text[:300]}"
    return f"沙箱异常（{text[:300]}）"


class SkillSandbox:
    """管理一个 E2B 沙箱：同步 skill 文件 + 执行脚本 / 代码 + 拉回产物。

    用法：:

        with SkillSandbox(template="...", envs={"QWEATHER_API_KEY": "..."}) as sb:
            remote = sb.sync_dir("skills/query_weather")
            res = sb.run_script(f"{remote}/scripts/get_city_code.py", args=["北京"])
            print(res.text)

    Sandbox 的 Python 是同步 API；对外提供 ``arun_*``/``async with`` 门面。
    """

    def __init__(
        self,
        *,
        template: str | None = None,
        timeout: int | None = None,
        envs: dict[str, str] | None = None,
        config: SandboxConfig | None = None,
    ) -> None:
        cfg = config or SandboxConfig.from_app_config()
        self._template = template or cfg.template or os.getenv("E2B_TEMPLATE", "")
        self._cfg = cfg
        if not self._template:
            raise SkillSandboxError("未配置 E2B 沙箱模板（config.yaml skills.sandbox.template 或环境变量 E2B_TEMPLATE）")
        if not os.getenv("E2B_API_KEY"):
            raise SkillSandboxError("缺少 E2B_API_KEY 环境变量，无法创建 E2B 沙箱（可写入 .env）")

        Sandbox = _import_e2b()
        api_key = os.getenv("E2B_API_KEY", "")
        domain = os.getenv("E2B_DOMAIN", "")
        logger.info("创建 E2B 沙箱: template=%s domain=%s", self._template, domain or "(默认)")
        create_kwargs: dict[str, Any] = {
            "template": self._template,
            "timeout": timeout or cfg.timeout,
            # 只注入白名单 env + 调用方显式 env，避免把宿主机全部密钥带进沙箱
            "envs": cfg.resolved_envs(envs) or None,
        }
        if api_key:
            create_kwargs["api_key"] = api_key
        if domain:
            create_kwargs["domain"] = domain
        # API 兼容：旧版（<1.5）用 Sandbox.create(...)；新版（1.5+）直接构造 Sandbox(...)
        try:
            if hasattr(Sandbox, "create"):
                self.sandbox = Sandbox.create(**create_kwargs)
            else:
                self.sandbox = Sandbox(**create_kwargs)
        except Exception as exc:
            raise SkillSandboxError(_classify_sandbox_error(exc)) from exc
        self._remote_roots: list[str] = []
        self._closed = False

    # ------------------------------------------------------------------ 文件同步

    def sync_dir(self, local_dir: str | Path, remote_root: str = _DEFAULT_REMOTE_ROOT) -> str:
        """把本地目录整棵上传到沙箱（保留相对结构），返回远端目录路径。

        自动跳过敏感/无关文件（env / 私钥 / .git / 缓存），受 max_files 上限保护。
        技能脚本内部用 Path(__file__) 锚定同目录数据文件，因此只要结构一致即可。
        """
        local = Path(local_dir)
        if not local.is_dir():
            raise SkillSandboxError(f"本地目录不存在: {local}")
        name = local.name
        dest = f"{remote_root.rstrip('/')}/{name}"
        uploaded = 0
        for p in sorted(local.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(local).as_posix()
            if any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(p.name, pat) for pat in self._cfg.exclude_patterns):
                continue
            self.sandbox.files.write(f"{dest}/{rel}", p.read_bytes())
            uploaded += 1
            if uploaded >= self._cfg.max_files:
                logger.warning("同步文件数达上限 %s，截断（%s）", self._cfg.max_files, local)
                break
        self._remote_roots.append(dest)
        logger.info("[sandbox] 已同步 %d 个文件: %s -> %s", uploaded, local, dest)
        return dest

    # ------------------------------------------------------------------ 执行

    @staticmethod
    def _build_command(remote_script: str, args: list[str] | None) -> str:
        parts = ["python3", shlex.quote(remote_script)]
        for a in args or []:
            parts.append(shlex.quote(str(a)))
        return " ".join(parts)

    def _assert_script_in_roots(self, remote_script: str) -> None:
        """脚本路径必须位于本沙箱已同步的远端目录内（防越界执行）。"""
        if not self._remote_roots:
            raise SkillSandboxError("尚未同步任何 skill 目录，无法执行远端脚本（请先调用 sync_dir）")
        if not any(remote_script.startswith(root) for root in self._remote_roots):
            raise SkillSandboxError(f"远端脚本路径不在已同步目录内（{self._remote_roots}）: {remote_script}")

    def run_script(
        self,
        remote_script: str,
        args: list[str] | None = None,
        *,
        cwd: str | None = None,
        envs: dict[str, str] | None = None,
        timeout: float | None = None,
        allow_outside_roots: bool = False,
    ) -> ScriptResult:
        """在沙箱内执行一个已同步的 python 脚本。

        Args:
            remote_script: 沙箱内脚本绝对路径（由 sync_dir 的返回值拼接）。
            args: 命令行参数（自动 quote）。
            cwd/envs/timeout: 命令级覆盖。
            allow_outside_roots: 是否允许执行已同步目录之外的路径（默认禁止，安全兜底）。
        """
        if not allow_outside_roots:
            self._assert_script_in_roots(remote_script)
        cmd = self._build_command(remote_script, args)
        return self._run_command(cmd, cwd=cwd, envs=envs, timeout=timeout)

    def run_code(
        self,
        code: str,
        *,
        cwd: str | None = None,
        envs: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> ScriptResult:
        """在沙箱内直接执行一段代码（只跑标准库/模板预装能力）。"""
        cmd = f"python3 -c {shlex.quote(code)}"
        return self._run_command(cmd, cwd=cwd, envs=envs, timeout=timeout)

    def _run_command(
        self,
        cmd: str,
        *,
        cwd: str | None,
        envs: dict[str, str] | None,
        timeout: float | None,
    ) -> ScriptResult:
        """执行命令并把结果归一化为 ScriptResult。

        非零退出时 SDK（1.5+）会抛 CommandExitException 且 exception.result 携带
        stdout/stderr/exit_code——必须捕获并取回内容，否则 LLM 只看到一句
        "Command exited with code 2"，看不到脚本打印的用法/原因。
        """
        try:
            result = self.sandbox.commands.run(
                cmd,
                cwd=cwd,
                envs=self._cfg.resolved_envs(envs) or None,
                timeout=timeout or self._cfg.command_timeout,
            )
            return self._to_result(result, timed_out=False)
        except Exception as exc:
            # e2b 1.5+：命令非零退出抛 CommandExitException，它本身继承 CommandResult，
            # stdout/stderr/exit_code 直接挂在异常上——取回完整内容，避免只报 "exit code N"。
            code = getattr(exc, "exit_code", None)
            if code is not None:
                return ScriptResult(
                    stdout=str(getattr(exc, "stdout", "") or ""),
                    stderr=str(getattr(exc, "stderr", "") or ""),
                    exit_code=int(code),
                )
            low = str(exc).lower()
            timed_out = "timeout" in low or "timed out" in low
            if timed_out:
                return ScriptResult(
                    stdout="",
                    stderr=f"沙箱命令执行超时（> {timeout or self._cfg.command_timeout}s，可调大 skills.sandbox.command_timeout）: {exc}",
                    exit_code=-1,
                    timed_out=True,
                )
            return ScriptResult(
                stdout="",
                stderr=_classify_sandbox_error(exc),
                exit_code=-1,
            )

    def _to_result(self, raw: Any, *, timed_out: bool) -> ScriptResult:
        # 注意：不要写成 `x or 1` —— 成功退出码 0 会被误判（0 是 falsy）。
        exit_code = getattr(raw, "exit_code", None)
        stdout = str(getattr(raw, "stdout", "") or "")
        stderr = str(getattr(raw, "stderr", "") or "")
        truncated = False
        cap = self._cfg.max_output_chars
        if cap and len(stdout) > cap:
            stdout = stdout[:cap] + f"\n…[输出截断, 共 {len(stdout)} 字符]"
            truncated = True
        if cap and len(stderr) > cap:
            stderr = stderr[:cap] + "\n…[stderr 截断]"
        return ScriptResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=int(exit_code) if exit_code is not None else -1,
            timed_out=timed_out,
            truncated=truncated,
        )

    # ------------------------------------------------------------------ 产物拉回

    def download_files(self, remote_globs: list[str], local_dir: str | Path) -> list[Path]:
        """把沙箱内的产物拉回本地目录（供人工介入时检查/展示）。返回本地路径列表。"""
        local = Path(local_dir)
        local.mkdir(parents=True, exist_ok=True)
        saved: list[Path] = []
        for rel_glob in remote_globs:
            # e2b files API 提供 list/read；这里用防御式实现：先 list 再逐个 read
            try:
                entries = self.sandbox.files.list("/home/user")  # 按需改造成指定目录
            except Exception:
                entries = []
            for entry in entries or []:
                name = getattr(entry, "name", "") or str(getattr(entry, "path", ""))
                if not fnmatch.fnmatch(name, rel_glob):
                    continue
                try:
                    data = self.sandbox.files.read(getattr(entry, "path", name))
                    out = local / Path(name).name
                    out.write_bytes(data if isinstance(data, bytes) else str(data).encode())
                    saved.append(out)
                except Exception as exc:  # 单个产物失败不影响其余
                    logger.warning("[sandbox] 拉回产物失败 %s: %s", name, exc)
        return saved

    # ------------------------------------------------------------------ 生命周期

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.sandbox.kill()
        except Exception:
            pass
        logger.info("E2B 沙箱已销毁（临时产物随之回收）")

    def __enter__(self) -> SkillSandbox:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ async 门面

    @classmethod
    async def acreate(cls, **kwargs: Any) -> SkillSandbox:
        return await asyncio.to_thread(cls, **kwargs)

    async def async_sync_dir(self, *args: Any, **kwargs: Any) -> str:
        return await asyncio.to_thread(self.sync_dir, *args, **kwargs)

    async def arun_script(self, *args: Any, **kwargs: Any) -> ScriptResult:
        return await asyncio.to_thread(self.run_script, *args, **kwargs)

    async def arun_code(self, *args: Any, **kwargs: Any) -> ScriptResult:
        return await asyncio.to_thread(self.run_code, *args, **kwargs)

    async def aclose(self) -> None:
        await asyncio.to_thread(self.close)


async def run_skill_script_once(
    local_skill_dir: str | Path,
    script_relpath: str,
    args: list[str] | None = None,
    *,
    envs: dict[str, str] | None = None,
) -> ScriptResult:
    """一次性便捷入口：创建沙箱 → 同步 skill 目录 → 执行脚本 → 销毁沙箱。

    Args:
        local_skill_dir: 本地 skill 目录（scripts 的父级）。
        script_relpath: 脚本相对 skill 目录的路径（如 scripts/get_city_code.py）。
    """
    sandbox = await SkillSandbox.acreate(envs=envs)
    try:
        remote = await sandbox.async_sync_dir(local_skill_dir)
        return await sandbox.arun_script(f"{remote}/{script_relpath.lstrip('/')}", args=args)
    finally:
        await sandbox.aclose()
