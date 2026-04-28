"""
logger.py — 结构化 JSON 日志模块
将交易日志写入 logs/trades.jsonl，便于后续统计和排障。

审计修复记录（v2）：
  [FIX-10] read_trade_logs 用 Path.read_text() 一次性读取全文件，
           文件过大时会 OOM → 改为逐行读取，只保留最后 limit 行
  [FIX-11] trade_log 并发写入无锁保护，多线程可能交错写入 → 加文件锁
  [FIX-12] 日志文件无大小限制，长期运行会无限增长
           → 使用 RotatingFileHandler（10MB × 5 个备份）
"""
import json
import logging
import os
import threading
from collections import deque
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from core.config import Config

# 确保日志目录存在
Path(Config.LOG_DIR).mkdir(parents=True, exist_ok=True)

# ── 标准日志配置（含滚动文件）────────────────────────────────────────────────
_system_log = os.path.join(Config.LOG_DIR, "system.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            _system_log,
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
            encoding="utf-8",
        ),
    ],
)

# ── 交易日志（JSONL）─────────────────────────────────────────────────────────
_TRADE_LOG_FILE = os.path.join(Config.LOG_DIR, "trades.jsonl")
_trade_log_lock = threading.Lock()  # [FIX-11] 写入锁


def trade_log(action: str, symbol: str, side: str, qty: float, price: float, **kwargs):
    """
    记录一笔交易到 trades.jsonl
    action: OPEN / CLOSE / SKIP
    """
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),  # 带时区的 ISO 8601
        "action": action,
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "price": price,
        **kwargs,
    }
    try:
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with _trade_log_lock:  # [FIX-11] 加锁保证原子写入
            # 自动创建父目录（_TRADE_LOG_FILE 可能在测试中被重定向）
            Path(_TRADE_LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
            with open(_TRADE_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception as e:
        logging.getLogger("trade_log").warning(f"写入交易日志失败: {e}")


def read_trade_logs(limit: int = 200) -> list:
    """
    读取最近 N 条交易日志。
    [FIX-10] 改为逐行读取，避免大文件 OOM。
    """
    path = Path(_TRADE_LOG_FILE)
    if not path.exists():
        return []

    # 使用 deque(maxlen=limit) 只保留最后 limit 行，内存占用恒定
    recent_lines: deque = deque(maxlen=limit)
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    recent_lines.append(line)
    except Exception as e:
        logging.getLogger("trade_log").warning(f"读取交易日志失败: {e}")
        return []

    records = []
    for line in recent_lines:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            pass  # 跳过损坏行

    return list(reversed(records))  # 最新的在前
