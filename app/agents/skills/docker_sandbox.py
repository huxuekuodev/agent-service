"""Docker 一次性容器沙箱：技能脚本的执行环境（替代云端 E2B，省钱且可控）。

设计（按实测结论定的，见 docs/SKILL_方案.md）：

    1. **一次性容器**：技能任务开始时建容器，任务结束（或空闲超时）即删除；容器内不留状态。
    2. **技能文件缓存在宿主机**：``<host_tmp_dir>/<YYYY-MM-DD>/<skill_id>/``；
       当天已有该技能就直接复用（**不重复上传**，省掉上传时间），跨天自动重新上传（技能改动当天生效）。
    3. **容器根文件系统只读**（``read_only=True``）：改不了系统文件；可写区只有 tmpfs ``/tmp``；
       技能目录以**只读**方式挂到 ``/workspace/<skill_id>``（脚本只能读技能自带的脚本/数据）。
    4. **走宿主机网络**（``network_mode=host`` 默认）：技能脚本能像宿主机一样访问外网/内网。
    5. **超时即 kill**：单条命令超过 ``skills.sandbox.command_timeout``（默认 5s）直接 kill 容器并删除，
       避免僵尸脚本占资源。
    6. 另有资源上限（内存/进程数/丢弃 capabilities/no-new-privileges），防止技能脚本影响宿主机。

**连接方式很重要**：docker-py 连远端守护进程用 ``ssh://`` 时，
``use_ssh_client=True``（走 ssh CLI 二进制）下 ``exec_run`` / ``put_archive`` 会**挂死**；
必须用 paramiko 传输（``use_ssh_client=False``，默认值）——已实测验证。
"""

from __future__ import annotations

import asyncio
import inspect
import io
import tarfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import date
from pathlib import Path
from typing import Any

from app.agents.skills.sandbox import SandboxConfig, ScriptResult, SkillSandboxError
from app.core.log import logger

__all__ = ["DockerSkillSandbox", "docker_available", "reset_docker_client"]

#: docker 客户端按 url 缓存（SSH 建连不便宜）
_clients: dict[str, Any] = {}


def _import_docker() -> Any:
    try:
        import docker  # type: ignore
    except ImportError as exc:  # pragma: no cover - 依赖缺失分支
        raise SkillSandboxError("Docker 沙箱不可用：缺少依赖 docker（宿主机执行 `uv sync` 安装后重试）") from exc
    return docker


def _client_for(url: str, *, timeout: int = 30) -> Any:
    """按 url 复用 DockerClient（远端 ssh 用 paramiko 传输）。"""
    client = _clients.get(url)
    if client is not None:
        try:
            client.ping()
            return client
        except Exception:  # 连接失效 → 重建
            _clients.pop(url, None)
    docker = _import_docker()
    kwargs: dict[str, Any] = {"timeout": timeout}
    if url.startswith("ssh://"):
        # 必须用 paramiko 传输：ssh CLI 传输下 exec/put_archive 会挂死（实测）
        kwargs["use_ssh_client"] = False
        try:
            import paramiko  # noqa: F401
        except ImportError as exc:
            raise SkillSandboxError("连接远端 Docker 需要 paramiko（宿主机执行 `uv sync` 安装后重试）") from exc
    try:
        client = docker.DockerClient(base_url=url or None, **kwargs)
        client.ping()
    except Exception as exc:
        raise SkillSandboxError(f"连接 Docker 失败（{url or '默认 socket'}）：{exc}") from exc
    _clients[url] = client
    return client


def reset_docker_client(url: str = "") -> None:
    """丢弃缓存的客户端（测试/切配置用）。"""
    if url:
        _clients.pop(url, None)
    else:
        _clients.clear()


def docker_available(config: SandboxConfig | None = None) -> tuple[bool, str]:
    """Docker 沙箱是否可用：(可用?, 原因)。只做**校验**，不创建容器。"""
    cfg = config or SandboxConfig.from_app_config()
    if not cfg.docker_url and not Path("/var/run/docker.sock").exists():
        return False, "未配置 skills.sandbox.docker_url（.env 的 DOCKER_HOST_URL），且本机没有 /var/run/docker.sock"
    try:
        client = _client_for(cfg.docker_url)
    except SkillSandboxError as exc:
        return False, str(exc)
    try:
        if not client.images.list(name=cfg.docker_image):
            return False, f"Docker 主机上不存在镜像 {cfg.docker_image}（请先 docker pull 或改用现成镜像）"
    except Exception as exc:
        return False, f"查询镜像失败：{exc}"
    return True, ""


