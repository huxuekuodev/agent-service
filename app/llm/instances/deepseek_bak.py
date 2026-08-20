"""DeepSeek 备用渠道（第二套 key，可选）。

key 变化 / 主 key 欠费时，把 config.yaml 的角色指向本实例即可切换：

    models:
      default: deepseek_bak
"""

from app.llm.base import LLMInstance, register

register(
    LLMInstance(
        name="deepseek_bak",
        display_name="DeepSeek 备用",
        use="langchain_deepseek:ChatDeepSeek",
        model="deepseek-chat",
        api_key_env="DEEPSEEK_API_KEY_2",
        max_tokens=8192,
        timeout=600.0,
        max_retries=2,
    )
)
