"""
binance_client.py — Binance USDT-M Futures 签名客户端
支持：市价开仓、止损止盈单、查询仓位、余额、标记价格、市场数据
所有关键请求均带指数退避重试。
"""
import hashlib
import hmac
import time
import math
import logging
from decimal import Decimal, ROUND_DOWN
from typing import Optional
from urllib.parse import urlencode

import requests

from core.config import Config

logger = logging.getLogger("binance_client")

BASE_URL = "https://testnet.binancefuture.com" if Config.TESTNET else "https://fapi.binance.com"


class BinanceError(Exception):
    pass


class BinanceFuturesClient:
    def __init__(self, api_key: str = None, api_secret: str = None,
                 retry_times: int = None, retry_backoff: float = None):
        self.api_key = api_key or Config.API_KEY
        self.api_secret = api_secret or Config.API_SECRET
        self.retry_times = retry_times if retry_times is not None else Config.API_RETRY_TIMES
        self.retry_backoff = retry_backoff if retry_backoff is not None else Config.API_RETRY_BACKOFF
        self._session = requests.Session()
        self._session.headers.update({"X-MBX-APIKEY": self.api_key})
        self._exchange_info: dict = {}   # symbol -> filters cache

    # ── 底层请求 ──────────────────────────────────────────────────

    def _sign(self, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        query = urlencode(params)
        sig = hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        params["signature"] = sig
        return params

    def _request_with_retry(self, method: str, path: str, signed: bool = False,
                             params: dict = None, data: dict = None) -> dict:
        params = params or {}
        data = data or {}
        if signed:
            if method.upper() in ("GET",):
                params = self._sign(params)
            else:
                data = self._sign(data)
        url = BASE_URL + path
        last_exc = None
        for attempt in range(self.retry_times):
            try:
                resp = self._session.request(method, url, params=params, data=data, timeout=10)
                if resp.status_code == 200:
                    return resp.json()
                err = resp.json()
                raise BinanceError(f"HTTP {resp.status_code}: {err}")
            except (requests.RequestException, BinanceError) as e:
                last_exc = e
                wait = self.retry_backoff ** attempt
                logger.warning(f"[retry {attempt+1}/{self.retry_times}] {path} failed: {e}, wait {wait:.1f}s")
                time.sleep(wait)
        raise BinanceError(f"All {self.retry_times} retries failed for {path}: {last_exc}")

    # ── 市场数据（公开接口）────────────────────────────────────────

    def get_mark_price(self, symbol: str) -> float:
        data = self._request_with_retry("GET", "/fapi/v1/premiumIndex", params={"symbol": symbol})
        return float(data["markPrice"])

    def get_funding_rate(self, symbol: str) -> float:
        data = self._request_with_retry("GET", "/fapi/v1/premiumIndex", params={"symbol": symbol})
        return float(data.get("lastFundingRate", 0))

    def get_open_interest(self, symbol: str, period: str = "5m", limit: int = 5) -> list:
        data = self._request_with_retry("GET", "/futures/data/openInterestHist",
                                        params={"symbol": symbol, "period": period, "limit": limit})
        return [float(x["sumOpenInterest"]) for x in data]

    def get_top_long_short_ratio(self, symbol: str, period: str = "5m", limit: int = 5) -> list:
        data = self._request_with_retry("GET", "/futures/data/topLongShortPositionRatio",
                                        params={"symbol": symbol, "period": period, "limit": limit})
        return [float(x["longShortRatio"]) for x in data]

    def get_klines(self, symbol: str, interval: str = "15m", limit: int = 50) -> list:
        """返回 OHLCV 列表，每项 [open_time, open, high, low, close, volume, ...]"""
        return self._request_with_retry("GET", "/fapi/v1/klines",
                                        params={"symbol": symbol, "interval": interval, "limit": limit})

    def get_exchange_info(self) -> dict:
        return self._request_with_retry("GET", "/fapi/v1/exchangeInfo")

    # ── 账户接口（签名）───────────────────────────────────────────

    def get_balance(self) -> float:
        """返回 USDT 可用余额"""
        data = self._request_with_retry("GET", "/fapi/v2/balance", signed=True)
        for item in data:
            if item["asset"] == "USDT":
                return float(item["availableBalance"])
        return 0.0

    def get_positions(self) -> list:
        """返回所有非零持仓列表"""
        data = self._request_with_retry("GET", "/fapi/v2/positionRisk", signed=True)
        return [p for p in data if float(p["positionAmt"]) != 0]

    def get_all_open_orders(self) -> list:
        return self._request_with_retry("GET", "/fapi/v1/openOrders", signed=True)

    # ── 交易接口 ──────────────────────────────────────────────────

    def _get_symbol_filters(self, symbol: str) -> dict:
        if symbol not in self._exchange_info:
            info = self.get_exchange_info()
            for s in info.get("symbols", []):
                self._exchange_info[s["symbol"]] = {
                    f["filterType"]: f for f in s.get("filters", [])
                }
        return self._exchange_info.get(symbol, {})

    def normalize_quantity(self, symbol: str, qty: float) -> Optional[Decimal]:
        """按交易所步进规整数量，返回 Decimal 或 None（数量不足）"""
        filters = self._get_symbol_filters(symbol)
        lot = filters.get("LOT_SIZE", {})
        step = Decimal(str(lot.get("stepSize", "0.001")))
        min_qty = Decimal(str(lot.get("minQty", "0.001")))
        qty_d = Decimal(str(qty)).quantize(step, rounding=ROUND_DOWN)
        if qty_d < min_qty:
            return None
        return qty_d

    def normalize_price(self, symbol: str, price: float) -> Decimal:
        filters = self._get_symbol_filters(symbol)
        pf = filters.get("PRICE_FILTER", {})
        tick = Decimal(str(pf.get("tickSize", "0.01")))
        return Decimal(str(price)).quantize(tick, rounding=ROUND_DOWN)

    def place_market_order(self, symbol: str, side: str, quantity: Decimal,
                           reduce_only: bool = False) -> dict:
        """市价开/平仓"""
        data = {
            "symbol": symbol,
            "side": side.upper(),
            "type": "MARKET",
            "quantity": str(quantity),
        }
        if reduce_only:
            data["reduceOnly"] = "true"
        return self._request_with_retry("POST", "/fapi/v1/order", signed=True, data=data)

    def place_stop_order(self, symbol: str, side: str, quantity: Decimal,
                         stop_price: Decimal, order_type: str = "STOP_MARKET") -> dict:
        """止损单（STOP_MARKET）"""
        data = {
            "symbol": symbol,
            "side": side.upper(),
            "type": order_type,
            "stopPrice": str(stop_price),
            "quantity": str(quantity),
            "reduceOnly": "true",
            "workingType": "MARK_PRICE",
        }
        return self._request_with_retry("POST", "/fapi/v1/order", signed=True, data=data)

    def place_take_profit_order(self, symbol: str, side: str, quantity: Decimal,
                                stop_price: Decimal) -> dict:
        """止盈单（TAKE_PROFIT_MARKET）"""
        data = {
            "symbol": symbol,
            "side": side.upper(),
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": str(stop_price),
            "quantity": str(quantity),
            "reduceOnly": "true",
            "workingType": "MARK_PRICE",
        }
        return self._request_with_retry("POST", "/fapi/v1/order", signed=True, data=data)

    def cancel_all_orders(self, symbol: str) -> dict:
        return self._request_with_retry("DELETE", "/fapi/v1/allOpenOrders",
                                        signed=True, data={"symbol": symbol})

    def set_leverage(self, symbol: str, leverage: int) -> dict:
        return self._request_with_retry("POST", "/fapi/v1/leverage",
                                        signed=True, data={"symbol": symbol, "leverage": leverage})
