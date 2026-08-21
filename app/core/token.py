"""Token 计费辅助。

按 config.yaml ``token_pricing``（模型角色 → 输入/输出单价，元 / 1K tokens）
计算一次调用的费用；未配置单价的模型按 0 计费（不影响 token 统计）。
"""

from __future__ import annotations

__all__ = ["compute_token_cost"]


def compute_token_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """计算某模型角色一次调用的费用（元）。

    Args:
        model: 模型角色名（config.yaml models 的 key）。
        input_tokens: 输入 token 数。
        output_tokens: 输出 token 数。

    Returns:
        费用（元，保留 6 位小数）；未配置单价返回 0.0。
    """
    from app.config import get_app_config

    price = get_app_config().token_pricing.price_of(model)
    cost = (input_tokens * price.input_price + output_tokens * price.output_price) / 1000.0
    return round(cost, 6)
