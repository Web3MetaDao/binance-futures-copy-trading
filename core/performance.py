"""
performance.py — 绩效追踪模块
滚动计算盈亏比、夏普率，支持状态序列化。
"""
import math
from collections import deque


class PerformanceTracker:
    def __init__(self, window: int = 20):
        self.window = window
        self._returns: deque = deque(maxlen=window)

    @property
    def sample_count(self) -> int:
        return len(self._returns)

    def record(self, pnl_pct: float):
        self._returns.append(pnl_pct)

    def pl_ratio(self) -> float:
        """盈亏比 = 平均盈利 / 平均亏损"""
        wins = [r for r in self._returns if r > 0]
        losses = [abs(r) for r in self._returns if r < 0]
        if not wins or not losses:
            return 1.5  # 无数据时返回默认值
        return (sum(wins) / len(wins)) / (sum(losses) / len(losses))

    def sharpe(self, risk_free: float = 0.0) -> float:
        """年化夏普率（假设每笔交易独立）"""
        if len(self._returns) < 2:
            return 1.5
        mean = sum(self._returns) / len(self._returns)
        variance = sum((r - mean) ** 2 for r in self._returns) / (len(self._returns) - 1)
        std = math.sqrt(variance)
        if std == 0:
            return 3.0
        return (mean - risk_free) / std * math.sqrt(252)

    def win_rate(self) -> float:
        if not self._returns:
            return 0.0
        return sum(1 for r in self._returns if r > 0) / len(self._returns)

    def total_return(self) -> float:
        result = 1.0
        for r in self._returns:
            result *= (1 + r)
        return result - 1.0

    def dump(self) -> dict:
        return {"returns": list(self._returns), "window": self.window}

    def load(self, data: dict):
        self.window = data.get("window", self.window)
        self._returns = deque(data.get("returns", []), maxlen=self.window)

    def summary(self) -> dict:
        return {
            "samples": self.sample_count,
            "win_rate": round(self.win_rate(), 4),
            "pl_ratio": round(self.pl_ratio(), 4),
            "sharpe": round(self.sharpe(), 4),
            "total_return": round(self.total_return(), 4),
        }
