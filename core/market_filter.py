"""
market_filter.py — 多时间框架信号确认 + 市场状态分类器

功能：
  1. MTF（Multi-Timeframe）过滤：
     - 15m 信号方向必须与 1h + 4h 趋势一致才执行
     - 使用 EMA20/EMA50 判断趋势方向

  2. 市场状态分类（MarketRegime）：
     - TRENDING:     ADX > 25，趋势市场，全仓跟单
     - RANGING:      ADX < 20，震荡市场，减仓 50%
     - HIGH_VOL:     布林带宽度 > 阈值，高波动，跳过
     - NORMAL:       默认状态

  3. 综合过滤结果：
     - 返回 (should_trade: bool, size_factor: float, reason: str)

审计修复记录：
  FIX-D01: _adx smooth 函数初始值使用 sum(lst[:p])（总和），
            而 Wilder 平滑法要求初始值为均值；
            且 smooth 结果包含了初始值，导致 zip 对齐偏移
            → 改为 _wilder_smooth，初始值为均值，结果长度正确
  FIX-D02: MTFFilter.check 每次调用都请求 3 个时间框架 K 线（3 次 API），
            高频调用时会触发 Binance 频率限制
            → 增加每个 (symbol, interval) 的 K 线缓存（TTL=5min）
  FIX-D03: _bollinger_width 当 std=0（价格完全不动）时返回 0.0，
            此时 adx 也接近 0，会误判为 RANGING
            → std=0 时返回极小值 0.001，不触发任何特殊状态
"""
import logging
import time
from typing import Dict, Tuple

from core.config import Config

logger = logging.getLogger("market_filter")


# ── 技术指标计算 ──────────────────────────────────────────────────────────────

def _ema(values: list, period: int) -> float:
    """计算 EMA（指数移动平均）"""
    if len(values) < period:
        return values[-1] if values else 0.0
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def _wilder_smooth(lst: list, period: int) -> list:
    """
    [FIX-D01] Wilder 平滑法标准实现
    初始值 = 前 period 个元素的均值
    后续 = prev × (period-1)/period + current
    返回长度 = len(lst) - period + 1
    """
    if len(lst) < period:
        return []
    initial = sum(lst[:period]) / period
    result = [initial]
    for v in lst[period:]:
        result.append(result[-1] * (period - 1) / period + v)
    return result


def _adx(klines: list, period: int = 14) -> float:
    """计算 ADX（平均趋向指数），衡量趋势强度"""
    if len(klines) < period * 2 + 1:
        return 20.0  # 数据不足时返回中性值

    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    closes = [float(k[4]) for k in klines]

    plus_dm, minus_dm, tr_list = [], [], []
    for i in range(1, len(klines)):
        h_diff = highs[i] - highs[i - 1]
        l_diff = lows[i - 1] - lows[i]
        plus_dm.append(h_diff if h_diff > l_diff and h_diff > 0 else 0.0)
        minus_dm.append(l_diff if l_diff > h_diff and l_diff > 0 else 0.0)
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        tr_list.append(tr)

    # [FIX-D01] 使用 Wilder 平滑法（初始值为均值）
    atr_s = _wilder_smooth(tr_list, period)
    pdm_s = _wilder_smooth(plus_dm, period)
    mdm_s = _wilder_smooth(minus_dm, period)

    if not atr_s:
        return 20.0

    dx_list = []
    for a, p, m in zip(atr_s, pdm_s, mdm_s):
        if a == 0:
            continue
        pdi = 100 * p / a
        mdi = 100 * m / a
        denom = pdi + mdi
        if denom == 0:
            continue
        dx_list.append(100 * abs(pdi - mdi) / denom)

    if not dx_list:
        return 20.0

    # ADX = 最近 period 个 DX 的均值（Wilder 平滑）
    adx_smooth = _wilder_smooth(dx_list, period)
    return adx_smooth[-1] if adx_smooth else 20.0


def _bollinger_width(klines: list, period: int = 20) -> float:
    """计算布林带宽度百分比（(上轨-下轨)/中轨）"""
    if len(klines) < period:
        return 0.05
    closes = [float(k[4]) for k in klines[-period:]]
    mean = sum(closes) / period
    if mean == 0:
        return 0.05
    variance = sum((c - mean) ** 2 for c in closes) / period
    std = variance ** 0.5

    # [FIX-D03] std=0 时价格完全不动，返回极小值，不触发任何特殊状态
    if std == 0:
        return 0.001

    return (4 * std) / mean  # 布林带宽度 ≈ 4σ/mean


