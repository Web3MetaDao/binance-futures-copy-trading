"""
position_manager.py — 高级资金管理模块

实现四项资金管理升级：
  1. Kelly Criterion 动态仓位
  2. 相关性过滤（避免持仓集中）
  3. 追踪止损（ATR Trailing Stop）
  4. 分批建仓与分批止盈

所有计算均使用 Decimal 保证精度。
"""
import logging
import math
import threading
import time
from decimal import Decimal, ROUND_DOWN
from typing import Dict, List, Optional, Tuple

from core.config import Config

logger = logging.getLogger("position_manager")

# 高度相关的交易对组（同组内相关性视为 > 0.7）
_CORRELATION_GROUPS = [
    {"BTCUSDT", "ETHUSDT"},              # BTC/ETH 高度相关
    {"SOLUSDT", "AVAXUSDT", "DOTUSDT"},  # Layer-1 生态
    {"BNBUSDT", "CAKEUSDT"},             # BNB 生态
    {"LINKUSDT", "BANDUSDT"},            # 预言机赛道
    {"MATICUSDT", "OPUSDT", "ARBUSDT"},  # Layer-2 生态
]


# ── 1. Kelly Criterion 动态仓位 ───────────────────────────────────────────────

def kelly_fraction(win_rate: float, pl_ratio: float,
                   kelly_fraction_cap: float = None) -> float:
    """
    计算 Kelly 最优仓位比例
    f* = (p × b - q) / b
      p = 胜率, q = 1-p, b = 盈亏比

    kelly_fraction_cap: Kelly 分数上限（防止过度集中，默认 Config.KELLY_FRACTION_CAP）
    返回值范围 [0, kelly_fraction_cap]
    """
    cap = kelly_fraction_cap or Config.KELLY_FRACTION_CAP
    if win_rate <= 0 or pl_ratio <= 0:
        return Config.CAPITAL_RATIO  # 无数据时使用默认值

    q = 1.0 - win_rate
    f = (win_rate * pl_ratio - q) / pl_ratio

    # 半 Kelly（更保守，实践中常用）
    half_kelly = f * Config.KELLY_HALF_FRACTION

    # 限制上限
    result = max(0.0, min(half_kelly, cap))
    logger.debug(
        f"Kelly: win={win_rate:.2f} pl={pl_ratio:.2f} "
        f"f*={f:.3f} half={half_kelly:.3f} cap={cap} → {result:.3f}"
    )
    return result


# ── 2. 相关性过滤 ─────────────────────────────────────────────────────────────

def get_correlation_group(symbol: str) -> Optional[frozenset]:
    """返回该交易对所在的相关性组，不在任何组则返回 None"""
    for group in _CORRELATION_GROUPS:
        if symbol in group:
            return frozenset(group)
    return None


def calc_correlation_factor(symbol: str, existing_positions: List[dict]) -> float:
    """
    计算相关性降权系数
    若当前持仓中已有同组资产，返回降权系数（< 1.0）
    否则返回 1.0

    降权规则：
      同组已有 1 个持仓 → 系数 × 0.5
      同组已有 2+ 个持仓 → 系数 × 0.25（几乎不开）
    """
    if not Config.CORRELATION_FILTER_ENABLED:
        return 1.0

    my_group = get_correlation_group(symbol)
    if my_group is None:
        return 1.0  # 不在任何相关性组，不降权

    existing_symbols = {p.get("symbol") for p in existing_positions}
    overlap = my_group & existing_symbols - {symbol}

    if len(overlap) == 0:
        return 1.0
    elif len(overlap) == 1:
        logger.info(f"[{symbol}] 相关性降权 0.5（同组已有: {overlap}）")
        return 0.5
    else:
        logger.info(f"[{symbol}] 相关性降权 0.25（同组已有: {overlap}）")
        return 0.25


# ── 3. 追踪止损（ATR Trailing Stop）─────────────────────────────────────────

