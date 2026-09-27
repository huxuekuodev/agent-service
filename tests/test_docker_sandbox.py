"""Docker 沙箱单元测试（离线：注入假 docker 客户端，不连任何真实 Docker）。

覆盖四条需求：一次性容器、宿主机技能目录按天缓存、根文件系统只读 + 只读挂载 + host 网络、
命令超时即 kill 容器；以及失败路径的可读报错。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.agents.skills.docker_sandbox import DockerSkillSandbox, docker_available
from app.agents.skills.sandbox import SandboxConfig, SkillSandboxError


class FakeContainer:
    def __init__(self, name: str, image: str, kwargs: dict[str, Any], client: FakeDocker) -> None:
        self.name = name
        self.id = f"id-{name}"
        self.image = image
        self.create_kwargs = kwargs
        self.short_id = name
        self.status = "created"
        self.removed = False
        self.killed = False
        self.exec_calls: list[list[str]] = []
        self.put_archives: list[tuple[str, bytes]] = []
        self._client = client

    def start(self) -> None:
        self.status = "running"

    def reload(self) -> None:
        return None

    def wait(self, timeout: int | None = None) -> dict:
        return {"StatusCode": 0}

    def logs(self) -> bytes:
        return b""

    def exec_run(self, cmd: list[str]) -> Any:
        self.exec_calls.append(list(cmd))
        key = " ".join(cmd)
        if key in self._client.exec_results:
            return self._client.exec_results[key]
        # 默认语义：存在性探测返回"不存在"，写入校验返回文件数
        if "wc -l" in key:
            return _FakeExecResult(b"6\n", 0)
        return _FakeExecResult(b"", 0)

    def put_archive(self, path: str, data: bytes) -> bool:
        self.put_archives.append((path, data))
        return True

    def kill(self) -> None:
        self.killed = True
        self.status = "exited"

    def remove(self, force: bool = False) -> None:
        self.removed = True
        self.status = "removed"


class _FakeExecResult:
    def __init__(self, output: bytes, exit_code: int) -> None:
        self.output = output
        self.exit_code = exit_code


class FakeDocker:
    """最小可用的假 docker 客户端。"""

    def __init__(self, *, images: tuple[str, ...] = ("python:3.11",), exec_results: dict[str, Any] | None = None, exec_hangs: bool = False) -> None:
        # 模拟 docker-py 客户端结构：client.containers.create / client.images.list / client.api.exec_*
        self.containers = self
        self.images = self
        self.api = self
        self.created: list[FakeContainer] = []
        self._images = images
        self.exec_results = exec_results or {}
        self.exec_hangs = exec_hangs
        self.killed_containers: list[str] = []

    # 镜像
    def list(self, name: str = "") -> list[Any]:
        return [object()] if name in self._images else []

    # 容器
    def create(self, image: str, **kwargs: Any) -> FakeContainer:
        container = FakeContainer(f"c{len(self.created)}", image, kwargs, self)
        self.created.append(container)
        return container

    def _find(self, container_id: str) -> FakeContainer:
        """真实调用方传的是 container.id（不是 short_id），两者都认。"""
        for c in self.created:
            if container_id in (c.id, c.short_id):
                return c
        raise KeyError(f"未知容器: {container_id}")

    # exec API
    def exec_create(self, container_id: str, cmd: list[str], workdir: str = "", environment: dict | None = None) -> dict:
        self._find(container_id).exec_calls.append(list(cmd))
        return {"Id": f"exec-{len(self.created)}-{len(self._find(container_id).exec_calls)}"}

    def exec_start(self, exec_id: str, detach: bool = False, stream: bool = False) -> bytes:  # noqa: ARG002
        if self.exec_hangs:
            import time

            time.sleep(30)  # 触发调用方超时 → kill 容器
        return self.exec_results.get("__default__", _FakeExecResult(b"out", 0)).output

    def exec_inspect(self, exec_id: str) -> dict:  # noqa: ARG002
        return {"ExitCode": 0, "Running": False}


@pytest.fixture
def skill_dir(tmp_path: Path) -> Path:
    """一个最小技能目录（含脚本与数据）。"""
    root = tmp_path / "query-weather"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "get_city_code.py").write_text("print('x')\n", encoding="utf-8")
    (root / "SKILL.md").write_text("# 技能\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=1\n", encoding="utf-8")  # 应被排除
    return root


def _config(tmp_path: Path, **overrides: Any) -> SandboxConfig:
    base = dict(backend="docker", docker_url="ssh://fake:22", docker_image="python:3.11", host_tmp_dir=str(tmp_path / "host"), command_timeout=5)
    base.update(overrides)
    return SandboxConfig(**base)


# --------------------------------------------------------------------------- 技能目录缓存


def test_prepare_uploads_once_then_reuses_host_cache(tmp_path: Path, skill_dir: Path) -> None:
    """首次上传（排除敏感文件），当天再 prepare 时跳过上传。"""
    fake = FakeDocker()
    sandbox = DockerSkillSandbox(config=_config(tmp_path), client=fake)

    remote, uploaded = sandbox.prepare(skill_dir, "query-weather")
    assert remote == "/workspace/query-weather"
    assert uploaded == 2  # scripts/get_city_code.py + SKILL.md；.env 被排除
    helper = fake.created[-1]
    assert helper.create_kwargs["volumes"]  # 挂载了宿主机目录
    assert helper.put_archives and helper.put_archives[0][0].endswith("/query-weather")
    assert helper.removed is True  # helper 容器用完即删

    # 宿主机已有该技能 → 第二次直接复用
    fake.exec_results["sh -c ls -A /mnt/query-weather 2>/dev/null | head -1"] = _FakeExecResult(b"SKILL.md\n", 0)
    sandbox2 = DockerSkillSandbox(config=_config(tmp_path), client=fake)
    _, uploaded2 = sandbox2.prepare(skill_dir, "query-weather")
    assert uploaded2 == 0
    assert not fake.created[-1].put_archives  # 没有再次上传


def test_prepare_separates_days(tmp_path: Path, skill_dir: Path) -> None:
    """按天分目录：不同日期各自上传（技能改动当天生效）。"""
    import datetime

    fake = FakeDocker()
    day1 = DockerSkillSandbox(config=_config(tmp_path), client=fake, clock=lambda: datetime.date(2026, 1, 1))
    day2 = DockerSkillSandbox(config=_config(tmp_path), client=fake, clock=lambda: datetime.date(2026, 1, 2))
    day1.prepare(skill_dir, "query-weather")
    day2.prepare(skill_dir, "query-weather")
    dirs = [c.create_kwargs["volumes"].popitem()[0] for c in fake.created if c.create_kwargs.get("volumes")]
    assert any("2026-01-01" in d for d in dirs) and any("2026-01-02" in d for d in dirs)


def test_prepare_rejects_empty_skill_dir(tmp_path: Path) -> None:
    empty = tmp_path / "empty-skill"
    empty.mkdir()
    sandbox = DockerSkillSandbox(config=_config(tmp_path), client=FakeDocker())
    with pytest.raises(SkillSandboxError):
        sandbox.prepare(empty, "empty-skill")


# --------------------------------------------------------------------------- 容器安全与网络


def test_container_is_hardened_readonly_with_host_network(tmp_path: Path, skill_dir: Path) -> None:
    """根文件系统只读 + tmpfs + host 网络 + 技能目录只读挂载 + 资源上限。"""
    fake = FakeDocker()
    sandbox = DockerSkillSandbox(config=_config(tmp_path), client=fake)
    sandbox.prepare(skill_dir, "query-weather")
    sandbox.start_container()

    kwargs = fake.created[-1].create_kwargs
    assert kwargs["read_only"] is True
    assert kwargs["tmpfs"]["/tmp"].startswith("rw,size=")
    assert kwargs["network_mode"] == "host"
    assert kwargs["working_dir"] == "/workspace/query-weather"
    mount = kwargs["volumes"][f"{tmp_path}/host/{sandbox._today.isoformat()}/query-weather"]
    assert mount["mode"] == "ro" and mount["bind"] == "/workspace/query-weather"
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["security_opt"] == ["no-new-privileges"]
    assert kwargs["mem_limit"] == "256m" and kwargs["pids_limit"] == 128
    assert kwargs["labels"]["deer.sandbox"] == "skill"


def test_run_command_rejects_path_outside_workspace(tmp_path: Path, skill_dir: Path) -> None:
    fake = FakeDocker()
    sandbox = DockerSkillSandbox(config=_config(tmp_path), client=fake)
    sandbox.prepare(skill_dir, "query-weather")
    with pytest.raises(SkillSandboxError):
        sandbox.run_command("ls", cwd="/etc")


def test_run_command_returns_output_and_exit_code(tmp_path: Path, skill_dir: Path) -> None:
    fake = FakeDocker(exec_results={"__default__": _FakeExecResult(b"hello\n", 0)})
    sandbox = DockerSkillSandbox(config=_config(tmp_path), client=fake)
    sandbox.prepare(skill_dir, "query-weather")
    result = sandbox.run_command("python3 scripts/hello.py")
    assert result.ok and result.stdout.strip() == "hello"


def test_run_command_puts_failure_output_in_stderr(tmp_path: Path, skill_dir: Path) -> None:
    """docker exec 把 stdout/stderr 合并：失败时整体作为 stderr 回传，避免 LLM 看不到原因。"""
    fake = FakeDocker(exec_results={"__default__": _FakeExecResult(b"usage: --province required\n", 1)})
    fake.exec_inspect = lambda exec_id: {"ExitCode": 2, "Running": False}  # type: ignore[method-assign]
    sandbox = DockerSkillSandbox(config=_config(tmp_path), client=fake)
    sandbox.prepare(skill_dir, "query-weather")
    result = sandbox.run_command("python3 scripts/x.py")
    assert result.exit_code == 2
    assert "usage" in result.stderr and result.stdout == ""


# --------------------------------------------------------------------------- 超时与回收


def test_timeout_kills_container(tmp_path: Path, skill_dir: Path) -> None:
    """命令超时 → kill 容器（用户要求的保护），并给出可执行的提示。"""
    fake = FakeDocker(exec_hangs=True)
    sandbox = DockerSkillSandbox(config=_config(tmp_path, command_timeout=1), client=fake)
    sandbox.prepare(skill_dir, "query-weather")
    sandbox.start_container()
    container = sandbox.container

    result = sandbox.run_command("sleep 30")
    assert result.timed_out is True and result.exit_code == -1
    assert "超时" in result.stderr and "command_timeout" in result.stderr
    assert container.killed is True and container.removed is True
    assert sandbox.is_alive is False


def test_close_removes_container_and_keeps_host_files(tmp_path: Path, skill_dir: Path) -> None:
    fake = FakeDocker()
    sandbox = DockerSkillSandbox(config=_config(tmp_path), client=fake)
    sandbox.prepare(skill_dir, "query-weather")
    sandbox.start_container()
    container = sandbox.container
    sandbox.close()
    assert container.removed is True
    assert sandbox.container is None
    # 宿主机目录保留（当天复用）：不再创建/删除任何挂载目录
    assert sandbox.host_skill_dir("query-weather").endswith("query-weather")


def test_ensure_container_recreates_after_death(tmp_path: Path, skill_dir: Path) -> None:
    fake = FakeDocker()
    sandbox = DockerSkillSandbox(config=_config(tmp_path), client=fake)
    sandbox.prepare(skill_dir, "query-weather")
    sandbox.start_container()
    first = sandbox.container
    first.status = "exited"  # 模拟被超时 kill / 外部停止
    sandbox.ensure_container()
    assert sandbox.container is not first


# --------------------------------------------------------------------------- 可用性校验


def test_docker_available_reports_missing_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDocker(images=("alpine:latest",))
    monkeypatch.setattr("app.agents.skills.docker_sandbox._client_for", lambda url, timeout=30: fake)
    ok, reason = docker_available(_config(tmp_path))
    assert ok is False
    assert "python:3.11" in reason


def test_docker_available_true_when_image_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.agents.skills.docker_sandbox._client_for", lambda url, timeout=30: FakeDocker())
    ok, reason = docker_available(_config(tmp_path))
    assert ok is True and reason == ""


def test_sandbox_available_requires_url_or_local_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agents.skills import sandbox as sandbox_module

    class _Cfg:
        backend = "docker"
        enabled = True
        docker_url = ""

    class _Skills:
        sandbox = _Cfg()

    class _App:
        skills = _Skills()

    monkeypatch.setattr("app.config.get_app_config", lambda: _App())
    monkeypatch.setattr(sandbox_module.Path, "exists", lambda self: False)
    ok, reason = sandbox_module.sandbox_available()
    assert ok is False and "docker_url" in reason
