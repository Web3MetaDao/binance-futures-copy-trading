"""
smart_money.py — Smart Money 评分引擎
综合资金费率、OI 变化、头部多空比、ATR 波动率、成交量确认对信号打分。
分数范围 0.0 ~ 1.0，低于 Config.SMART_MONEY_MIN_SCORE 的信号将被过滤。
"""
import math
import logging
from typing import Optional

from core.config import Config

logger = logging.getLogger("smart_money")


def _calc_atr(klines: list, period: int = 14) -> float:
    """计算 ATR（平均真实波幅）"""
    if len(klines) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(klines)):
        high = float(klines[i][2])
        low = float(klines[i][3])
        prev_close = float(klines[i - 1][4])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    return sum(trs[-period:]) / period


def _calc_volume_ratio(klines: list, period: int = 10) -> float:
    """最新成交量 / 近期均量，>1 表示放量"""
    if len(klines) < period + 1:
        return 1.0
    vols = [float(k[5]) for k in klines]
    avg = sum(vols[-period - 1:-1]) / period
    if avg == 0:
        return 1.0
    return vols[-1] / avg


class SmartMoneyScorer:
    def __init__(self, client):
        self.client = client

    def score(self, symbol: str, side: str, signal_weight: float = 1.0) -> dict:
        """
        对单个信号打分，返回:
        {
            "score": float,          # 综合分 0~1
            "skip": bool,            # 是否应跳过
            "reason": str,           # 跳过原因（若 skip=True）
            "details": dict          # 各项子分
        }
        """
        if not Config.SMART_MONEY_ENABLED:
            return {"score": 1.0, "skip": False, "reason": "", "details": {}}

        details = {}
        scores = []

        # 1. 资金费率评分
        try:
            fr = self.client.get_funding_rate(symbol)
            details["funding_rate"] = fr
            # 做多时负资金费率有利（空头付费），做空时正资金费率有利
            if side == "LONG":
                fr_score = max(0.0, min(1.0, 0.5 - fr * 100))
            else:
                fr_score = max(0.0, min(1.0, 0.5 + fr * 100))
            details["funding_score"] = round(fr_score, 4)
            scores.append(fr_score * 0.2)
        except Exception as e:
            logger.warning(f"[{symbol}] funding rate error: {e}")
            scores.append(0.1)

        # 2. OI 变化评分（OI 增加 + 方向一致 = 更高分）
        try:
            oi_list = self.client.get_open_interest(symbol, period=Config.OI_PERIOD, limit=5)
            if len(oi_list) >= 2:
                oi_change = (oi_list[-1] - oi_list[0]) / (oi_list[0] + 1e-9)
                details["oi_change_pct"] = round(oi_change * 100, 2)
                oi_score = max(0.0, min(1.0, 0.5 + oi_change * 5))
                details["oi_score"] = round(oi_score, 4)
                scores.append(oi_score * 0.25)
            else:
                scores.append(0.125)
        except Exception as e:
            logger.warning(f"[{symbol}] OI error: {e}")
            scores.append(0.125)

        # 3. 头部账户多空比评分
        try:
            ratio_list = self.client.get_top_long_short_ratio(symbol, period=Config.OI_PERIOD, limit=5)
            if ratio_list:
                avg_ratio = sum(ratio_list) / len(ratio_list)
                details["top_ls_ratio"] = round(avg_ratio, 4)
                if side == "LONG":
                    ls_score = max(0.0, min(1.0, avg_ratio / 2.0))
                else:
                    ls_score = max(0.0, min(1.0, 1.0 / (avg_ratio + 1e-9) / 2.0))
                details["ls_score"] = round(ls_score, 4)
                scores.append(ls_score * 0.25)
            else:
                scores.append(0.125)
        except Exception as e:
            logger.warning(f"[{symbol}] top ratio error: {e}")
            scores.append(0.125)

        # 4. ATR 波动率过滤
        try:
            klines = self.client.get_klines(symbol, interval="15m", limit=Config.ATR_PERIOD + 5)
            atr = _calc_atr(klines, Config.ATR_PERIOD)
            mark = float(klines[-1][4])
            atr_pct = atr / mark if mark > 0 else 0
            details["atr_pct"] = round(atr_pct * 100, 4)

            # 高波动时降分或跳过
            atr_threshold = 0.03  # 3% ATR 为高波动
            if Config.HIGH_VOL_SKIP and atr_pct > atr_threshold * Config.HIGH_VOL_ATR_MULT:
                logger.info(f"[{symbol}] HIGH VOLATILITY skip: ATR={atr_pct:.2%}")
                return {"score": 0.0, "skip": True,
                        "reason": f"ATR过高({atr_pct:.2%})", "details": details}
            atr_score = max(0.0, 1.0 - atr_pct / atr_threshold)
            details["atr_score"] = round(atr_score, 4)
            scores.append(atr_score * 0.15)

            # 5. 成交量确认
            vol_ratio = _calc_volume_ratio(klines)
            details["vol_ratio"] = round(vol_ratio, 4)
            vol_score = min(1.0, vol_ratio / 1.5)
            details["vol_score"] = round(vol_score, 4)
            scores.append(vol_score * 0.15)
        except Exception as e:
            logger.warning(f"[{symbol}] kline/ATR error: {e}")
            scores.append(0.075)
            scores.append(0.075)

        # 综合分 × 信号权重（多交易员置信度）
        raw_score = sum(scores)
        final_score = round(raw_score * signal_weight, 4)
        details["signal_weight"] = signal_weight
        details["raw_score"] = round(raw_score, 4)

        skip = final_score < Config.SMART_MONEY_MIN_SCORE
        reason = f"评分({final_score:.2f}) < 阈值({Config.SMART_MONEY_MIN_SCORE})" if skip else ""

        return {"score": final_score, "skip": skip, "reason": reason, "details": details}
