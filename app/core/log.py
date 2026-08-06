"""日志配置（独立服务版）。

从 config.yaml 的 logging 段读取日志级别（惰性），失败时回退环境变量。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from loguru import logger

from app.core.context import trace_id_ctx_var

# 配置日志格式
log_format = "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <magenta>trace_id - {extra[trace_id]}</magenta> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"


def _resolve_log_level() -> tuple[str, str | None]:
    """从 config.yaml 读取日志级别和文件；失败回退环境变量。"""
    # 环境变量优先（显式覆盖）
    env_level = os.getenv("LOG_LEVEL")
    env_file = os.getenv("LOG_FILE")
    if env_level:
        return env_level.upper(), env_file

    # 从 config.yaml 读取
    try:
        import yaml
        from app.config import _find_config_file

        path = _find_config_file()
        if path:
            data = yaml.safe_load(open(path, encoding="utf-8")) or {}
            logging_cfg = data.get("logging") or {}
            level = str(logging_cfg.get("level", "INFO")).upper()
            file = logging_cfg.get("file")
            return level, file
    except Exception:
        pass
    return "INFO", env_file


LOG_LEVEL, LOG_FILE = _resolve_log_level()


# 注入 trace_id 到每条日志记录
def inject_trace_id(record):
    record["extra"]["trace_id"] = trace_id_ctx_var.get() or "-"


logger.remove()  # 移除默认输出配置

# 给日志打补丁，使其自动携带 trace_id
logger = logger.patch(inject_trace_id)

# 配置日志输出
logger.add(sink=sys.stdout, level=LOG_LEVEL, format=log_format)

# 可选文件输出
if LOG_FILE:
    path = Path(LOG_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    logger.add(sink=str(path), level=LOG_LEVEL, format=log_format, rotation="10 MB", encoding="utf-8")
