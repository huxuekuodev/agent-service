"""应用配置（独立服务版）。

从 config.yaml 读取（YAML 格式），替代 deerflow.config.app_config。
配置优先级：
1. 显式 config_path 参数
2. AGENT_CONFIG_PATH 环境变量
3. ./config.yaml（当前目录）
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# 加载 .env（必须在任何 os.getenv 之前，且对所有入口生效）
load_dotenv()


# 环境变量引用：$VAR 会被替换为环境变量的值
def _resolve_env(value: Any) -> Any:
    """将 $VAR 字符串替换为环境变量值。"""
    if isinstance(value, str) and value.startswith("$") and len(value) > 1:
        return os.getenv(value[1:], "")
    return value


def _find_config_file() -> Path | None:
    """按优先级查找 config.yaml。"""
    # 1. 环境变量指定路径
    env_path = os.getenv("AGENT_CONFIG_PATH")
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p
    # 2. 当前目录
    cwd = Path.cwd() / "config.yaml"
    if cwd.exists():
        return cwd
    # 3. 项目根（pyproject.toml 同目录）
    here = Path(__file__).resolve().parent.parent.parent / "config.yaml"
    if here.exists():
        return here
    return None


@dataclass
class ModelConfig:
    """单个模型配置。"""

    name: str
    use: str = "langchain_openai:ChatOpenAI"
    """模型类路径，如 langchain_openai:ChatOpenAI。"""
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    """模型名称（provider 侧）。"""
    supports_thinking: str | None = None
    """是否支持 thinking。"""
    supports_vision: bool = False
    max_tokens: int | None = None
    temperature: float | None = None
    timeout: float | None = None
    max_retries: int | None = None

    @classmethod
    def from_dict(cls, d: dict) -> ModelConfig:
        return cls(
            name=_resolve_env(d.get("name", "default")) or "default",
            use=_resolve_env(d.get("use", "langchain_openai:ChatOpenAI")) or "langchain_openai:ChatOpenAI",
            api_key=_resolve_env(d.get("api_key")),
            base_url=_resolve_env(d.get("base_url")),
            model=_resolve_env(d.get("model")),
            supports_thinking=_resolve_env(d.get("supports_thinking")),
            supports_vision=bool(d.get("supports_vision", False)),
            max_tokens=d.get("max_tokens"),
            temperature=d.get("temperature"),
            timeout=d.get("timeout"),
            max_retries=d.get("max_retries"),
        )


@dataclass
class PlanEvaluationSettings:
    """规划评估配置（与 agentsv2 评估器兼容）。"""

    enabled: bool = False
    sample_rate: float = 1.0
    judge_model: str | None = None
    dimensions: dict[str, bool] = field(
        default_factory=lambda: {
            "clarification_quality": True,
            "task_atomicity": True,
            "agent_selection_validity": True,
        }
    )

    @classmethod
    def from_dict(cls, d: dict | None) -> PlanEvaluationSettings:
        d = d or {}
        return cls(
            enabled=bool(d.get("enabled", False)),
            sample_rate=float(d.get("sample_rate", 1.0)),
            judge_model=d.get("judge_model"),
            dimensions={**cls().dimensions, **(d.get("dimensions") or {})},
        )


@dataclass
class EvaluatorSettings:
    """评估器配置（config.yaml 的 ``evaluators`` 列表项）。

    字段语义与 ``models`` 列表项对齐：
      - ``name``: 评估器唯一名（registry 里按此注册，节点通过名字取用）。
      - ``display_name``: 展示名（Langfuse observation 名默认用它）。
      - ``use``: BaseEvaluator 子类的 import 路径，如 ``app.agents.evaluation.plan_evaluator:PlanEvaluator``。
      - ``model``: 评估 LLM 在 ``models`` 列表里的 name；省略时用默认模型。
      - ``system_prompt``: 评估 LLM 的系统提示词（可定制）。
      - ``enabled``: 总开关。
      - ``sample_rate``: 采样率 0-1。
      - ``metrics``: 按指标名覆盖 ``{name: {enabled/pass_score/label/description}}``。
      - ``extra``: 传给子类 __init__ 的额外关键字参数（任意子类定制字段）。
    """

    name: str
    display_name: str = ""
    use: str = ""
    model: str | None = None
    system_prompt: str | None = None
    enabled: bool = True
    sample_rate: float = 1.0
    metrics: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict | None) -> EvaluatorSettings:
        d = d or {}
        extra = dict(d.get("extra") or {})
        return cls(
            name=str(d.get("name", "")),
            display_name=str(d.get("display_name", "")),
            use=str(d.get("use", "")),
            model=_resolve_env(d.get("model")),
            system_prompt=d.get("system_prompt"),
            enabled=bool(d.get("enabled", True)),
            sample_rate=float(d.get("sample_rate", 1.0)),
            metrics=dict(d.get("metrics") or {}),
            extra=extra,
        )

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "use": self.use,
            "model": self.model,
            "system_prompt": self.system_prompt,
            "enabled": self.enabled,
            "sample_rate": self.sample_rate,
            "metrics": self.metrics,
            "extra": self.extra,
        }


@dataclass
class SubagentsSettings:
    """执行 agent 配置（兼容 agentsv2 的 custom_agents）。"""

    custom_agents: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict | None) -> SubagentsSettings:
        d = d or {}
        return cls(custom_agents=d.get("custom_agents") or {})


@dataclass
class LangfuseConfig:
    """Langfuse 配置。"""

    enabled: bool = False
    public_key: str | None = None
    secret_key: str | None = None
    host: str = "https://cloud.langfuse.com"

    @classmethod
    def from_dict(cls, d: dict | None) -> LangfuseConfig:
        d = d or {}
        return cls(
            enabled=bool(d.get("enabled", False)),
            public_key=_resolve_env(d.get("public_key")),
            secret_key=_resolve_env(d.get("secret_key")),
            host=_resolve_env(d.get("host", "https://cloud.langfuse.com")) or "https://cloud.langfuse.com",
        )


@dataclass
class LoggingConfig:
    """日志配置。"""

    level: str = "INFO"
    file: str | None = None

    @classmethod
    def from_dict(cls, d: dict | None) -> LoggingConfig:
        d = d or {}
        level = str(d.get("level", "INFO")).upper()
        file = d.get("file")
        return cls(level=level, file=file)


@dataclass
class DatabaseConfig:
    """数据库配置（checkpointer 共享存储）。"""

    backend: str = "memory"
    """memory | sqlite | postgres。集群部署用 postgres。"""
    postgres_url: str = ""
    """PostgreSQL 连接 URL，如 postgresql://user:pass@host:5432/db。"""

    @classmethod
    def from_dict(cls, d: dict | None) -> DatabaseConfig:
        d = d or {}
        return cls(
            backend=str(d.get("backend", "memory")),
            postgres_url=_resolve_env(d.get("postgres_url", "")),
        )


