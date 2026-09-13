"""统一响应信封。

所有 HTTP 接口（含 SSE 事件）一律返回如下结构::

    {
        "data": <业务返回的数据>,
        "msg":  <错误提示，成功为空字符串>,
        "status": <业务编码，200 成功，1000 起为自定义业务错误>,
    }

HTTP 状态码统一为 200；业务结果/错误通过 ``status`` 表达，由前端按业务编码分发。
"""

from __future__ import annotations

from typing import Any

# 成功编码
OK = 200

# ---------------------------------------------------------------------------
# 业务错误编码（自定义，从 1000 起）
# ---------------------------------------------------------------------------
# 通用
BAD_REQUEST = 1000  # 请求参数错误
NOT_FOUND = 1001  # 资源不存在（会话/线程）
INTERNAL_ERROR = 1002  # 服务内部错误

# 会话相关（1100 - 1199）
SESSION_NOT_FOUND = 1100  # 会话不存在
INTERRUPT_PENDING = 1103  # 会话有挂起的中断，需先经 /resume 答复
NO_PENDING_INTERRUPT = 1104  # 会话没有挂起的中断（无需/无法恢复）
INTERRUPT_MISMATCH = 1105  # 答复对应的中断 id 与当前挂起不一致（卡片已过期）
SESSION_EXISTS = 1101  # 会话已存在（创建时冲突）
SERVICE_NOT_READY = 1102  # AgentService 未初始化

# 认证相关（1200 - 1299）
UNAUTHORIZED = 1200  # 未登录 / 凭证缺失
TOKEN_EXPIRED = 1201  # 令牌过期（前端应静默刷新）
TOKEN_INVALID = 1202  # 令牌非法（需重新登录）
USER_EXISTS = 1203  # 用户已存在
PASSWORD_TOO_WEAK = 1204  # 密码强度不足
USER_DISABLED = 1205  # 用户被禁用/锁定
CREDENTIALS_INVALID = 1206  # 账号或密码错误
STORE_UNAVAILABLE = 1207  # 存储不可用（业务库未配置/连接失败）


def ok(data: Any = None, msg: str = "") -> dict[str, Any]:
    """构造成功响应。"""
    return {"data": data, "msg": msg, "status": OK}


def err(status: int, msg: str, data: Any = None) -> dict[str, Any]:
    """构造错误响应。"""
    return {"data": data, "msg": msg, "status": status}


class BizError(Exception):
    """业务异常：携带业务编码，由全局异常处理器转为统一信封。"""

    def __init__(self, status: int, msg: str, data: Any = None) -> None:
        super().__init__(msg)
        self.status = status
        self.msg = msg
        self.data = data
