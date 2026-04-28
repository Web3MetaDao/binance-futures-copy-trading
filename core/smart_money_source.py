"""
smart_money_source.py — Binance Smart Money 备用信号链路

当主链路（公开实盘持仓抓取）失败或无信号时，自动切换到此模块。
本模块通过 Binance 排行榜 API 抓取头部聪明钱交易员的公开持仓，
并将其聚合为标准信号格式，与主链路信号格式完全兼容。

数据来源：
  - Binance Futures Leaderboard（排行榜）
  - 筛选条件：isShared=True（公开持仓）、按 ROI/PNL 排序
  - 抓取各交易员当前持仓，聚合为方向信号

审计修复记录（v2）：
  [FIX-13] 重试循环第 0 次也执行 sleep(backoff**0=1s)，浪费时间
           → 改为失败后才 sleep，首次立即请求
  [FIX-14] fallback 模式下备用信号也被 SM_MERGE_WEIGHT_FACTOR 降权
           → 仅在 merge 模式下降权，fallback 模式保持原始权重
  [FIX-15] 排行榜刷新失败时直接覆盖空列表，丢失旧缓存
           → 刷新失败时保留旧缓存，仅在成功时更新
  [FIX-16] fetch_top_traders 中 isTrader 字段含义不明确，应为 False
           （isTrader=True 为官方跟单交易员，False 为普通公开实盘）
"""

import time
import logging
from typing import Optional

import requests

from core.config import Config

logger = logging.getLogger("smart_money_source")

# ── Binance 公开排行榜 API ────────────────────────────────────────────────────
_LEADERBOARD_SEARCH = (
    "https://www.binance.com/bapi/futures/v2/public/future/leaderboard/searchLeaderboard"
)
_OTHER_POSITION = (
    "https://www.binance.com/bapi/futures/v1/public/future/leaderboard/getOtherPosition"
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Content-Type": "application/json",
    "clienttype": "web",
    "lang": "zh-CN",
    "Referer": "https://www.binance.com/zh-CN/futures-activity/leaderboard",
    "Origin": "https://www.binance.com",
}


