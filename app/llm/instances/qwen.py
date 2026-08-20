"""通义千问渠道（示例，含视觉能力）。

通义千问兼容 OpenAI 协议，使用 langchain_openai:ChatOpenAI 即可接入；
qwen_vl 实例 supports_vision=True，可作知识库图片转文字模型。
"""

from app.llm.base import LLMInstance, register

_QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

register(
    LLMInstance(
        name="qwen",
        display_name="通义千问",
        use="langchain_openai:ChatOpenAI",
        model="qwen-plus",
        api_key_env="DASHSCOPE_API_KEY",
        base_url=_QWEN_BASE_URL,
        max_tokens=8192,
        timeout=300.0,
        max_retries=2,
    )
)

register(
    LLMInstance(
        name="qwen_vl",
        display_name="通义千问视觉",
        use="langchain_openai:ChatOpenAI",
        model="qwen-vl-plus",
        api_key_env="DASHSCOPE_API_KEY",
        base_url=_QWEN_BASE_URL,
        supports_vision=True,
        max_tokens=4096,
        timeout=300.0,
        max_retries=2,
    )
)
