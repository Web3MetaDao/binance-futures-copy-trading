"""
config.py — 配置加载模块
从 .env 文件或环境变量读取所有参数，支持多交易员配置。
"""
import os
import json
from decimal import Decimal
from pathlib import Path


def _load_dotenv(path: str = ".env"):
    """轻量级 .env 文件加载（不覆盖已有环境变量）"""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv()


def _get(key: str, default=None, cast=str):
    val = os.environ.get(key, "")
    if val == "":
        return default
    try:
        return cast(val)
    except Exception:
        return default


def _get_bool(key: str, default: bool = False) -> bool:
    val = os.environ.get(key, "").lower()
    if val in ("1", "true", "yes"):
        return True
    if val in ("0", "false", "no"):
        return False
    return default


class Config:
    # ── Binance API ──────────────────────────────────────────────
    API_KEY: str = _get("BINANCE_API_KEY", "")
    API_SECRET: str = _get("BINANCE_API_SECRET", "")
    TESTNET: bool = _get_bool("BINANCE_TESTNET", False)

    # ── 多交易员配置（JSON 数组字符串）──────────────────────────────
    # 格式: '[{"name":"trader1","url":"https://...","weight":1.0},...]'
    LEADERS_JSON: str = _get("LEADERS_JSON", "[]")

    @classmethod
    def get_leaders(cls) -> list:
        """返回交易员列表，每项包含 name / url / weight"""
        try:
            leaders = json.loads(cls.LEADERS_JSON)
        except Exception:
            leaders = []
        # 兼容旧版单交易员配置
        if not leaders:
            url = _get("LEAD_SHARE_URL", "")
            if url:
                leaders = [{"name": "default", "url": url, "weight": 1.0}]
        return leaders

    # ── 轮询 ─────────────────────────────────────────────────────
    POLL_INTERVAL: int = _get("POLL_INTERVAL", 30, int)

    # ── 资金模式 ─────────────────────────────────────────────────
    CAPITAL_RATIO: float = _get("CAPITAL_RATIO", 0.1, float)
    RISK_MULTIPLIER: float = _get("RISK_MULTIPLIER", 1.0, float)
    MAX_NOTIONAL_PER_SYMBOL: float = _get("MAX_NOTIONAL_PER_SYMBOL", 500.0, float)
    MAX_TOTAL_NOTIONAL: float = _get("MAX_TOTAL_NOTIONAL", 2000.0, float)

    # 固定跟单员模式
    FIXED_FOLLOWER_ENABLED: bool = _get_bool("FIXED_FOLLOWER_ENABLED", False)
    FIXED_FOLLOWER_NOTIONAL_USDT: float = _get("FIXED_FOLLOWER_NOTIONAL_USDT", 50.0, float)

    # ── 复利（真复利模式）────────────────────────────────────────
    # 每笔平仓后立即从交易所拉取最新余额作为下一笔开仓的基础资金
    # 盈利 → 自动扩大仓位；亏损 → 自动缩小仓位
    COMPOUND_ENABLED: bool = _get_bool("COMPOUND_ENABLED", True)
    COMPOUND_REINVEST_RATIO: float = _get("COMPOUND_REINVEST_RATIO", 1.0, float)  # 真复利固定为 1.0，保留字段供回测兼容

    # ── 绩效目标 ─────────────────────────────────────────────────
    TARGET_PL_RATIO_MIN: float = _get("TARGET_PL_RATIO_MIN", 1.5, float)
    TARGET_PL_RATIO_MAX: float = _get("TARGET_PL_RATIO_MAX", 3.0, float)
    TARGET_SHARPE_MIN: float = _get("TARGET_SHARPE_MIN", 1.5, float)
    TARGET_SHARPE_MAX: float = _get("TARGET_SHARPE_MAX", 3.0, float)
    PERFORMANCE_WINDOW: int = _get("PERFORMANCE_WINDOW", 20, int)

    # ── 风控 ─────────────────────────────────────────────────────
    STOP_LOSS_PCT: float = _get("STOP_LOSS_PCT", 0.05, float)       # 5% 止损
    TAKE_PROFIT_PCT: float = _get("TAKE_PROFIT_PCT", 0.10, float)   # 10% 止盈
    MAX_DRAWDOWN_PCT: float = _get("MAX_DRAWDOWN_PCT", 0.15, float) # 15% 最大回撤熔断
    CIRCUIT_BREAKER_MINUTES: int = _get("CIRCUIT_BREAKER_MINUTES", 60, int)
    EMPTY_SNAPSHOT_TOLERANCE: int = _get("EMPTY_SNAPSHOT_TOLERANCE", 3, int)
    SYMBOL_WHITELIST: list = [s.strip() for s in _get("SYMBOL_WHITELIST", "").split(",") if s.strip()]
    SYMBOL_BLACKLIST: list = [s.strip() for s in _get("SYMBOL_BLACKLIST", "").split(",") if s.strip()]
    NO_REVERSE: bool = _get_bool("NO_REVERSE", True)

    # ── API 重试 ─────────────────────────────────────────────────
    API_RETRY_TIMES: int = _get("API_RETRY_TIMES", 3, int)
    API_RETRY_BACKOFF: float = _get("API_RETRY_BACKOFF", 2.0, float)

    # ── Smart Money 评分器 ────────────────────────────────────────
    SMART_MONEY_ENABLED: bool = _get_bool("SMART_MONEY_ENABLED", True)
    SMART_MONEY_MIN_SCORE: float = _get("SMART_MONEY_MIN_SCORE", 0.4, float)
    OI_PERIOD: str = _get("OI_PERIOD", "5m")
    ATR_PERIOD: int = _get("ATR_PERIOD", 14, int)
    HIGH_VOL_SKIP: bool = _get_bool("HIGH_VOL_SKIP", True)
    HIGH_VOL_ATR_MULT: float = _get("HIGH_VOL_ATR_MULT", 2.5, float)

    # ── Smart Money 备用信号链路 ──────────────────────────────────
    # 当主链路（公开实盘抓取）失败或无信号时，自动切换到排行榜聪明钱链路
    SM_SOURCE_ENABLED: bool = _get_bool("SM_SOURCE_ENABLED", True)
    # 信号模式: "fallback"=主链路失败时切换 | "merge"=主备信号融合
    SM_SIGNAL_MERGE_MODE: str = _get("SM_SIGNAL_MERGE_MODE", "fallback")
    # 主链路连续无信号/失败多少次后切换到备用链路
    SM_FALLBACK_THRESHOLD: int = _get("SM_FALLBACK_THRESHOLD", 3, int)
    # 备用链路信号降权系数（融合模式下备用信号权重乘以此系数）
    SM_MERGE_WEIGHT_FACTOR: float = _get("SM_MERGE_WEIGHT_FACTOR", 0.6, float)
    # 从排行榜抓取头部 N 名交易员
    SM_SOURCE_TOP_N: int = _get("SM_SOURCE_TOP_N", 10, int)
    # 排行榜排序方式: "ROI" | "PNL" | "COPIERS"
    SM_SOURCE_SORT_TYPE: str = _get("SM_SOURCE_SORT_TYPE", "ROI")
    # 合约类型: "PERPETUAL"（USDT-M）| "DELIVERY"（COIN-M）
    SM_SOURCE_TRADE_TYPE: str = _get("SM_SOURCE_TRADE_TYPE", "PERPETUAL")

    # ── Telegram ─────────────────────────────────────────────────
    TELEGRAM_ENABLED: bool = _get_bool("TELEGRAM_ENABLED", False)
    TELEGRAM_BOT_TOKEN: str = _get("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = _get("TELEGRAM_CHAT_ID", "")

    # ── 回测 ─────────────────────────────────────────────────────
    BACKTEST_FEE_BPS: float = _get("BACKTEST_FEE_BPS", 4.0, float)
    BACKTEST_SLIPPAGE_BPS: float = _get("BACKTEST_SLIPPAGE_BPS", 2.0, float)

    # ── Web 面板 ─────────────────────────────────────────────────
    WEB_HOST: str = _get("WEB_HOST", "0.0.0.0")
    WEB_PORT: int = _get("WEB_PORT", 5000, int)
    WEB_SECRET: str = _get("WEB_SECRET", "changeme")

    # ── 日志 ─────────────────────────────────────────────────────
    LOG_DIR: str = _get("LOG_DIR", "logs")
    STATE_FILE: str = _get("STATE_FILE", "data/state.json")