def _post(url: str, payload: dict, retry: int = 3, backoff: float = 2.0) -> Optional[dict]:
    """带重试的 POST 请求。[FIX-13] 首次立即请求，失败后才 sleep。"""
    last_exc = None
    for i in range(retry):
        try:
            resp = requests.post(url, json=payload, headers=_HEADERS, timeout=12)
            if resp.status_code == 200:
                return resp.json()
            logger.warning(f"[SM_POST] HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            last_exc = e
            logger.warning(f"[SM_POST] attempt {i+1}/{retry} failed: {e}")
        if i < retry - 1:  # [FIX-13] 最后一次失败不再 sleep
            time.sleep(backoff ** i)
    logger.error(f"[SM_POST] 所有重试均失败: {last_exc}")
    return None


def _get(url: str, params: dict = None, retry: int = 3, backoff: float = 2.0) -> Optional[dict]:
    """带重试的 GET 请求。[FIX-13] 首次立即请求，失败后才 sleep。"""
    last_exc = None
    for i in range(retry):
        try:
            resp = requests.get(url, params=params, headers=_HEADERS, timeout=12)
            if resp.status_code == 200:
                return resp.json()
            logger.warning(f"[SM_GET] HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            last_exc = e
            logger.warning(f"[SM_GET] attempt {i+1}/{retry} failed: {e}")
        if i < retry - 1:
            time.sleep(backoff ** i)
    logger.error(f"[SM_GET] 所有重试均失败: {last_exc}")
    return None


# ── 排行榜抓取 ────────────────────────────────────────────────────────────────

def fetch_top_traders(
    trade_type: str = "PERPETUAL",
    sort_type: str = "ROI",
    limit: int = 10,
    shared_only: bool = True,
) -> list:
    """
    抓取排行榜头部交易员列表。

    Args:
        trade_type:   "PERPETUAL"（USDT-M）或 "DELIVERY"（COIN-M）
        sort_type:    "ROI" | "PNL" | "COPIERS"
        limit:        最多取几名交易员
        shared_only:  是否只取公开持仓的交易员

    Returns:
        [{"encryptedUid": str, "nickName": str, "roi": float, "pnl": float}, ...]
    """
    payload = {
        "isShared": shared_only,
        "isTrader": False,   # [FIX-16] False=普通公开实盘，True=官方跟单交易员
        "tradeType": trade_type,
        "periodType": "MONTHLY",
        "sortType": sort_type,
        "pageNumber": 1,
        "pageSize": max(limit * 2, 20),  # 多取一些，过滤后再截断
    }
    data = _post(_LEADERBOARD_SEARCH, payload,
                 retry=Config.API_RETRY_TIMES, backoff=Config.API_RETRY_BACKOFF)
    if not data:
        return []

    rows = (data.get("data") or {}).get("list") or []
    traders = []
    for row in rows:
        uid = row.get("encryptedUid")
        if not uid:
            continue
        if shared_only and not row.get("positionShared", False):
            continue
        traders.append({
            "encryptedUid": uid,
            "nickName": row.get("nickName", "unknown"),
            "roi": float(row.get("roi") or 0),
            "pnl": float(row.get("pnl") or 0),
            "followerCount": int(row.get("followerCount") or 0),
        })
        if len(traders) >= limit:
            break

    logger.info(f"[SM] 排行榜抓取: {len(traders)} 名交易员 (sort={sort_type})")
    return traders


def fetch_trader_positions(encrypted_uid: str, trade_type: str = "PERPETUAL") -> list:
    """
    抓取单个交易员的公开持仓列表。

    Returns:
        [{"symbol": str, "side": "LONG"/"SHORT", "amount": float,
          "entry_price": float, "leverage": int, "notional": float}, ...]
    """
    payload = {"encryptedUid": encrypted_uid, "tradeType": trade_type}
    data = _post(_OTHER_POSITION, payload,
                 retry=Config.API_RETRY_TIMES, backoff=Config.API_RETRY_BACKOFF)
    if not data:
        return []

    raw_list = (data.get("data") or {}).get("otherPositionRetList") or []
    positions = []
    for raw in raw_list:
        symbol = raw.get("symbol", "")
        if symbol and not symbol.endswith("USDT"):
            symbol = symbol + "USDT"
        amt = float(raw.get("amount", 0))
        entry = float(raw.get("entryPrice", 0))
        leverage = int(raw.get("leverage", 1))
        if amt == 0:
            continue
        positions.append({
            "symbol": symbol,
            "side": "LONG" if amt > 0 else "SHORT",
            "amount": abs(amt),
            "entry_price": entry,
            "leverage": leverage,
            "notional": abs(amt) * entry,
        })
    return positions


# ── Smart Money 信号聚合器 ────────────────────────────────────────────────────

class SmartMoneySource:
    """
    Binance Smart Money 备用信号链路

    工作流程：
    1. 从排行榜抓取头部 N 名公开持仓交易员
    2. 逐一抓取其当前持仓
    3. 按持仓方向加权聚合，生成标准信号字典
    4. 与主链路信号格式完全兼容

    信号格式（与 MultiLeaderAggregator.fetch_all() 完全一致）：
    {
      symbol: {
        "side": "LONG"/"SHORT",
        "weight": float,          # 归一化置信度 0~1
        "sources": [nickName, ...],
        "avg_leverage": float,
        "avg_notional": float,
        "from_smart_money": True  # 标记来源
      }
    }
    """

    def __init__(self):
        self.top_n = Config.SM_SOURCE_TOP_N
        self.sort_type = Config.SM_SOURCE_SORT_TYPE
        self.trade_type = Config.SM_SOURCE_TRADE_TYPE
        self._cached_traders: list = []
        self._trader_refresh_interval = 3600  # 每小时刷新一次排行榜
        self._last_trader_refresh: float = 0.0

    def _refresh_traders_if_needed(self):
        """
        定期刷新排行榜（避免每次轮询都请求）。
        [FIX-15] 刷新失败时保留旧缓存，不覆盖为空列表。
        """
        now = time.time()
        if now - self._last_trader_refresh > self._trader_refresh_interval:
            traders = fetch_top_traders(
                trade_type=self.trade_type,
                sort_type=self.sort_type,
                limit=self.top_n,
                shared_only=True,
            )
            if traders:
                # [FIX-15] 仅在成功时更新缓存
                self._cached_traders = traders
                self._last_trader_refresh = now
                logger.info(
                    f"[SM] 排行榜已刷新: {len(traders)} 名交易员 "
                    f"({[t['nickName'] for t in traders[:3]]}...)"
                )
            else:
                # [FIX-15] 失败时保留旧缓存，但更新时间戳（避免频繁重试）
                self._last_trader_refresh = now - self._trader_refresh_interval + 300  # 5 分钟后重试
                logger.warning("[SM] 排行榜刷新失败，保留旧缓存，5 分钟后重试")

    def fetch_all(self) -> dict:
        """
        抓取所有头部交易员持仓并聚合为标准信号。
        返回格式与 MultiLeaderAggregator.fetch_all() 完全一致。
        """
        self._refresh_traders_if_needed()

        if not self._cached_traders:
            logger.warning("[SM] 无可用交易员，备用链路返回空信号")
            return {}

        aggregated: dict = {}

        for trader in self._cached_traders:
            uid = trader["encryptedUid"]
            name = trader["nickName"]
            # 按 ROI 归一化权重（ROI 越高权重越大，最大 2.0）
            weight = min(2.0, max(0.1, 1.0 + trader["roi"] * 0.5))

            positions = fetch_trader_positions(uid, trade_type=self.trade_type)
            logger.debug(f"[SM] {name}: {len(positions)} 个持仓")

            for pos in positions:
                sym = pos["symbol"]
                if sym not in aggregated:
                    aggregated[sym] = {
                        "long_weight": 0.0,
                        "short_weight": 0.0,
                        "sources": [],
                        "leverages": [],
                        "notionals": [],
                    }
                entry = aggregated[sym]
                if pos["side"] == "LONG":
                    entry["long_weight"] += weight
                else:
                    entry["short_weight"] += weight
                entry["sources"].append(name)
                entry["leverages"].append(pos["leverage"])
                entry["notionals"].append(pos["notional"])

        result = {}
        for sym, data in aggregated.items():
            lw = data["long_weight"]
            sw = data["short_weight"]
            net = lw - sw
            if net == 0:
                continue
            side = "LONG" if net > 0 else "SHORT"
            confidence = abs(net) / (lw + sw)
            avg_lev = sum(data["leverages"]) / len(data["leverages"])
            avg_notional = sum(data["notionals"]) / len(data["notionals"])
            result[sym] = {
                "side": side,
                "weight": round(confidence, 4),
                "sources": data["sources"],
                "avg_leverage": round(avg_lev, 1),
                "avg_notional": round(avg_notional, 2),
                "from_smart_money": True,
            }

        logger.info(f"[SM] 备用链路聚合信号: {len(result)} 个交易对")
        return result

    def is_available(self) -> bool:
        """检查备用链路是否可用（有缓存交易员或能刷新）"""
        if self._cached_traders:
            return True
        traders = fetch_top_traders(limit=3, shared_only=True)
        return len(traders) > 0


# ── 双链路信号路由器 ──────────────────────────────────────────────────────────

class DualSourceRouter:
    """
    双链路信号路由器

    优先级策略：
    ┌─────────────────────────────────────────────────────────┐
    │  主链路（公开实盘持仓）                                   │
    │    ↓ 失败 / 无信号 / 连续空快照超限                       │
    │  备用链路（Smart Money 排行榜）                           │
    │    ↓ 也失败                                              │
    │  返回空信号，跳过本轮                                     │
    └─────────────────────────────────────────────────────────┘

    融合模式（SIGNAL_MERGE_MODE=merge）：
    - 主链路信号 + 备用链路信号取并集
    - 同一 symbol 主链路优先，备用链路补充缺失信号
    - 备用链路信号 weight 乘以 SM_MERGE_WEIGHT_FACTOR 降权

    [FIX-14] fallback 模式下备用信号不再降权，保持原始置信度
    """

    def __init__(self, primary_source, fallback_source: SmartMoneySource):
        self.primary = primary_source
        self.fallback = fallback_source
        self._primary_fail_count = 0
        self._primary_fail_threshold = Config.SM_FALLBACK_THRESHOLD
        self._merge_mode = Config.SM_SIGNAL_MERGE_MODE  # "fallback" | "merge"
        self._merge_weight = Config.SM_MERGE_WEIGHT_FACTOR

    def fetch_signals(self) -> dict:
        """
        获取信号，自动处理主/备链路切换与融合。

        Returns:
            标准信号字典，每个信号附加 "source_chain" 字段标识来源
        """
        primary_signals = {}
        primary_ok = False

        # 尝试主链路
        try:
            primary_signals = self.primary.fetch_all()
            if primary_signals:
                primary_ok = True
                self._primary_fail_count = 0
                logger.debug(f"[Router] 主链路正常: {len(primary_signals)} 个信号")
            else:
                self._primary_fail_count += 1
                logger.info(
                    f"[Router] 主链路无信号 "
                    f"(连续空次数: {self._primary_fail_count}/{self._primary_fail_threshold})"
                )
        except Exception as e:
            self._primary_fail_count += 1
            logger.warning(
                f"[Router] 主链路异常: {e} "
                f"(连续失败: {self._primary_fail_count}/{self._primary_fail_threshold})"
            )

        # 标记主链路信号来源
        for sig in primary_signals.values():
            sig["source_chain"] = "primary"

        # 判断是否需要备用链路
        need_fallback = (
            not primary_ok
            or self._primary_fail_count >= self._primary_fail_threshold
        )

        fallback_signals = {}
        if need_fallback or self._merge_mode == "merge":
            if not Config.SM_SOURCE_ENABLED:
                logger.info("[Router] Smart Money 备用链路已禁用（SM_SOURCE_ENABLED=false）")
            else:
                try:
                    fallback_signals = self.fallback.fetch_all()
                    logger.info(
                        f"[Router] 备用链路({'融合' if self._merge_mode == 'merge' else '切换'}): "
                        f"{len(fallback_signals)} 个信号"
                    )
                    for sig in fallback_signals.values():
                        # [FIX-14] 仅在 merge 模式下对备用信号降权
                        if self._merge_mode == "merge":
                            sig["weight"] = round(sig["weight"] * self._merge_weight, 4)
                        sig["source_chain"] = "smart_money"
                except Exception as e:
                    logger.warning(f"[Router] 备用链路异常: {e}")

        # 信号合并
        if self._merge_mode == "merge":
            # 融合模式：主链路优先，备用链路补充
            merged = dict(fallback_signals)  # 先放备用
            merged.update(primary_signals)   # 主链路覆盖同 symbol
            if merged:
                logger.info(
                    f"[Router] 融合信号: 主链路 {len(primary_signals)} + "
                    f"备用 {len(fallback_signals)} → 合并 {len(merged)} 个"
                )
            return merged
        else:
            # 切换模式：主链路有信号用主，否则用备用
            if primary_signals:
                return primary_signals
            if fallback_signals:
                logger.info(f"[Router] 已切换到备用链路: {len(fallback_signals)} 个信号")
                return fallback_signals
            logger.warning("[Router] 主备链路均无信号，本轮跳过")
            return {}

    @property
    def primary_fail_count(self) -> int:
        return self._primary_fail_count

    def reset_fail_count(self):
        self._primary_fail_count = 0
