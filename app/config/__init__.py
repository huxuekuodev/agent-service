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
    """模型角色配置：角色名 + 引用的 LLM 实例名（app/llm/instances/）。

    config.yaml 的 ``models`` 段为 {角色名: LLM实例名} 映射，模型类、模型名、
    API Key、端点等完整参数只在 app/llm/instances/ 的实例里配置一处。
    """

    name: str
    """角色名（config.yaml models 的 key，代码按它取模型）。"""
    instance: str
    """LLM 实例名（app/llm/instances/ 中注册的实例）。"""


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
class ToolConfig:
    """第三方工具配置（config.yaml 的 ``tools`` 列表项）。

    字段语义：
      - ``name``: 工具唯一名（即 agent 看到的工具名）。
      - ``use``: 工具的可导入路径，可指向：
          1) 已实例化的 BaseTool 对象（如 ``@tool`` 装饰的函数）；
          2) 工具工厂函数（接收 ``extra`` 关键字参数，返回 BaseTool）；
          3) BaseTool 子类（用 ``extra`` 实例化）。
      - ``category``: 业务分类（web / knowledge / ...），仅用于组织代码。
      - ``allowed_agents``: 可用 agent 名列表；为空表示所有 agent 可用。
      - ``enabled``: 总开关。
      - ``extra``: 透传给工具工厂/类的额外参数（API Key、endpoint 等）。
    """

    name: str
    display_name: str = ""
    use: str = ""
    category: str = "general"
    allowed_agents: list[str] = field(default_factory=list)
    enabled: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict | None) -> ToolConfig:
        d = d or {}
        extra = dict(d.get("extra") or {})
        extra = {k: _resolve_env(v) for k, v in extra.items()}
        return cls(
            name=str(d.get("name", "")),
            display_name=str(d.get("display_name", "")),
            use=str(d.get("use", "")),
            category=str(d.get("category", "general")),
            allowed_agents=list(d.get("allowed_agents") or []),
            enabled=bool(d.get("enabled", True)),
            extra=extra,
        )


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
class TrackingConfig:
    """打点（tracking）配置：独立数据日志，不写入执行日志（app.log）。"""

    output: str = "logs/tracking.data"
    """数据日志文件路径；每行一条打点（ISO 时间 + base64(protobuf)），供后续数据链路消费。"""

    @classmethod
    def from_dict(cls, d: dict | None) -> TrackingConfig:
        d = d or {}
        return cls(output=str(d.get("output", "logs/tracking.data")) or "logs/tracking.data")


@dataclass
class ModelTokenPrice:
    """单个模型的 token 单价（元 / 1K tokens）。"""

    input_price: float = 0.0
    """输入 token 单价（元 / 1K）。"""
    output_price: float = 0.0
    """输出 token 单价（元 / 1K）。"""


@dataclass
class TokenPricingConfig:
    """Token 计费配置（config.yaml ``token_pricing``）。

    按模型角色名（``models`` 段 key）配置输入/输出单价；未配置的模型按 0 计费。
    """

    prices: dict[str, ModelTokenPrice] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict | None) -> TokenPricingConfig:
        d = d or {}
        prices: dict[str, ModelTokenPrice] = {}
        for model, price in d.items():
            if isinstance(price, dict):
                prices[str(model)] = ModelTokenPrice(
                    input_price=float(price.get("input_price", 0.0) or 0.0),
                    output_price=float(price.get("output_price", 0.0) or 0.0),
                )
        return cls(prices=prices)

    def price_of(self, model: str) -> ModelTokenPrice:
        """取某模型角色的单价（未配置返回 0 价）。"""
        return self.prices.get(model, ModelTokenPrice())


