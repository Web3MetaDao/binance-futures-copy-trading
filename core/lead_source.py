"""
lead_source.py — 多交易员公开实盘抓取与信号聚合

支持链接格式：
  1. app.binance.com/uni-qr/cpro/{nickname}?r={refCode}
     → 先通过 refCode 查 encryptedUid，再抓持仓
  2. www.binance.com/en/futures-activity/leaderboard?encryptedUid={uid}
     → 直接使用 encryptedUid
  3. 直接传入 encryptedUid 字符串（32 位十六进制）
"""
import re
import time
import logging
from typing import Optional

import requests

from core.config import Config

logger = logging.getLogger("lead_source")

# ── Binance 公开 API ─────────────────────────────────────────────────────────
# 持仓查询（POST）
_POSITION_API = (
    "https://www.binance.com/bapi/futures/v1/public/future/leaderboard/getOtherPosition"
)
# 通过 refCode 查询 encryptedUid（GET）
_REF_TO_UID_API = (
    "https://www.binance.com/bapi/accounts/v1/public/account/user/query-user-by-ref"
)
# 通过 nickname 查询 encryptedUid（POST）
_NICK_TO_UID_API = (
    "https://www.binance.com/bapi/futures/v2/public/future/leaderboard/searchLeaderboard"
)

_DEFAULT_HEADERS = {
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
    "Referer": "https://www.binance.com/zh-CN/copy-trading",
    "Origin": "https://www.binance.com",
}


# ── 链接解析 ─────────────────────────────────────────────────────────────────

def _extract_ref_code(url: str) -> Optional[str]:
    """从 uni-qr 分享链接中提取 r= 参数（邀请码）"""
    m = re.search(r"[?&]r=([A-Z0-9a-z]+)", url)
    return m.group(1) if m else None


def _extract_nickname(url: str) -> Optional[str]:
    """从 uni-qr/cpro/{nickname} 路径中提取昵称"""
    m = re.search(r"/(?:uni-qr/cpro|copy-trading/lead-data)/([^/?&]+)", url)
    return m.group(1) if m else None


def _extract_encrypted_uid_from_url(url: str) -> Optional[str]:
    """从 leaderboard URL 中直接提取 encryptedUid"""
    m = re.search(r"encryptedUid=([A-F0-9a-f]{32,})", url)
    return m.group(1) if m else None


def _is_encrypted_uid(s: str) -> bool:
    """判断字符串是否为 encryptedUid（32 位十六进制）"""
    return bool(re.fullmatch(r"[A-F0-9]{32,}", s.upper()))


# ── encryptedUid 查询 ────────────────────────────────────────────────────────

def _uid_via_ref_code(ref_code: str, retry: int = 3, backoff: float = 2.0) -> Optional[str]:
    """通过 refCode（邀请码）查询 encryptedUid"""
    url = f"{_REF_TO_UID_API}?refCode={ref_code}"
    for i in range(retry):
        try:
            resp = requests.get(url, headers=_DEFAULT_HEADERS, timeout=10)
            data = resp.json()
            uid = (data.get("data") or {}).get("encryptedUid")
            if uid:
                logger.info(f"[ref→uid] refCode={ref_code} → uid={uid[:8]}...")
                return uid
            logger.warning(f"[ref→uid] no uid in response: {data}")
        except Exception as e:
            logger.warning(f"[ref→uid] attempt {i+1} failed: {e}")
            time.sleep(backoff ** i)
    return None


def _uid_via_nickname(nickname: str, retry: int = 3, backoff: float = 2.0) -> Optional[str]:
    """通过昵称搜索排行榜，获取 encryptedUid"""
    payload = {"keyword": nickname, "isShared": True, "tradeType": "PERPETUAL"}
    for i in range(retry):
        try:
            resp = requests.post(
                _NICK_TO_UID_API, json=payload, headers=_DEFAULT_HEADERS, timeout=10
            )
            data = resp.json()
            rows = (data.get("data") or {}).get("list") or []
            # 精确匹配昵称（大小写不敏感）
            for row in rows:
                if (row.get("nickName") or "").lower() == nickname.lower():
                    uid = row.get("encryptedUid")
                    if uid:
                        logger.info(f"[nick→uid] nickname={nickname} → uid={uid[:8]}...")
                        return uid
            # 如果没有精确匹配，取第一条
            if rows:
                uid = rows[0].get("encryptedUid")
                if uid:
                    logger.info(
                        f"[nick→uid] nickname={nickname} fuzzy match → uid={uid[:8]}..."
                    )
                    return uid
            logger.warning(f"[nick→uid] no result for nickname={nickname}")
        except Exception as e:
            logger.warning(f"[nick→uid] attempt {i+1} failed: {e}")
            time.sleep(backoff ** i)
    return None


def resolve_encrypted_uid(url_or_uid: str,
                           retry: int = 3,
                           backoff: float = 2.0) -> Optional[str]:
    """
    智能解析 encryptedUid，支持三种输入：
      1. 直接是 encryptedUid（32位十六进制）
      2. 含 encryptedUid= 的 leaderboard URL
      3. uni-qr/cpro 分享链接（先 refCode，再 nickname）
    """
    # 情况 1：直接是 encryptedUid
    if _is_encrypted_uid(url_or_uid):
        return url_or_uid.upper()

    # 情况 2：leaderboard URL 中直接含 encryptedUid
    uid = _extract_encrypted_uid_from_url(url_or_uid)
    if uid:
        return uid

    # 情况 3：uni-qr 分享链接
    # 优先通过 refCode 查询
    ref_code = _extract_ref_code(url_or_uid)
    if ref_code:
        uid = _uid_via_ref_code(ref_code, retry=retry, backoff=backoff)
        if uid:
            return uid

    # 降级：通过 nickname 搜索
    nickname = _extract_nickname(url_or_uid)
    if nickname:
        uid = _uid_via_nickname(nickname, retry=retry, backoff=backoff)
        if uid:
            return uid

    logger.error(f"[resolve_uid] 无法解析 encryptedUid，输入: {url_or_uid}")
    return None