@dataclass
class AppConfig:
    """独立服务的全局配置。"""

    config_version: int = 1
    log_level: str = "INFO"
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    models: list[ModelConfig] = field(default_factory=list)
    """模型列表，第一个为默认模型。"""

    langfuse: LangfuseConfig = field(default_factory=LangfuseConfig)

    # 规划评估（LLM-as-Judge，旧配置，向后兼容）
    plan_evaluation: PlanEvaluationSettings = field(default_factory=PlanEvaluationSettings)

    # 评估器列表（推荐方式，见 EvaluatorSettings）
    evaluators: list[EvaluatorSettings] = field(default_factory=list)

    # 执行 agent 配置
    subagents: SubagentsSettings = field(default_factory=SubagentsSettings)

    # 数据存储（会话/线程持久化）
    storage_dir: str = ".deer-agent"
    database: DatabaseConfig = field(default_factory=DatabaseConfig)

    @classmethod
    def from_file(cls, path: str | None = None) -> AppConfig:
        """从 YAML 文件加载配置。"""
        config_path = Path(path) if path else _find_config_file()
        if config_path is None:
            raise FileNotFoundError("config.yaml not found. Create config.yaml (see config.example.yaml) or set AGENT_CONFIG_PATH.")

        with open(config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> AppConfig:
        models = [ModelConfig.from_dict(m) for m in data.get("models") or []]
        return cls(
            config_version=int(data.get("config_version", 1)),
            log_level=str(data.get("log_level", "INFO")).upper(),
            logging=LoggingConfig.from_dict(data.get("logging")),
            models=models,
            langfuse=LangfuseConfig.from_dict(data.get("langfuse")),
            plan_evaluation=PlanEvaluationSettings.from_dict(data.get("plan_evaluation")),
            evaluators=[EvaluatorSettings.from_dict(e) for e in data.get("evaluators") or []],
            subagents=SubagentsSettings.from_dict(data.get("subagents")),
            storage_dir=str(data.get("storage_dir", ".deer-agent")),
            database=DatabaseConfig.from_dict(data.get("database")),
        )

    def get_model_config(self, name: str) -> ModelConfig | None:
        for m in self.models:
            if m.name == name:
                return m
        return None

    def get_evaluator(self, name: str) -> EvaluatorSettings | None:
        """按名字取评估器配置。"""
        for e in self.evaluators:
            if e.name == name:
                return e
        return None

    @property
    def default_model(self) -> ModelConfig:
        return self.models[0] if self.models else ModelConfig(name="default")


_config: AppConfig | None = None


def get_app_config() -> AppConfig:
    """获取全局配置（惰性加载）。"""
    global _config
    if _config is None:
        _config = AppConfig.from_file()
    return _config


def reset_app_config() -> None:
    """重置配置（测试用）。"""
    global _config
    _config = None
