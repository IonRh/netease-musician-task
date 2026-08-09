"""统一日志：文件轮转 + 控制台。供全 app 复用。"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from app.config import LOG_DIR

logger = logging.getLogger("netease_app")

class UvicornStyleFormatter(logging.Formatter):
    """模拟 Uvicorn 的 levelprefix 对齐格式。"""

    def __init__(self) -> None:
        super().__init__("%(message)s")

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        timestamp = self.formatTime(record)
        level_prefix = f"{record.levelname}:"
        return f"{level_prefix:<9} {timestamp} {message}"


if not logger.handlers:
    logger.setLevel(logging.INFO)
    # 与 Uvicorn 控制台日志保持一致的级别前缀和空格对齐。
    fmt = UvicornStyleFormatter()

    fh = RotatingFileHandler(
        os.path.join(LOG_DIR, "app.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
