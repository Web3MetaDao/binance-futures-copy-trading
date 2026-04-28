"""
db.py — SQLite 数据库持久化模块

替代 trades.jsonl 文件，提供结构化存储和复杂查询能力。
支持：
  - 交易记录存储（含交易员来源、评分、PnL）
  - 按交易员统计绩效（胜率/盈亏比/最大回撤）
  - 权益曲线历史
  - 多账户隔离

审计修复记录：
  FIX-F01: _conn 对所有操作（含只读 SELECT）都调用 conn.commit()，
            产生不必要的磁盘写入
            → 增加 write 参数，只读操作不 commit
  FIX-F02: get_recent_trades / get_equity_curve / get_summary 等读操作
            未加 _lock，与写操作并发时可能读到脏数据
            → 所有操作统一加 _lock
  FIX-F03: trader_stats_cache 表定义但从未使用，get_trader_stats 每次全量查询
            → 利用缓存表实现 TTL=1小时 的查询结果缓存
"""
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import List, Optional

from core.config import Config

logger = logging.getLogger("db")

_DB_PATH = os.path.join(Config.LOG_DIR, "copytrader.db")
_STATS_CACHE_TTL = 3600  # trader_stats 缓存 1 小时


class DatabaseManager:
    """SQLite 数据库管理器（线程安全）"""

    def __init__(self, db_path: str = None):
        self._db_path = db_path or _DB_PATH
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self, write: bool = False):
        """
        线程安全的连接上下文管理器
        [FIX-F01] write=True 时才 commit，只读操作不产生不必要的磁盘写入
        """
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            if write:
                conn.commit()
        except Exception:
            if write:
                conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self):
        """初始化数据库表结构"""
        with self._lock:
            with self._conn(write=True) as conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS trades (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts          INTEGER NOT NULL,
                        account     TEXT NOT NULL DEFAULT 'default',
                        symbol      TEXT NOT NULL,
                        side        TEXT NOT NULL,
                        action      TEXT NOT NULL,
                        qty         REAL NOT NULL,
                        price       REAL NOT NULL,
                        pnl_pct     REAL,
                        pnl_usdt    REAL,
                        score       REAL,
                        trader      TEXT,
                        regime      TEXT,
                        compound_base REAL
                    );

                    CREATE TABLE IF NOT EXISTS equity_curve (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts          INTEGER NOT NULL,
                        account     TEXT NOT NULL DEFAULT 'default',
                        balance     REAL NOT NULL,
                        open_pnl    REAL NOT NULL DEFAULT 0
                    );

                    CREATE TABLE IF NOT EXISTS trader_stats_cache (
                        trader       TEXT NOT NULL,
                        account      TEXT NOT NULL DEFAULT 'default',
                        cached_at    INTEGER NOT NULL,
                        win_rate     REAL,
                        pl_ratio     REAL,
                        max_drawdown REAL,
                        total_trades INTEGER,
                        PRIMARY KEY (trader, account)
                    );

                    CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
                    CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);
                    CREATE INDEX IF NOT EXISTS idx_trades_trader ON trades(trader);
                    CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_curve(ts);
                """)
        logger.info(f"数据库已初始化: {self._db_path}")

    # ── 交易记录 ──────────────────────────────────────────────────────────────

    def insert_trade(self, symbol: str, side: str, action: str,
                     qty: float, price: float, pnl_pct: float = None,
                     pnl_usdt: float = None, score: float = None,
                     trader: str = None, regime: str = None,
                     compound_base: float = None,
                     account: str = "default"):
        """插入一条交易记录"""
        ts = int(time.time() * 1000)
        with self._lock:
            with self._conn(write=True) as conn:
                conn.execute("""
                    INSERT INTO trades
                    (ts, account, symbol, side, action, qty, price,
                     pnl_pct, pnl_usdt, score, trader, regime, compound_base)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (ts, account, symbol, side, action, qty, price,
                      pnl_pct, pnl_usdt, score, trader, regime, compound_base))

    def get_recent_trades(self, limit: int = 200, account: str = "default") -> List[dict]:
        """获取最近 N 条交易记录"""
        with self._lock:  # [FIX-F02] 加锁保护读操作
            with self._conn(write=False) as conn:
                rows = conn.execute("""
                    SELECT * FROM trades WHERE account=?
                    ORDER BY ts DESC LIMIT ?
                """, (account, limit)).fetchall()
        return [dict(r) for r in rows]

    def get_trader_stats(self, trader: str, lookback_days: int = 30,
                         account: str = "default") -> Optional[dict]:
        """
        计算指定交易员的历史绩效统计
        [FIX-F03] 优先从缓存表读取（TTL=1小时），避免每次全量查询
        返回 {win_rate, pl_ratio, max_drawdown, total_trades}
        """
        now_ts = int(time.time() * 1000)

        # [FIX-F03] 尝试读取缓存
        with self._lock:
            with self._conn(write=False) as conn:
                cached = conn.execute("""
                    SELECT * FROM trader_stats_cache
                    WHERE trader=? AND account=?
                """, (trader, account)).fetchone()

        if cached:
            age_seconds = (now_ts - cached["cached_at"]) / 1000
            if age_seconds < _STATS_CACHE_TTL:
                return {
                    "win_rate": cached["win_rate"],
                    "pl_ratio": cached["pl_ratio"],
                    "max_drawdown": cached["max_drawdown"],
                    "total_trades": cached["total_trades"],
                }

        # 缓存未命中，全量查询
        since_ts = int((time.time() - lookback_days * 86400) * 1000)
        with self._lock:
            with self._conn(write=False) as conn:
                rows = conn.execute("""
                    SELECT pnl_pct FROM trades
                    WHERE trader=? AND account=? AND action='CLOSE'
                    AND ts >= ? AND pnl_pct IS NOT NULL
                    ORDER BY ts ASC
                """, (trader, account, since_ts)).fetchall()

        if not rows:
            return None

        pnls = [r["pnl_pct"] for r in rows]
        total = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        losses = [abs(p) for p in pnls if p < 0]
        gains = [p for p in pnls if p > 0]
        win_rate = wins / total if total > 0 else 0.5
        avg_win = sum(gains) / len(gains) if gains else 0.01
        avg_loss = sum(losses) / len(losses) if losses else 0.01
        pl_ratio = avg_win / avg_loss if avg_loss > 0 else 1.0

        # 计算最大回撤
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in pnls:
            cumulative += p
            if cumulative > peak:
                peak = cumulative
            dd = (peak - cumulative) / (1 + abs(peak))
            if dd > max_dd:
                max_dd = dd

        result = {
            "win_rate": win_rate,
            "pl_ratio": pl_ratio,
            "max_drawdown": max_dd,
            "total_trades": total,
        }

        # [FIX-F03] 写入缓存
        with self._lock:
            with self._conn(write=True) as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO trader_stats_cache
                    (trader, account, cached_at, win_rate, pl_ratio, max_drawdown, total_trades)
                    VALUES (?,?,?,?,?,?,?)
                """, (trader, account, now_ts,
                      win_rate, pl_ratio, max_dd, total))

        return result

    # ── 权益曲线 ──────────────────────────────────────────────────────────────

    def insert_equity(self, balance: float, open_pnl: float = 0.0,
                      account: str = "default"):
        """插入权益曲线数据点"""
        ts = int(time.time() * 1000)
        with self._lock:
            with self._conn(write=True) as conn:
                conn.execute("""
                    INSERT INTO equity_curve (ts, account, balance, open_pnl)
                    VALUES (?,?,?,?)
                """, (ts, account, balance, open_pnl))

    def get_equity_curve(self, limit: int = 500, account: str = "default") -> List[dict]:
        """获取权益曲线历史"""
        with self._lock:  # [FIX-F02] 加锁保护读操作
            with self._conn(write=False) as conn:
                rows = conn.execute("""
                    SELECT ts, balance, open_pnl FROM equity_curve
                    WHERE account=? ORDER BY ts DESC LIMIT ?
                """, (account, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    # ── 汇总统计 ──────────────────────────────────────────────────────────────

    def get_summary(self, account: str = "default") -> dict:
        """获取账户汇总统计"""
        with self._lock:  # [FIX-F02] 加锁保护读操作
            with self._conn(write=False) as conn:
                total = conn.execute(
                    "SELECT COUNT(*) as n FROM trades WHERE account=? AND action='CLOSE'",
                    (account,)
                ).fetchone()["n"]
                wins = conn.execute(
                    "SELECT COUNT(*) as n FROM trades WHERE account=? "
                    "AND action='CLOSE' AND pnl_pct > 0",
                    (account,)
                ).fetchone()["n"]
                total_pnl = conn.execute(
                    "SELECT SUM(pnl_usdt) as s FROM trades WHERE account=? "
                    "AND action='CLOSE' AND pnl_usdt IS NOT NULL",
                    (account,)
                ).fetchone()["s"] or 0.0
        return {
            "total_trades": total,
            "win_trades": wins,
            "win_rate": wins / total if total > 0 else 0.0,
            "total_pnl_usdt": total_pnl,
        }
