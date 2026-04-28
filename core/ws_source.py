"""
ws_source.py — WebSocket 实时持仓推送模块
通过 Binance User Data Stream 监听账户仓位变化，毫秒级响应，替代轮询。
同时维护一个本地仓位快照，供引擎直接读取。

架构：
  - ListenKeyManager: 维护 listenKey 的创建/续期/关闭
  - UserDataStream:   WebSocket 连接，解析 ORDER_TRADE_UPDATE / ACCOUNT_UPDATE
  - PositionCache:    线程安全的本地仓位快照

审计修复记录：
  FIX-A01: WS_BASE 使用类变量在类定义时求值，导致 Config.TESTNET 修改后不生效
            → 改为 property，运行时动态读取 Config.TESTNET
  FIX-A02: _run_with_reconnect 重连时重复调用 _lk_manager.start()，
            导致旧 listenKey 未关闭（资源泄漏）
            → 每次重连前先调用 _lk_manager.stop() 关闭旧 key
  FIX-A03: _on_message 中 on_position_update 回调在锁外调用，
            若回调耗时会阻塞 WebSocket 消息处理线程
            → 改为在独立线程中调用回调
  FIX-A04: PositionCache.update 中 float() 转换可能抛出 ValueError
            （positionAmt 为非数字字符串时）→ 加 try/except 保护
  FIX-A05: _sync_positions 在 _on_open 中同步调用，会阻塞 WebSocket 心跳线程
            → 改为在独立线程中异步执行
  FIX-A06: unused import requests（ws_source 不直接使用 requests）
            → 移除
"""
import json
import logging
import threading
import time
from typing import Optional, Callable

import websocket  # websocket-client

from core.config import Config
from core.binance_client import BinanceFuturesClient

logger = logging.getLogger("ws_source")

# ListenKey 续期间隔（Binance 要求每 30 分钟续期一次，有效期 60 分钟）
_LISTEN_KEY_RENEW_INTERVAL = 1800  # 30 min


class ListenKeyManager:
    """管理 Binance User Data Stream 的 listenKey 生命周期"""

    def __init__(self, client: BinanceFuturesClient):
        self.client = client
        self._listen_key: Optional[str] = None
        self._renew_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> str:
        """创建 listenKey 并启动自动续期线程"""
        self._listen_key = self._create_listen_key()
        self._stop_event.clear()
        self._renew_thread = threading.Thread(
            target=self._renew_loop, daemon=True, name="ListenKeyRenewer"
        )
        self._renew_thread.start()
        logger.info(f"ListenKey 已创建: {self._listen_key[:8]}...")
        return self._listen_key

    def stop(self):
        """停止续期并关闭 listenKey"""
        self._stop_event.set()
        if self._listen_key:
            try:
                self._close_listen_key(self._listen_key)
                logger.info("ListenKey 已关闭")
            except Exception as e:
                logger.warning(f"关闭 ListenKey 失败: {e}")
            finally:
                # [FIX-A02] 确保 key 被清空，防止重连时重复关闭
                self._listen_key = None

    def _create_listen_key(self) -> str:
        resp = self.client._request_with_retry("POST", "/fapi/v1/listenKey", signed=False)
        return resp["listenKey"]

    def _renew_listen_key(self, listen_key: str):
        self.client._request_with_retry(
            "PUT", "/fapi/v1/listenKey", signed=False,
            data={"listenKey": listen_key}
        )

    def _close_listen_key(self, listen_key: str):
        self.client._request_with_retry(
            "DELETE", "/fapi/v1/listenKey", signed=False,
            data={"listenKey": listen_key}
        )

    def _renew_loop(self):
        while not self._stop_event.wait(_LISTEN_KEY_RENEW_INTERVAL):
            try:
                if self._listen_key:
                    self._renew_listen_key(self._listen_key)
                    logger.debug("ListenKey 已续期")
            except Exception as e:
                logger.warning(f"ListenKey 续期失败: {e}")


