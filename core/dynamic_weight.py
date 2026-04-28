"""
dynamic_weight.py — 动态交易员权重计算模块

根据每位交易员过去 N 天的历史绩效（胜率、盈亏比、最大回撤）
动态计算权重，替代配置文件中的静态权重。

权重公式：
  score = win_rate × pl_ratio / (1 + max_drawdown)
  weight = softmax(scores)

数据来源：从本地 SQLite 数据库读取历史交易记录（由 db.py 写入）
降级方案：若无历史数据，使用配置文件中的静态权重

审计修复记录：
  FIX-E01: _cache / _cache_ts 在多线程下无锁访问，存在竞态条件
            → 增加 _cache_lock 保护缓存读写
  FIX-E02: get_weights 返回 self._cache 引用，调用方若修改会污染缓存
            → 返回 dict 副本
  FIX-E03: _calculate 抛出异常时缓存未更新，下次调用仍重新计算
            → 增加异常捕获，失败时降级为静态权重并缓存结果
"""
import logging
import math
import threading
import time
from typing import Dict, List

logger = logging.getLogger("dynamic_weight")


def _softmax(scores: Dict[str, float]) -> Dict[str, float]:
    """将分数归一化为权重（softmax）"""
    if not scores:
        return {}
    vals = list(scores.values())
    max_v = max(vals)
    exp_vals = {k: math.exp(v - max_v) for k, v in scores.items()}
    total = sum(exp_vals.values())
    if total == 0:
        n = len(scores)
        return {k: 1.0 / n for k in scores}
    return {k: v / total for k, v in exp_vals.items()}


class DynamicWeightCalculator:
    """
    动态交易员权重计算器
    """

    def __init__(self, db=None, lookback_days: int = 30):
        """
        db: DatabaseManager 实例（可选，无则降级为静态权重）
        lookback_days: 历史绩效回溯天数
        """
        self.db = db
        self.lookback_days = lookback_days

        # [FIX-E01] 缓存读写加锁保护
        self._cache_lock = threading.Lock()
        self._cache: Dict[str, float] = {}
        self._cache_ts: float = 0.0
        self._cache_ttl: float = 3600.0  # 每小时刷新一次

    def get_weights(self, traders: List[dict]) -> Dict[str, float]:
        """
        获取交易员动态权重
        traders: [{"name": "lanaai", "weight": 1.0}, ...]
        返回: {"lanaai": 0.65, ...}（归一化后的动态权重）
        """
        now = time.time()

        # [FIX-E01] 在锁内检查缓存，[FIX-E02] 返回副本
        with self._cache_lock:
            if self._cache and (now - self._cache_ts) < self._cache_ttl:
                return dict(self._cache)

        # 锁外计算（避免长时间持锁）
        try:
            weights = self._calculate(traders)
        except Exception as e:
            logger.warning(f"[DynamicWeight] 权重计算失败: {e}，降级为静态权重")
            # [FIX-E03] 失败时降级为静态权重
            static = {t["name"]: float(t.get("weight", 1.0)) for t in traders}
            total = sum(static.values()) or 1.0
            weights = {k: v / total for k, v in static.items()}

        # [FIX-E01] 在锁内更新缓存
        with self._cache_lock:
            self._cache = weights
            self._cache_ts = now

        return dict(weights)  # [FIX-E02] 返回副本

    def _calculate(self, traders: List[dict]) -> Dict[str, float]:
        """计算动态权重"""
        if not self.db:
            # 无数据库，使用静态权重归一化
            static = {t["name"]: float(t.get("weight", 1.0)) for t in traders}
            total = sum(static.values()) or 1.0
            return {k: v / total for k, v in static.items()}

        scores = {}
        for trader in traders:
            name = trader["name"]
            try:
                stats = self.db.get_trader_stats(name, self.lookback_days)
                if stats and stats.get("total_trades", 0) >= 5:
                    win_rate = stats.get("win_rate", 0.5)
                    pl_ratio = stats.get("pl_ratio", 1.5)
                    max_dd = stats.get("max_drawdown", 0.1)
                    # 综合评分：胜率×盈亏比 / (1+最大回撤)
                    score = (win_rate * pl_ratio) / (1 + max_dd)
                    scores[name] = max(score, 0.01)  # 最小分数防止权重为 0
                    logger.debug(
                        f"[{name}] 动态权重评分: win={win_rate:.2f} "
                        f"pl={pl_ratio:.2f} dd={max_dd:.2f} → score={score:.3f}"
                    )
                else:
                    # 历史数据不足，使用静态权重
                    scores[name] = float(trader.get("weight", 1.0))
                    logger.debug(f"[{name}] 历史数据不足，使用静态权重")
            except Exception as e:
                logger.warning(f"[{name}] 动态权重计算失败: {e}，使用静态权重")
                scores[name] = float(trader.get("weight", 1.0))

        weights = _softmax(scores)
        logger.info(f"动态权重更新: {weights}")
        return weights

    def invalidate_cache(self):
        """强制刷新缓存（交易完成后调用）"""
        # [FIX-E01] 加锁保护
        with self._cache_lock:
            self._cache_ts = 0.0