def _trend_direction(klines: list) -> str:
    """
    使用 EMA20/EMA50 判断趋势方向
    返回 'BULLISH' / 'BEARISH' / 'NEUTRAL'
    """
    if len(klines) < 50:
        return "NEUTRAL"
    closes = [float(k[4]) for k in klines]
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    if ema20 > ema50 * 1.002:
        return "BULLISH"
    elif ema20 < ema50 * 0.998:
        return "BEARISH"
    return "NEUTRAL"


# ── 市场状态分类 ──────────────────────────────────────────────────────────────

class MarketRegime:
    TRENDING = "TRENDING"
    RANGING = "RANGING"
    HIGH_VOL = "HIGH_VOL"
    NORMAL = "NORMAL"


def classify_market(klines_15m: list) -> Tuple[str, float]:
    """
    分类市场状态，返回 (regime, size_factor)
      TRENDING  → size_factor=1.0（全仓）
      NORMAL    → size_factor=1.0
      RANGING   → size_factor=0.5（减仓）
      HIGH_VOL  → size_factor=0.0（跳过）
    """
    adx = _adx(klines_15m)
    boll_width = _bollinger_width(klines_15m)

    if boll_width > Config.MTF_HIGH_VOL_BOLL_WIDTH:
        return MarketRegime.HIGH_VOL, 0.0
    elif adx > Config.MTF_TRENDING_ADX_THRESHOLD:
        return MarketRegime.TRENDING, 1.0
    elif adx < Config.MTF_RANGING_ADX_THRESHOLD:
        return MarketRegime.RANGING, 0.5
    else:
        return MarketRegime.NORMAL, 1.0


# ── MTF 多时间框架过滤器 ──────────────────────────────────────────────────────

# [FIX-D02] K 线缓存：{(symbol, interval): (fetch_time, klines)}
_kline_cache: Dict[Tuple[str, str], Tuple[float, list]] = {}
_KLINE_CACHE_TTL = 300  # 5 分钟缓存，避免频繁 API 调用


def _get_klines_cached(client, symbol: str, interval: str, limit: int = 60) -> list:
    """[FIX-D02] 带 TTL 缓存的 K 线获取"""
    key = (symbol, interval)
    now = time.time()
    cached = _kline_cache.get(key)
    if cached and (now - cached[0]) < _KLINE_CACHE_TTL:
        return cached[1]
    klines = client.get_klines(symbol, interval=interval, limit=limit)
    _kline_cache[key] = (now, klines)
    return klines


class MTFFilter:
    """
    多时间框架信号确认过滤器
    只有当 15m 信号方向与 1h + 4h 趋势一致时才放行
    """

    def __init__(self, client):
        self.client = client

    def check(self, symbol: str, side: str) -> Tuple[bool, float, str]:
        """
        检查信号是否通过 MTF 过滤
        返回 (should_trade, size_factor, reason)
        """
        if not Config.MTF_ENABLED:
            return True, 1.0, ""

        try:
            # [FIX-D02] 使用缓存获取 K 线，避免每次都请求 API
            klines_1h = _get_klines_cached(self.client, symbol, "1h", 60)
            klines_4h = _get_klines_cached(self.client, symbol, "4h", 60)
            klines_15m = _get_klines_cached(self.client, symbol, "15m", 60)

            # 市场状态分类
            regime, regime_factor = classify_market(klines_15m)
            if regime == MarketRegime.HIGH_VOL:
                return False, 0.0, f"高波动市场({regime})，跳过"
            if regime == MarketRegime.RANGING:
                logger.info(f"[{symbol}] 震荡市场，仓位缩减 50%")

            # MTF 趋势确认
            trend_1h = _trend_direction(klines_1h)
            trend_4h = _trend_direction(klines_4h)
            expected_trend = "BULLISH" if side == "LONG" else "BEARISH"

            # 1h 和 4h 都需要方向一致（或 NEUTRAL 放行）
            h1_ok = trend_1h in (expected_trend, "NEUTRAL")
            h4_ok = trend_4h in (expected_trend, "NEUTRAL")

            if not h1_ok:
                return False, 0.0, f"1h 趋势({trend_1h}) 与信号方向({side}) 冲突"
            if not h4_ok:
                return False, 0.0, f"4h 趋势({trend_4h}) 与信号方向({side}) 冲突"

            logger.debug(
                f"[{symbol}] MTF 通过: 1h={trend_1h} 4h={trend_4h} "
                f"regime={regime} factor={regime_factor}"
            )
            return True, regime_factor, ""

        except Exception as e:
            logger.warning(f"[{symbol}] MTF 过滤异常: {e}，默认放行")
            return True, 1.0, ""