class PositionCache:
    """
    线程安全的本地仓位快照缓存
    存储格式与 BinanceFuturesClient.get_positions() 返回格式一致
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._positions: dict = {}  # symbol -> position dict

    def update(self, symbol: str, position: dict):
        # [FIX-A04] 保护 float() 转换，防止非数字字符串导致崩溃
        try:
            amt = float(position.get("positionAmt", 0))
        except (ValueError, TypeError):
            logger.warning(f"[PositionCache] {symbol} positionAmt 解析失败: "
                           f"{position.get('positionAmt')!r}，跳过更新")
            return
        with self._lock:
            if amt == 0:
                self._positions.pop(symbol, None)
            else:
                self._positions[symbol] = position

    def get_all(self) -> list:
        with self._lock:
            return list(self._positions.values())

    def get(self, symbol: str) -> Optional[dict]:
        with self._lock:
            return self._positions.get(symbol)

    def clear(self):
        with self._lock:
            self._positions.clear()


class UserDataStream:
    """
    Binance User Data Stream WebSocket 客户端
    监听 ORDER_TRADE_UPDATE 和 ACCOUNT_UPDATE 事件，实时更新本地仓位缓存。
    """

    def __init__(self, client: BinanceFuturesClient,
                 on_position_update: Optional[Callable] = None):
        self.client = client
        self.on_position_update = on_position_update  # 仓位变化回调
        self.position_cache = PositionCache()
        self._lk_manager = ListenKeyManager(client)
        self._ws: Optional[websocket.WebSocketApp] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._running = False
        self._reconnect_delay = 5  # 断线重连初始等待秒数
        self._connected = False    # 连接状态（供 health checker 读取）

    @property
    def ws_base(self) -> str:
        # [FIX-A01] 运行时动态读取 Config.TESTNET，而非类定义时求值
        return (
            "wss://stream.binancefuture.com"
            if Config.TESTNET
            else "wss://fstream.binance.com"
        )

    @property
    def is_connected(self) -> bool:
        return self._connected

    def start(self):
        """启动 WebSocket 连接（非阻塞）"""
        self._running = True
        self._ws_thread = threading.Thread(
            target=self._run_with_reconnect, daemon=True, name="UserDataStream"
        )
        self._ws_thread.start()
        logger.info("UserDataStream 已启动")

    def stop(self):
        """停止 WebSocket 连接"""
        self._running = False
        self._connected = False
        self._lk_manager.stop()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        logger.info("UserDataStream 已停止")

    def _run_with_reconnect(self):
        """带指数退避重连的 WebSocket 运行循环"""
        delay = self._reconnect_delay
        while self._running:
            try:
                # [FIX-A02] 每次重连前先关闭旧 listenKey，再创建新的
                self._lk_manager.stop()
                listen_key = self._lk_manager.start()
                ws_url = f"{self.ws_base}/ws/{listen_key}"
                self._ws = websocket.WebSocketApp(
                    ws_url,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                    on_open=self._on_open,
                )
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
                delay = self._reconnect_delay  # 成功连接后重置延迟
            except Exception as e:
                logger.error(f"WebSocket 异常: {e}")
            finally:
                self._connected = False
            if self._running:
                logger.info(f"WebSocket 断线，{delay}s 后重连...")
                time.sleep(delay)
                delay = min(delay * 2, 60)  # 指数退避，最大 60s

    def _on_open(self, ws):
        self._connected = True
        logger.info("WebSocket 连接已建立")
        # [FIX-A05] 异步执行仓位同步，避免阻塞 WebSocket 心跳线程
        threading.Thread(
            target=self._sync_positions, daemon=True, name="WSSyncPositions"
        ).start()

    def _on_close(self, ws, close_status_code, close_msg):
        self._connected = False
        logger.warning(f"WebSocket 连接关闭: {close_status_code} {close_msg}")

    def _on_error(self, ws, error):
        self._connected = False
        logger.error(f"WebSocket 错误: {error}")

    def _on_message(self, ws, message: str):
        try:
            data = json.loads(message)
            event_type = data.get("e")

            if event_type == "ACCOUNT_UPDATE":
                # 账户更新事件，包含仓位变化
                positions = data.get("a", {}).get("P", [])
                updated_symbols = []
                for pos in positions:
                    symbol = pos.get("s")
                    if not symbol:
                        continue
                    position = {
                        "symbol": symbol,
                        "positionAmt": pos.get("pa", "0"),
                        "entryPrice": pos.get("ep", "0"),
                        # WS ACCOUNT_UPDATE 中无 markPrice，使用 entryPrice 近似
                        # 引擎在使用前应通过 REST 补充精确 markPrice
                        "markPrice": pos.get("ep", "0"),
                        "unRealizedProfit": pos.get("up", "0"),
                        "leverage": pos.get("l", "1"),
                        "positionSide": pos.get("ps", "BOTH"),
                    }
                    self.position_cache.update(symbol, position)
                    logger.debug(f"[WS] 仓位更新: {symbol} amt={pos.get('pa')}")
                    updated_symbols.append((symbol, position))

                # [FIX-A03] 在独立线程中执行回调，避免阻塞 WebSocket 消息线程
                if self.on_position_update and updated_symbols:
                    def _fire_callbacks(items):
                        for sym, pos_data in items:
                            try:
                                self.on_position_update(sym, pos_data)
                            except Exception as cb_err:
                                logger.warning(
                                    f"[WS] 仓位回调异常 [{sym}]: {cb_err}"
                                )
                    threading.Thread(
                        target=_fire_callbacks,
                        args=(updated_symbols,),
                        daemon=True,
                        name="WSCallback"
                    ).start()

            elif event_type == "ORDER_TRADE_UPDATE":
                # 订单成交事件
                order = data.get("o", {})
                symbol = order.get("s")
                status = order.get("X")  # 订单状态
                side = order.get("S")
                qty = order.get("q")
                price = order.get("ap", "0")  # 成交均价
                if status == "FILLED":
                    logger.info(
                        f"[WS] 订单成交: {symbol} {side} qty={qty} price={price}"
                    )

        except json.JSONDecodeError as e:
            logger.warning(f"[WS] JSON 解析失败: {e}, raw={message[:200]}")
        except Exception as e:
            logger.warning(f"[WS] 消息处理异常: {e}, raw={message[:200]}")

    def _sync_positions(self):
        """同步完整仓位快照（WebSocket 连接建立时异步调用）"""
        try:
            positions = self.client.get_positions()
            self.position_cache.clear()
            for pos in positions:
                symbol = pos.get("symbol")
                if symbol:
                    self.position_cache.update(symbol, pos)
            logger.info(f"[WS] 仓位快照同步完成: {len(positions)} 个持仓")
        except Exception as e:
            logger.warning(f"[WS] 仓位快照同步失败: {e}")