class DockerSkillSandbox:
    """一个技能的一次性沙箱（容器 + 宿主机技能目录缓存）。

    与 E2B 版 :class:`app.agents.skills.sandbox.SkillSandbox` 接口对齐（``prepare`` / ``arun_command`` /
    ``aclose``），由 ``session.py`` 统一按 ``skills.sandbox.backend`` 选择。
    """

    def __init__(
        self,
        *,
        config: SandboxConfig | None = None,
        envs: dict[str, str] | None = None,
        client: Any = None,
        clock: Callable[[], date] | None = None,
    ) -> None:
        self._cfg = config or SandboxConfig.from_app_config()
        self._envs = self._cfg.resolved_envs(envs)
        self._client = client
        self._today = (clock or date.today)()
        self.skill_id = ""
        self.remote_dir = ""
        self.container: Any = None
        self.uploaded_files = 0
        self._dead = False

    # ------------------------------------------------------------------ 连接 / 准备

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = _client_for(self._cfg.docker_url)
        return self._client

    @property
    def is_alive(self) -> bool:
        """容器是否仍然可用（被 kill / 删除 / 超时后为 False，需要重建）。"""
        if self._dead or self.container is None:
            return False
        try:
            self.container.reload()
            return self.container.status == "running"
        except Exception:
            return False

    def _host_dir(self, skill_id: str) -> str:
        """宿主机上的技能目录（按天分目录，便于缓存与过期清理）。"""
        return f"{self._cfg.host_tmp_dir.rstrip('/')}/{self._today.isoformat()}/{skill_id}"

    def host_skill_dir(self, skill_id: str) -> str:
        """对外暴露：技能在宿主机上的目录（供日志/排障）。"""
        return self._host_dir(skill_id)

    def prepare(self, local_skill_dir: str | Path, skill_id: str = "") -> tuple[str, int]:
        """准备技能目录：宿主机已有则**跳过上传**，否则整份上传（含限制与敏感文件过滤）。

        Returns:
            ``(容器内技能目录, 上传文件数)``；复用缓存时上传数为 0。
        """
        local = Path(local_skill_dir)
        if not local.is_dir():
            raise SkillSandboxError(f"本地技能目录不存在: {local}")
        skill_id = skill_id or local.name
        host_dir = self._host_dir(skill_id)
        self.skill_id = skill_id
        self.remote_dir = f"/workspace/{skill_id}"

        files = self._collect_files(local)
        if not files:
            raise SkillSandboxError(f"技能目录没有可上传的文件: {local}")
        tar_bytes = self._build_tar(local, files)
        if self._ensure_host_files(host_dir, skill_id, tar_bytes):
            logger.info("[docker-sandbox] 复用宿主机技能目录（跳过上传）: {}", host_dir)
            return self.remote_dir, 0
        self.uploaded_files = len(files)
        logger.info("[docker-sandbox] 已上传技能到宿主机: {} ({} 个文件, {} KB)", host_dir, len(files), len(tar_bytes) // 1024)
        return self.remote_dir, len(files)

    def _ensure_host_files(self, host_dir: str, skill_id: str, tar_bytes: bytes) -> bool:
        """确保宿主机上有该技能目录：已存在 → 复用（返回 True），否则写入（返回 False）。

        只用一个临时容器完成"检查 + 建目录 + 写 tar"，减少容器创建次数（每次约 1s）；
        ``put_archive`` 要求目标目录已存在，所以先 ``mkdir``。
        """
        day_dir = host_dir.rsplit("/", 1)[0]
        helper = None
        try:
            helper = self.client.containers.create(
                self._cfg.docker_image,
                command=["sleep", "120"],
                volumes={day_dir: {"bind": "/mnt", "mode": "rw"}},
            )
            helper.start()
            probe = helper.exec_run(["sh", "-c", f"ls -A /mnt/{skill_id} 2>/dev/null | head -1"])
            if probe.output.strip():
                return True
            helper.exec_run(["mkdir", "-p", f"/mnt/{skill_id}"])
            helper.put_archive(f"/mnt/{skill_id}", tar_bytes)
            count = helper.exec_run(["sh", "-c", f"find /mnt/{skill_id} -type f | wc -l"]).output.decode("utf-8", "ignore").strip()
            if not count or count == "0":
                raise SkillSandboxError(f"技能文件写入宿主机后校验为空（{host_dir}）")
            logger.debug("[docker-sandbox] 宿主机技能文件数={}", count)
            return False
        except SkillSandboxError:
            raise
        except Exception as exc:
            raise SkillSandboxError(f"准备宿主机技能目录失败（{host_dir}）: {exc}") from exc
        finally:
            if helper is not None:
                try:
                    helper.remove(force=True)
                except Exception:
                    pass

    def _collect_files(self, local: Path) -> list[Path]:
        """按排除规则收集要上传的文件（受 max_files 上限保护）。"""
        files: list[Path] = []
        for p in sorted(local.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(local).as_posix()
            if self._cfg.is_excluded(rel, p.name):
                continue
            files.append(p)
            if len(files) >= self._cfg.max_files:
                logger.warning("[docker-sandbox] 文件数达上限 {}，已截断", self._cfg.max_files)
                break
        return files

    def _build_tar(self, local: Path, files: list[Path]) -> bytes:
        """把技能目录打包成 tar（上传用）。"""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for path in files:
                tar.add(path, arcname=path.relative_to(local).as_posix())
        return buf.getvalue()

    # ------------------------------------------------------------------ 容器生命周期

    def start_container(self) -> Any:
        """创建并启动一次性沙箱容器（只读根 + tmpfs + 宿主网络 + 只读技能挂载）。"""
        if not self.skill_id or not self.remote_dir:
            raise SkillSandboxError("尚未 prepare 技能目录，无法创建容器")
        create_kwargs: dict[str, Any] = {
            "image": self._cfg.docker_image,
            # 容器常驻，命令用 exec 依次执行（同一个技能任务内状态连续）
            "command": ["sleep", "infinity"],
            "detach": True,
            "read_only": True,
            "tmpfs": {"/tmp": f"rw,size={self._cfg.tmpfs_size},exec"},
            "network_mode": self._cfg.docker_network_mode,
            "working_dir": self.remote_dir,
            # 技能目录只读挂载：脚本只能读，不能改技能本身
            "volumes": {self._host_dir(self.skill_id): {"bind": self.remote_dir, "mode": "ro"}},
            "mem_limit": self._cfg.mem_limit,
            "pids_limit": self._cfg.pids_limit,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges"],
            "environment": self._envs or {},
            "labels": {"deer.sandbox": "skill", "deer.skill_id": self.skill_id},
        }
        try:
            self.container = self.client.containers.create(**create_kwargs)
            self.container.start()
        except Exception as exc:
            raise SkillSandboxError(f"创建沙箱容器失败: {exc}") from exc
        self._dead = False
        logger.info("[docker-sandbox] 容器已启动: skill={} container={} image={}", self.skill_id, self.container.short_id, self._cfg.docker_image)
        return self.container

    def ensure_container(self) -> Any:
        """取可用容器（没有/已死则新建）。"""
        if not self.is_alive:
            self.start_container()
        return self.container

    # ------------------------------------------------------------------ 执行

    def run_command(
        self,
        command: str | list[str],
        *,
        cwd: str | None = None,
        timeout: float | None = None,
    ) -> ScriptResult:
        """在沙箱容器里执行一条命令；**超时即 kill 并删除容器**（避免僵尸进程占资源）。"""
        container = self.ensure_container()
        workdir = cwd or self.remote_dir
        if not str(workdir).startswith("/workspace"):
            raise SkillSandboxError(f"工作目录必须位于 /workspace 内（技能目录只读挂载区）: {workdir}")
        limit = float(timeout or self._cfg.command_timeout)
        cmd = ["sh", "-c", command] if isinstance(command, str) else list(command)

        started = time.perf_counter()
        try:
            exec_id = self.client.api.exec_create(container.id, cmd, workdir=workdir, environment=self._envs or None)["Id"]
        except Exception as exc:
            raise SkillSandboxError(f"创建执行会话失败: {exc}") from exc

        # 关键：exec_start(detach=True) 会丢弃输出，必须阻塞读取；
        # 超时控制放在"等结果"的线程上——到点就 kill 容器，容器一死 exec 随之结束。
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(self.client.api.exec_start, exec_id, False, False)
        try:
            output = future.result(timeout=limit)
        except FuturesTimeoutError:
            self._kill_container(f"命令超时（>{limit:.0f}s）")
            executor.shutdown(wait=False)
            return ScriptResult(
                stdout="",
                stderr=f"命令执行超时（> {limit:.0f}s），已按策略 kill 容器；可调大 skills.sandbox.command_timeout。命令：{str(command)[:120]}",
                exit_code=-1,
                duration_ms=int((time.perf_counter() - started) * 1000),
                timed_out=True,
            )
        except Exception as exc:
            executor.shutdown(wait=False)
            raise SkillSandboxError(f"执行命令失败: {exc}") from exc
        executor.shutdown(wait=False)

        duration_ms = int((time.perf_counter() - started) * 1000)
        try:
            exit_code = int(self.client.api.exec_inspect(exec_id).get("ExitCode") or 0)
        except Exception:
            exit_code = 0
        text = output.decode("utf-8", "replace") if isinstance(output, bytes) else str(output or "")
        # docker exec 把 stdout/stderr 合并在同一流：按退出码决定放到哪一侧，避免丢信息
        stdout, stderr = (text, "") if exit_code == 0 else ("", text)
        return self._to_result(stdout, stderr, exit_code, duration_ms)

    def _to_result(self, stdout: str, stderr: str, exit_code: int, duration_ms: int) -> ScriptResult:
        """输出截断（保护上下文）后返回结果。"""
        cap = self._cfg.max_output_chars
        truncated = False
        if cap and len(stdout) > cap:
            stdout = stdout[:cap] + f"\n…[输出截断, 共 {len(stdout)} 字符]"
            truncated = True
        if cap and len(stderr) > cap:
            stderr = stderr[:cap] + "\n…[stderr 截断]"
            truncated = True
        return ScriptResult(stdout=stdout, stderr=stderr, exit_code=exit_code, duration_ms=duration_ms, truncated=truncated)

    def _kill_container(self, reason: str) -> None:
        """kill + 删除容器（超时/异常路径），并标记沙箱已死。"""
        logger.warning("[docker-sandbox] kill 容器: skill={} 原因={}", self.skill_id, reason)
        if self.container is not None:
            try:
                self.container.kill()
            except Exception:
                pass
            try:
                self.container.remove(force=True)
            except Exception:
                pass
        self._dead = True
        self.container = None

    # ------------------------------------------------------------------ 关闭

    def close(self) -> None:
        """删除容器（技能文件保留在宿主机，当天复用）。"""
        if self.container is not None:
            try:
                self.container.remove(force=True)
                logger.info("[docker-sandbox] 容器已删除: skill={}", self.skill_id)
            except Exception as exc:
                logger.warning("[docker-sandbox] 删除容器失败（忽略）: {}", exc)
        self.container = None
        self._dead = True

    def cleanup_host_dirs(self, *, keep_days: int | None = None) -> int:
        """清理宿主机上过期的按天技能目录（保留最近 N 天）。"""
        keep = int(keep_days if keep_days is not None else self._cfg.keep_host_files_days)
        script = f"cd {self._cfg.host_tmp_dir} 2>/dev/null || exit 0; ls -1d */ 2>/dev/null | sort -r | tail -n +{keep + 1} | xargs -r rm -rf; echo cleaned"
        helper = None
        try:
            helper = self.client.containers.create(
                self._cfg.docker_image,
                command=["sh", "-c", script],
                volumes={self._cfg.host_tmp_dir: {"bind": "/mnt", "mode": "rw"}},
            )
            helper.start()
            helper.wait(timeout=30)
            return 0
        except Exception as exc:
            logger.warning("[docker-sandbox] 清理宿主技能目录失败（忽略）: {}", exc)
            return 0
        finally:
            if helper is not None:
                try:
                    helper.remove(force=True)
                except Exception:
                    pass

    # ------------------------------------------------------------------ async 门面

    @classmethod
    async def acreate(cls, **kwargs: Any) -> DockerSkillSandbox:
        return await asyncio.to_thread(cls, **kwargs)

    async def aprepare(self, *args: Any, **kwargs: Any) -> tuple[str, int]:
        return await asyncio.to_thread(self.prepare, *args, **kwargs)

    async def arun_command(self, *args: Any, **kwargs: Any) -> ScriptResult:
        return await asyncio.to_thread(self.run_command, *args, **kwargs)

    async def aclose(self) -> None:
        await asyncio.to_thread(self.close)

    # 兼容 SkillSandbox 的旧方法名（session.py 早期版本用 async_sync_dir）
    async def async_sync_dir(self, *args: Any, **kwargs: Any) -> str:
        remote, _ = await self.aprepare(*args, **kwargs)
        return remote

    @classmethod
    def from_config(cls, config: SandboxConfig | None = None, *, envs: dict[str, str] | None = None) -> DockerSkillSandbox:
        return cls(config=config, envs=envs)


def _is_coroutine(fn: Any) -> bool:
    return inspect.iscoroutinefunction(fn)