# ── 持仓抓取 ─────────────────────────────────────────────────────────────────

def _fetch_positions_by_uid(encrypted_uid: str,
                             retry: int = 3,
                             backoff: float = 2.0) -> list:
    """通过 encryptedUid 抓取公开持仓（POST）"""
    payload = {"encryptedUid": encrypted_uid, "tradeType": "PERPETUAL"}
    for i in range(retry):
        try:
            resp = requests.post(
                _POSITION_API, json=payload, headers=_DEFAULT_HEADERS, timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                positions = (data.get("data") or {}).get("otherPositionRetList") or []
                return positions
            else:
                logger.warning(
                    f"[fetch_pos] HTTP {resp.status_code}: {resp.text[:200]}"
                )
        except Exception as e:
            logger.warning(f"[fetch_pos] attempt {i+1} failed: {e}")
            time.sleep(backoff ** i)
    return []


def _normalize_position(raw: dict) -> dict:
    """将原始持仓数据标准化为统一格式"""
    symbol = raw.get("symbol", "")
    if symbol and not symbol.endswith("USDT"):
        symbol = symbol + "USDT"
    amt = float(raw.get("amount", 0))
    entry = float(raw.get("entryPrice", 0))
    leverage = int(raw.get("leverage", 1))
    side = "LONG" if amt > 0 else "SHORT"
    return {
        "symbol": symbol,
        "side": side,
        "amount": abs(amt),
        "entry_price": entry,
        "leverage": leverage,
        "notional": abs(amt) * entry,
    }


# ── 单交易员追踪器 ────────────────────────────────────────────────────────────

class LeaderTracker:
    """
    单个交易员跟踪器
    支持 uni-qr 分享链接、leaderboard URL、直接 encryptedUid
    """

    def __init__(self, name: str, url: str, weight: float = 1.0):
        self.name = name
        self.url = url
        self.weight = weight
        self._encrypted_uid: Optional[str] = None

    def _ensure_uid(self):
        if self._encrypted_uid:
            return
        uid = resolve_encrypted_uid(
            self.url,
            retry=Config.API_RETRY_TIMES,
            backoff=Config.API_RETRY_BACKOFF,
        )
        if uid:
            self._encrypted_uid = uid
            logger.info(f"[{self.name}] encryptedUid 已解析: {uid[:8]}...")
        else:
            logger.error(f"[{self.name}] 无法获取 encryptedUid，链接: {self.url}")

    def fetch(self) -> list:
        """抓取该交易员当前公开持仓，返回标准化列表"""
        self._ensure_uid()
        if not self._encrypted_uid:
            return []
        raw_list = _fetch_positions_by_uid(
            self._encrypted_uid,
            retry=Config.API_RETRY_TIMES,
            backoff=Config.API_RETRY_BACKOFF,
        )
        positions = [_normalize_position(r) for r in raw_list]
        logger.info(f"[{self.name}] 抓取到 {len(positions)} 个持仓")
        return positions

    @property
    def encrypted_uid(self) -> Optional[str]:
        return self._encrypted_uid


# ── 多交易员聚合器 ────────────────────────────────────────────────────────────

class MultiLeaderAggregator:
    """
    多交易员信号聚合器

    聚合逻辑：
    - 同一 symbol 多个交易员方向一致 → 权重累加，信号更强
    - 方向冲突 → 按净权重决定方向（净权重为 0 则跳过）
    - 返回归一化置信度 weight ∈ (0, 1]
    """

    def __init__(self):
        leaders_cfg = Config.get_leaders()
        self.trackers = [
            LeaderTracker(l["name"], l["url"], float(l.get("weight", 1.0)))
            for l in leaders_cfg
        ]
        logger.info(
            f"已加载 {len(self.trackers)} 个交易员: {[t.name for t in self.trackers]}"
        )

    def fetch_all(self) -> dict:
        """
        返回聚合后的信号字典：
        {
          symbol: {
            "side": "LONG"/"SHORT",
            "weight": float,          # 归一化置信度 0~1
            "sources": [name, ...],
            "avg_leverage": float,
            "avg_notional": float,
          }
        }
        """
        aggregated: dict = {}

        for tracker in self.trackers:
            positions = tracker.fetch()
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
                    entry["long_weight"] += tracker.weight
                else:
                    entry["short_weight"] += tracker.weight
                entry["sources"].append(tracker.name)
                entry["leverages"].append(pos["leverage"])
                entry["notionals"].append(pos["notional"])

        result = {}
        for sym, data in aggregated.items():
            lw = data["long_weight"]
            sw = data["short_weight"]
            net = lw - sw
            if net == 0:
                logger.debug(f"[agg] {sym} 方向冲突，跳过")
                continue
            side = "LONG" if net > 0 else "SHORT"
            weight = abs(net) / (lw + sw)  # 归一化置信度
            avg_lev = sum(data["leverages"]) / len(data["leverages"])
            avg_notional = sum(data["notionals"]) / len(data["notionals"])
            result[sym] = {
                "side": side,
                "weight": round(weight, 4),
                "sources": data["sources"],
                "avg_leverage": round(avg_lev, 1),
                "avg_notional": round(avg_notional, 2),
            }

        logger.info(f"聚合信号: {len(result)} 个交易对")
        return result