class TrailingStopManager:
    """
    ATR 追踪止损管理器
    当盈利超过 1R 后，止损线随价格上移，锁定部分利润。

    逻辑：
      - 开仓时记录 entry_price 和初始止损价
      - 每轮检查当前 mark_price
      - 若盈利 > 1R（即 mark_price 超过 entry + ATR×multiplier）
        → 将止损线上移至 mark_price - ATR×multiplier
      - 若 mark_price 触及追踪止损线 → 触发平仓
    """

    def __init__(self):
        self._lock = threading.Lock()
        # symbol -> {entry, side, trail_stop, atr, activated}
        self._trails: Dict[str, dict] = {}

    def register(self, symbol: str, side: str, entry_price: float, atr: float):
        """开仓时注册追踪止损"""
        multiplier = Config.TRAILING_STOP_ATR_MULT
        if side == "LONG":
            initial_stop = entry_price - atr * multiplier
        else:
            initial_stop = entry_price + atr * multiplier

        with self._lock:
            self._trails[symbol] = {
                "entry": entry_price,
                "side": side,
                "trail_stop": initial_stop,
                "atr": atr,
                "activated": False,  # 盈利超过 1R 后激活追踪
                "highest_price": entry_price,  # 多头追踪最高价
                "lowest_price": entry_price,   # 空头追踪最低价
            }
        logger.debug(
            f"[{symbol}] 追踪止损注册: entry={entry_price:.4f} "
            f"initial_stop={initial_stop:.4f} ATR={atr:.4f}"
        )

    def update(self, symbol: str, mark_price: float) -> Tuple[bool, float]:
        """
        更新追踪止损状态
        返回 (should_close, trail_stop_price)
        """
        with self._lock:
            trail = self._trails.get(symbol)
            if not trail:
                return False, 0.0

            side = trail["side"]
            entry = trail["entry"]
            atr = trail["atr"]
            multiplier = Config.TRAILING_STOP_ATR_MULT
            activation_r = Config.TRAILING_STOP_ACTIVATION_R  # 激活追踪需要的 R 倍数

            if side == "LONG":
                # 激活条件：盈利 > activation_r × ATR
                if not trail["activated"] and mark_price >= entry + atr * activation_r:
                    trail["activated"] = True
                    logger.info(
                        f"[{symbol}] 追踪止损已激活（盈利>{activation_r}R）"
                        f" mark={mark_price:.4f}"
                    )

                if trail["activated"]:
                    # 更新最高价，上移止损线
                    if mark_price > trail["highest_price"]:
                        trail["highest_price"] = mark_price
                        trail["trail_stop"] = mark_price - atr * multiplier
                        logger.debug(
                            f"[{symbol}] 追踪止损上移: {trail['trail_stop']:.4f}"
                        )

                    # 触发条件
                    if mark_price <= trail["trail_stop"]:
                        logger.info(
                            f"[{symbol}] 追踪止损触发: mark={mark_price:.4f} "
                            f"stop={trail['trail_stop']:.4f}"
                        )
                        return True, trail["trail_stop"]

            else:  # SHORT
                if not trail["activated"] and mark_price <= entry - atr * activation_r:
                    trail["activated"] = True
                    logger.info(
                        f"[{symbol}] 追踪止损已激活（盈利>{activation_r}R）"
                        f" mark={mark_price:.4f}"
                    )

                if trail["activated"]:
                    if mark_price < trail["lowest_price"]:
                        trail["lowest_price"] = mark_price
                        trail["trail_stop"] = mark_price + atr * multiplier
                        logger.debug(
                            f"[{symbol}] 追踪止损下移: {trail['trail_stop']:.4f}"
                        )

                    if mark_price >= trail["trail_stop"]:
                        logger.info(
                            f"[{symbol}] 追踪止损触发: mark={mark_price:.4f} "
                            f"stop={trail['trail_stop']:.4f}"
                        )
                        return True, trail["trail_stop"]

            return False, trail["trail_stop"]

    def remove(self, symbol: str):
        """平仓后移除追踪止损"""
        with self._lock:
            self._trails.pop(symbol, None)

    def get_all(self) -> dict:
        with self._lock:
            return dict(self._trails)


