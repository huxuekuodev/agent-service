"""OpenAI 渠道（示例）。"""

from app.llm.base import LLMInstance, register

register(
    LLMInstance(
        name="openai",
        display_name="OpenAI",
        use="langchain_openai:ChatOpenAI",
        model="gpt-4o-mini",
        api_key_env="OPENAI_API_KEY",
        max_tokens=4096,
        timeout=300.0,
        max_retries=2,
    )
)
