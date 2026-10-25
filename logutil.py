"""
=========================================================
日志配置模块（logutil.py）
=========================================================
职责：统一配置项目日志写入文件（logs/app.log）+ 控制台。
=========================================================
设计说明：
  各业务模块（collector / llm_processor / scheduler / notifier /
  daily_report / qa_service）都用 logging.getLogger("模块名") 拿 logger，
  但都没有配置 handler。默认情况下日志只进 stderr（uvicorn 控制台），
  服务被后台拉起时 stdout/stderr 不可见，排障时完全抓瞎。

  这里在 root logger 上挂一个 RotatingFileHandler，所有模块的日志
  都会落盘到 logs/app.log（root 的 handler 会被子 logger 继承）。
  幂等：重复调用不会重复挂 handler（服务重启、CLI 多入口都安全）。

  文件位置：项目根目录 logs/app.log（与 .env / uvicorn.log 同级）。
  轮转：单文件超 10MB 切分，保留最近 5 份（logs/app.log.1 ... app.log.5），
  避免日志无限膨胀占满磁盘。
=========================================================
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ---------- 日志路径 ----------
LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_FILE = LOG_DIR / "app.log"

# 单文件最大 10MB，超出后轮转，保留 5 份历史
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 5


def setup_logging(level: int = logging.INFO) -> Path:
    """
    配置 root logger 输出到 logs/app.log（追加）和控制台。

    幂等：已挂过同名文件 handler 时直接返回，不会重复添加。
    返回日志文件路径（供需要打印"日志写到哪"的场景使用）。
    """
    # 确保目录存在（首次运行创建 logs/）
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()

    # 幂等检查：已有指向相同文件的 handler 就不再挂
    for h in root.handlers:
        if isinstance(h, RotatingFileHandler) and h.baseFilename == str(LOG_FILE):
            return LOG_FILE

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 文件 handler（业务日志落盘）
    file_handler = RotatingFileHandler(
        str(LOG_FILE),
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    root.addHandler(file_handler)

    # 控制台 handler：只在没有 StreamHandler 时补一个，避免重复刷屏
    has_console = any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
        for h in root.handlers
    )
    if not has_console:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        console.setLevel(level)
        root.addHandler(console)

    root.setLevel(level)

    return LOG_FILE


def get_log_file() -> Path:
    """返回日志文件路径（供日志展示/统计用）。"""
    return LOG_FILE
