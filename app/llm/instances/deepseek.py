"""DeepSeek 官方渠道。"""

from app.llm.base import LLMInstance, register

register(
    LLMInstance(
        name="deepseek",
        display_name="DeepSeek 官方",
        use="langchain_deepseek:ChatDeepSeek",
        model="deepseek-chat",
        api_key_env="DEEPSEEK_API_KEY",
        max_tokens=8192,
        timeout=600.0,
        max_retries=2,
    )
)