@dataclass
class SkillSandboxConfig:
    """Skill 沙箱配置（config.yaml ``skills.sandbox``）。

    三个 E2B 变量支持环境变量兜底（.env / 宿主环境）：
      - ``E2B_TEMPLATE``：模板名（config 未填 template 时使用）
      - ``E2B_API_KEY``：SDK 鉴权（SkillSandbox 创建时校验）
      - ``E2B_DOMAIN``：自托管域名（SDK 自行读取）

    仅当可选依赖 ``e2b-code-interpreter`` 已安装、template 非空、宿主环境存在 E2B_API_KEY
    时沙箱可用。skill 自带的脚本在沙箱内运行，调用注册工具的步骤仍在本地执行。
    """

    enabled: bool = False
    template: str = ""
    """E2B 沙箱模板名（config 未填时读环境变量 E2B_TEMPLATE）。"""
    timeout: int = 3600
    """沙箱最长存活秒数。"""
    command_timeout: int = 120
    """单条命令默认超时秒数。"""
    max_output_chars: int = 30000
    """单次执行 stdout/stderr 回传给 LLM 的最大字符数（0 = 不截断）。"""
    session_ttl_seconds: int = 1800
    """沙箱会话空闲存活秒数：超时未使用则自动销毁（LLM 也可主动 sandbox_close）。"""
    max_sessions: int = 4
    """同时存活的沙箱会话数上限（超出时回收最久未用者）。"""
    env_keys: list[str] = field(default_factory=list)
    """允许从宿主环境注入沙箱的环境变量白名单（如 QWEATHER_API_KEY）。"""
    exclude_patterns: list[str] = field(default_factory=lambda: [".env*", "*.pem", "*.key", "*.p12", ".git/*", "__pycache__/*", "*.pyc", ".venv/*", "node_modules/*"])
    """同步 skill 目录到沙箱时跳过的敏感/无关文件（fnmatch 匹配相对路径）。"""

    @classmethod
    def from_dict(cls, d: dict | None) -> SkillSandboxConfig:
        d = d or {}
        template = str(d.get("template") or os.getenv("E2B_TEMPLATE", "") or "")
        # enabled：config 显式给出则以其为准；未给出时若存在模板（config 或 E2B_TEMPLATE）则自动开启
        explicit_enabled = d.get("enabled")
        enabled = bool(explicit_enabled) if explicit_enabled is not None else bool(template)
        return cls(
            enabled=enabled,
            template=template,
            timeout=int(d.get("timeout", 3600)),
            command_timeout=int(d.get("command_timeout", 120)),
            max_output_chars=int(d.get("max_output_chars", 30000)),
            session_ttl_seconds=max(60, int(d.get("session_ttl_seconds", 1800) or 1800)),
            max_sessions=max(1, int(d.get("max_sessions", 4) or 4)),
            env_keys=list(d.get("env_keys") or []),
            exclude_patterns=list(d.get("exclude_patterns") or cls().exclude_patterns),
        )