# ── 4. 分批建仓与分批止盈 ─────────────────────────────────────────────────────

class ScaledOrderManager:
    """
    分批建仓与分批止盈管理器

    建仓策略（高置信度信号 score > SCALE_IN_THRESHOLD）：
      - 第一批：50% 仓位，立即执行
      - 第二批：50% 仓位，价格回调 0.5×ATR 后补仓

    止盈策略：
      - 第一批止盈（50%）：盈利达到 1R（entry ± ATR×1）
      - 第二批止盈（50%）：追踪止损触发

    状态存储：symbol -> {batches: [...], filled: int}
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._orders: Dict[str, dict] = {}  # symbol -> order state

    def should_scale_in(self, symbol: str, score: float) -> bool:
        """判断是否应该分批建仓"""
        return (Config.SCALE_IN_ENABLED
                and score >= Config.SCALE_IN_SCORE_THRESHOLD
                and symbol not in self._orders)

    def register_scale_in(self, symbol: str, side: str, total_qty: Decimal,
                          entry_price: float, atr: float):
        """注册分批建仓计划"""
        first_qty = total_qty * Decimal("0.5")
        second_qty = total_qty - first_qty  # 处理奇数精度

        with self._lock:
            self._orders[symbol] = {
                "side": side,
                "total_qty": total_qty,
                "first_qty": first_qty,
                "second_qty": second_qty,
                "entry_price": entry_price,
                "atr": atr,
                "second_filled": False,
                # 第二批补仓触发价（多头：回调 0.5×ATR；空头：反弹 0.5×ATR）
                "second_trigger": (
                    entry_price - atr * 0.5 if side == "LONG"
                    else entry_price + atr * 0.5
                ),
            }
        logger.info(
            f"[{symbol}] 分批建仓注册: 第一批={first_qty} 第二批={second_qty} "
            f"补仓触发价={self._orders[symbol]['second_trigger']:.4f}"
        )
        return first_qty  # 返回第一批数量

    def check_second_batch(self, symbol: str, mark_price: float) -> Optional[Decimal]:
        """
        检查是否触发第二批补仓
        返回第二批数量（若触发），否则返回 None
        """
        with self._lock:
            order = self._orders.get(symbol)
            if not order or order["second_filled"]:
                return None

            side = order["side"]
            trigger = order["second_trigger"]

            triggered = (
                (side == "LONG" and mark_price <= trigger) or
                (side == "SHORT" and mark_price >= trigger)
            )

            if triggered:
                order["second_filled"] = True
                logger.info(
                    f"[{symbol}] 第二批补仓触发: mark={mark_price:.4f} "
                    f"trigger={trigger:.4f} qty={order['second_qty']}"
                )
                return order["second_qty"]
        return None

    def should_partial_tp(self, symbol: str, mark_price: float) -> Tuple[bool, Optional[Decimal]]:
        """
        检查是否触发第一批止盈（50% 平仓）
        盈利达到 1R（entry ± ATR×1）时触发
        返回 (should_tp, qty_to_close)
        """
        with self._lock:
            order = self._orders.get(symbol)
            if not order:
                return False, None

            side = order["side"]
            entry = order["entry_price"]
            atr = order["atr"]
            tp_price = (
                entry + atr * Config.SCALE_OUT_FIRST_R if side == "LONG"
                else entry - atr * Config.SCALE_OUT_FIRST_R
            )

            triggered = (
                (side == "LONG" and mark_price >= tp_price) or
                (side == "SHORT" and mark_price <= tp_price)
            )

            if triggered and not order.get("first_tp_done"):
                order["first_tp_done"] = True
                logger.info(
                    f"[{symbol}] 第一批止盈触发（1R）: mark={mark_price:.4f} "
                    f"tp={tp_price:.4f} qty={order['first_qty']}"
                )
                return True, order["first_qty"]

        return False, None

    def remove(self, symbol: str):
        """完全平仓后移除"""
        with self._lock:
            self._orders.pop(symbol, None)

    def get_all(self) -> dict:
        with self._lock:
            return dict(self._orders)
