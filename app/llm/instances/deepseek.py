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


register(
    LLMInstance(
        name="deepseek_v4",
        display_name="Deepseek V4",
        use="langchain_deepseek:ChatDeepSeek",
        model="deepseek-flash",
        api_key_env="DEEPSEEK_API_KEY",
        max_tokens=8192,
        timeout=300.0,
        max_retries=2,
        supports_thinking=True,
        # V4 系列（deepseek-flash / deepseek-v4-pro）默认走思考模式：只接受
        # tool_choice=auto|none，强制值会 400「Thinking mode does not support this
        # tool_choice」，response_format=json_schema 也不可用。
        # → 因此计划这类结构化产出走**普通工具**（app/agents/plan/protocol.py 的
        #   submit_plan）而不是 LangChain 的结构化输出，业务契约不依赖渠道特性。
    )
)