@dataclass
class SkillsConfig:
    """SKILL 能力配置（config.yaml ``skills`` 段）。"""

    enabled: bool = True
    """总开关：关闭后规划节点不注入技能索引/不生成 skill_probe，执行 agent 不注入技能工具链
    （用于对比「用/不用 skill」的 token 消耗与行为差异）。"""
    dir: str = "skills"
    """技能仓库目录（每子目录一个 skill，入口 SKILL.md）。"""
    cleanup_default: str = "confirm"
    """产物清理默认等级：auto / confirm / forbidden（人工介入用）。"""
    max_candidates: int = 3
    """一次请求最多候选 skill 数。"""
    sandbox: SkillSandboxConfig = field(default_factory=SkillSandboxConfig)

    @classmethod
    def from_dict(cls, d: dict | None) -> SkillsConfig:
        d = d or {}
        return cls(
            enabled=bool(d.get("enabled", True)),
            dir=str(d.get("dir", "skills")) or "skills",
            cleanup_default=str(d.get("cleanup_default", "confirm")) or "confirm",
            max_candidates=int(d.get("max_candidates", 3)),
            sandbox=SkillSandboxConfig.from_dict(d.get("sandbox")),
        )


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
class VoiceConfig:
    """语音通话配置（config.yaml ``voice``）。

    链路：浏览器原生语音识别（STT，见 docs/语音通话方案.md）→ 现有 agent（同一会话/上下文）
    → **服务端按句流式 TTS** → 浏览器排队播放。TTS 走 OpenAI 兼容的 ``/audio/speech``
    （当前用硅基流动，复用已有 SILICONFLOW_KEY，零新增依赖）。
    """

    enabled: bool = True
    base_url: str = "https://api.siliconflow.cn/v1"
    """OpenAI 兼容语音接口地址。"""
    api_key_env: str = "SILICONFLOW_KEY"
    """语音接口 Key 的环境变量名（不存明文）。"""
    tts_model: str = "FunAudioLLM/CosyVoice2-0.5B"
    tts_voice: str = "FunAudioLLM/CosyVoice2-0.5B:alex"
    chunk_max_chars: int = 60
    """单块播报文本上限（越大越少切、但单块合成更慢）。"""
    chunk_min_chars: int = 12
    """单块下限：更短的句子会并到下一句，避免一顿一顿。"""
    first_chunk_max_chars: int = 30
    """首块上限：更短 → 第一声更快（通话体感关键）。"""
    max_chunks: int = 20
    """最多合成几块（超出丢弃并提示，完整文字仍在前端）。"""
    timeout: float = 45.0
    """单次 TTS 请求超时秒数。"""

    @classmethod
    def from_dict(cls, d: dict | None) -> VoiceConfig:
        d = d or {}
        return cls(
            enabled=bool(d.get("enabled", True)),
            base_url=str(_resolve_env(d.get("base_url", "https://api.siliconflow.cn/v1")) or "https://api.siliconflow.cn/v1"),
            api_key_env=str(d.get("api_key_env", "SILICONFLOW_KEY") or "SILICONFLOW_KEY"),
            tts_model=str(d.get("tts_model", "FunAudioLLM/CosyVoice2-0.5B") or "FunAudioLLM/CosyVoice2-0.5B"),
            tts_voice=str(d.get("tts_voice", "FunAudioLLM/CosyVoice2-0.5B:alex") or "FunAudioLLM/CosyVoice2-0.5B:alex"),
            chunk_max_chars=max(20, int(d.get("chunk_max_chars", 60) or 60)),
            chunk_min_chars=max(4, int(d.get("chunk_min_chars", 12) or 12)),
            first_chunk_max_chars=max(10, int(d.get("first_chunk_max_chars", 30) or 30)),
            max_chunks=max(1, int(d.get("max_chunks", 20) or 20)),
            timeout=float(d.get("timeout", 45.0) or 45.0),
        )

    @property
    def api_key(self) -> str:
        """按 api_key_env 从环境变量取 Key（空 = 未配置，语音不可用）。"""
        import os

        return os.getenv(self.api_key_env, "") if self.api_key_env else ""


@dataclass
class ApprovalConfig:
    """人工确认（interrupt）配置（config.yaml ``approval``）。

    计划生成后是否等用户确认再执行；确认卡片里是否展示系统内部任务。
    关闭 ``plan_review`` 时图不会中断（自动化/压测/脚本调用场景用）。
    """

    plan_review: bool = True
    """计划生成后是否中断等待用户确认（False = 直接执行）。"""
    show_internal_tasks: bool = False
    """确认卡片是否展示系统内部任务（skill_probe 等）；默认不展示。"""
    allow_feedback: bool = True
    """是否允许用户只留言不选选项（留空即照此执行）。"""

    @classmethod
    def from_dict(cls, d: dict | None) -> ApprovalConfig:
        d = d or {}
        return cls(
            plan_review=bool(d.get("plan_review", True)),
            show_internal_tasks=bool(d.get("show_internal_tasks", False)),
            allow_feedback=bool(d.get("allow_feedback", True)),
        )


@dataclass
class BusinessDatabaseConfig:
    """业务库配置（config.yaml ``business_database``）。

    与 ``database``（checkpointer）**分开**：业务库存放用户/会话/消息/监控配置，
    是"用户可见事实"的长期真相；checkpointer 只管 agent 运行态。
    建表见 ``deploy/sql/business_schema.sql``，设计见 ``docs/业务库表结构设计.md``。
    """

    postgres_url: str = ""
    """业务库连接 URL（建议 .env 用 BUSINESS_DATABASE_URL 注入）；为空表示业务库未接入。"""
    sessions_page_size: int = 20
    """会话列表默认分页大小。"""
    messages_page_size: int = 50
    """历史消息默认分页大小。"""
    fail_fast: bool = False
    """消息落库失败是否阻塞对话：False 仅记日志（对话优先），True 直接报错。"""

    @classmethod
    def from_dict(cls, d: dict | None) -> BusinessDatabaseConfig:
        d = d or {}
        return cls(
            postgres_url=_resolve_env(d.get("postgres_url", "")),
            sessions_page_size=max(1, int(d.get("sessions_page_size", 20) or 20)),
            messages_page_size=max(1, int(d.get("messages_page_size", 50) or 50)),
            fail_fast=bool(d.get("fail_fast", False)),
        )

    @property
    def enabled(self) -> bool:
        """是否已配置业务库（未配置时接口降级：会话/历史不可用）。"""
        return bool(self.postgres_url.strip())


@dataclass
class AuthConfig:
    """认证配置（config.yaml ``auth``）：账号密码 + JWT。

    - access token：短时效、无状态校验（只验签 + exp），随请求头 ``Authorization: Bearer`` 传递；
    - refresh token：长时效、**落库** ``user_tokens``（jti + SHA-256 哈希），支持撤销与旋转
      （刷新时旧 token 立即置 ``revoked_at``，防重放）。
    """

    enabled: bool = True
    """认证总开关；关闭时接口不校验身份（仅本地调试，禁止生产关闭）。"""
    jwt_secret: str = ""
    """HS256 签名密钥（.env 用 JWT_SECRET 注入）；为空则认证不可用。"""
    jwt_algorithm: str = "HS256"
    access_ttl_minutes: int = 120
    refresh_ttl_days: int = 30
    password_min_length: int = 8
    issuer: str = "deer-agent"

    @classmethod
    def from_dict(cls, d: dict | None) -> AuthConfig:
        d = d or {}
        secret = str(_resolve_env(d.get("jwt_secret")) or "")
        enabled_raw = d.get("enabled")
        return cls(
            # 未显式配置 enabled 时：有密钥即启用，避免"配了密钥却忘记开开关"
            enabled=bool(enabled_raw) if enabled_raw is not None else bool(secret),
            jwt_secret=secret,
            jwt_algorithm=str(d.get("jwt_algorithm", "HS256") or "HS256").upper(),
            access_ttl_minutes=max(1, int(d.get("access_ttl_minutes", 120) or 120)),
            refresh_ttl_days=max(1, int(d.get("refresh_ttl_days", 30) or 30)),
            password_min_length=max(6, int(d.get("password_min_length", 8) or 8)),
            issuer=str(d.get("issuer", "deer-agent") or "deer-agent"),
        )

    @property
    def usable(self) -> bool:
        """认证是否真正可用（开关打开且密钥已配置）。"""
        return self.enabled and bool(self.jwt_secret)


@dataclass
class YuqueConfig:
    """语雀数据源配置。"""

    enabled: bool = False
    token: str = ""
    """语雀个人令牌（建议 .env 用 YUQUE_TOKEN 注入）。"""
    login: str = ""
    """用于列出用户/组织知识库的登录名；为空时路由需显式传 repos。"""
    group_repos: bool = False
    """True 时用 /groups/{login}/repos 拉组织知识库，否则拉用户知识库。"""
    namespaces: list[str] = field(default_factory=list)
    """需要同步的知识库 namespace（org/repo）白名单；为空表示拉全部。"""

    @classmethod
    def from_dict(cls, d: dict | None) -> YuqueConfig:
        d = d or {}
        return cls(
            enabled=bool(d.get("enabled", False)),
            token=_resolve_env(d.get("token")),
            login=_resolve_env(d.get("login")),
            group_repos=bool(d.get("group_repos", False)),
            namespaces=list(d.get("namespaces") or []),
        )


@dataclass
class ElasticsearchConfig:
    """ElasticSearch 向量库配置（见 docs/RAG_方案.md）。"""

    url: str = "http://localhost:9200"
    index: str = "rag_docs"
    dims: int = 1536
    """embedding 向量维度，必须与 embedding 模型一致。"""
    username: str | None = None
    password: str | None = None
    verify_certs: bool = True
    reindex_on_ingest: bool = False
    """每次摄取前是否删除重建索引（简化全量同步）。"""

    @classmethod
    def from_dict(cls, d: dict | None) -> ElasticsearchConfig:
        d = d or {}
        return cls(
            url=_resolve_env(d.get("url", "http://localhost:9200")) or "http://localhost:9200",
            index=_resolve_env(d.get("index", "rag_docs")) or "rag_docs",
            dims=int(d.get("dims", 1536)),
            username=_resolve_env(d.get("username")),
            password=_resolve_env(d.get("password")),
            verify_certs=bool(d.get("verify_certs", True)),
            reindex_on_ingest=bool(d.get("reindex_on_ingest", False)),
        )


@dataclass
class EmbeddingConfig:
    """Embedding 模型配置（向量化 chunk）。"""

    model: str = "text-embedding-3-small"
    """embedding 模型名。"""
    api_key: str = ""
    """embedding API Key（.env 用 EMBEDDING_API_KEY 注入）。"""
    base_url: str | None = None
    dimensions: int = 1536
    """向量维度，与 ElasticsearchConfig.dims 一致。"""

    @classmethod
    def from_dict(cls, d: dict | None) -> EmbeddingConfig:
        d = d or {}
        return cls(
            model=_resolve_env(d.get("model", "text-embedding-3-small")) or "text-embedding-3-small",
            api_key=_resolve_env(d.get("api_key")),
            base_url=_resolve_env(d.get("base_url")),
            dimensions=int(d.get("dimensions", 1536)),
        )


@dataclass
class KnowledgeIngestConfig:
    """知识库摄取流水线配置。"""

    enabled: bool = False
    """知识库摄取总开关。"""
    vision_model: str = ""
    """图片转文字用的视觉模型名（对应 models 列表里 supports_vision: true 的条目）。"""
    chunk_size: int = 2000
    """最大 chunk 字符数（按标题层级语义切分）。"""
    overlap: int = 200
    """chunk 相邻重叠字符数。"""
    download_images: bool = True
    """是否下载文档内图片并转文字；关闭时图片仅保留占位标记。"""
    image_placeholder: str = "![image]({url})"
    """图片占位标记模板。"""
    auto_interval_seconds: int = 0
    """自动同步间隔（秒）；0 表示关闭自动同步。"""
    parallel: int = 4
    """文档并发处理数。"""

    @classmethod
    def from_dict(cls, d: dict | None) -> KnowledgeIngestConfig:
        d = d or {}
        return cls(
            enabled=bool(d.get("enabled", False)),
            vision_model=_resolve_env(d.get("vision_model")) or "",
            chunk_size=int(d.get("chunk_size", 2000)),
            overlap=int(d.get("overlap", 200)),
            download_images=bool(d.get("download_images", True)),
            image_placeholder=str(d.get("image_placeholder", "![image]({url})")),
            auto_interval_seconds=int(d.get("auto_interval_seconds", 0)),
            parallel=max(1, int(d.get("parallel", 4))),
        )


@dataclass
class AppConfig:
    """独立服务的全局配置。"""

    config_version: int = 1
    log_level: str = "INFO"
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    # 打点（独立数据日志，不写入执行日志 app.log）
    tracking: TrackingConfig = field(default_factory=TrackingConfig)

    # Token 计费（可选）：按模型角色配置输入/输出单价（元 / 1K tokens）
    token_pricing: TokenPricingConfig = field(default_factory=TokenPricingConfig)

    # SKILL 能力（标准 SOP 技能库 + E2B 沙箱执行）
    skills: SkillsConfig = field(default_factory=SkillsConfig)

    models: dict[str, str] = field(default_factory=dict)
    """模型角色 → LLM 实例名（见 app/llm/instances/，每个实例只配置一套）。"""

    langfuse: LangfuseConfig = field(default_factory=LangfuseConfig)

    # 数据源：语雀
    yuque: YuqueConfig = field(default_factory=YuqueConfig)

    # 知识库摄取流水线
    ingest: KnowledgeIngestConfig = field(default_factory=KnowledgeIngestConfig)

    # ElasticSearch 向量库
    elasticsearch: ElasticsearchConfig = field(default_factory=ElasticsearchConfig)

    # Embedding 模型
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)

    # 规划评估（LLM-as-Judge，旧配置，向后兼容）
    plan_evaluation: PlanEvaluationSettings = field(default_factory=PlanEvaluationSettings)

    # 评估器列表（推荐方式，见 EvaluatorSettings）
    evaluators: list[EvaluatorSettings] = field(default_factory=list)

    # 执行 agent 配置
    subagents: SubagentsSettings = field(default_factory=SubagentsSettings)

    # 第三方工具配置（供 agent 使用，按业务二分类组织）
    tools: list[ToolConfig] = field(default_factory=list)

    # 数据存储（会话/线程持久化）
    storage_dir: str = ".deer-agent"
    database: DatabaseConfig = field(default_factory=DatabaseConfig)

    # 业务库（用户/会话/消息/监控配置；与 checkpointer 库分离）
    business_database: BusinessDatabaseConfig = field(default_factory=BusinessDatabaseConfig)

    # 认证（账号密码 + JWT）
    auth: AuthConfig = field(default_factory=AuthConfig)

    # 人工确认（计划确认中断）
    approval: ApprovalConfig = field(default_factory=ApprovalConfig)

    # 语音通话（ASR + TTS）
    voice: VoiceConfig = field(default_factory=VoiceConfig)

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
        models_raw = data.get("models") or {}
        if not isinstance(models_raw, dict):
            raise ValueError("config.yaml 的 models 段必须为 {角色名: LLM实例名} 映射（实例见 app/llm/instances/）")
        models = {str(k): str(v) for k, v in models_raw.items()}
        return cls(
            config_version=int(data.get("config_version", 1)),
            log_level=str(data.get("log_level", "INFO")).upper(),
            logging=LoggingConfig.from_dict(data.get("logging")),
            tracking=TrackingConfig.from_dict(data.get("tracking")),
            token_pricing=TokenPricingConfig.from_dict(data.get("token_pricing")),
            skills=SkillsConfig.from_dict(data.get("skills")),
            models=models,
            langfuse=LangfuseConfig.from_dict(data.get("langfuse")),
            plan_evaluation=PlanEvaluationSettings.from_dict(data.get("plan_evaluation")),
            evaluators=[EvaluatorSettings.from_dict(e) for e in data.get("evaluators") or []],
            subagents=SubagentsSettings.from_dict(data.get("subagents")),
            tools=[ToolConfig.from_dict(t) for t in data.get("tools") or []],
            storage_dir=str(data.get("storage_dir", ".deer-agent")),
            database=DatabaseConfig.from_dict(data.get("database")),
            business_database=BusinessDatabaseConfig.from_dict(data.get("business_database")),
            auth=AuthConfig.from_dict(data.get("auth")),
            approval=ApprovalConfig.from_dict(data.get("approval")),
            voice=VoiceConfig.from_dict(data.get("voice")),
            yuque=YuqueConfig.from_dict(data.get("yuque")),
            ingest=KnowledgeIngestConfig.from_dict(data.get("ingest")),
            elasticsearch=ElasticsearchConfig.from_dict(data.get("elasticsearch")),
            embedding=EmbeddingConfig.from_dict(data.get("embedding")),
        )

    def get_model_config(self, name: str) -> ModelConfig | None:
        """按角色名返回模型配置（角色 → 实例引用）；未配置返回 None。"""
        instance = self.models.get(name)
        if instance is None:
            return None
        return ModelConfig(name=name, instance=instance)

    def get_evaluator(self, name: str) -> EvaluatorSettings | None:
        """按名字取评估器配置。"""
        for e in self.evaluators:
            if e.name == name:
                return e
        return None

    def get_vision_model(self) -> str | None:
        """返回视觉模型角色名（图片转文字用）。

        优先 ``ingest.vision_model`` 指定的角色；否则返回 ``models`` 中第一个
        指向 supports_vision 实例的角色；都没有则返回 None。
        """
        from app.llm.base import get_llm_instance

        if self.ingest.vision_model and self.ingest.vision_model in self.models:
            return self.ingest.vision_model
        for role, instance_name in self.models.items():
            try:
                if get_llm_instance(instance_name).supports_vision:
                    return role
            except ValueError:
                # 实例未注册：跳过，交由 create_chat_model 报清晰错误
                continue
        return None

    @property
    def default_model_name(self) -> str:
        """默认角色名：优先 ``models.default``，否则第一个角色。"""
        if "default" in self.models:
            return "default"
        return next(iter(self.models), "")


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
