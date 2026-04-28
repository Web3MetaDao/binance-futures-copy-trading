"""
health.py — 健康检查与自动告警模块
监控以下异常并通过 Telegram 推送告警：
  1. 引擎心跳超时（超过 N 分钟无轮询）
  2. 账户余额低于安全线
  3. API 连续失败超过阈值
  4. 熔断触发
  5. WebSocket 断线超时

审计修复记录：
  FIX-C01: _last_balance 初始值为 0.0，系统启动时余额尚未更新，
            原代码 `_last_balance > 0` 条件虽有一定保护，但若余额恰好为 0
            （账户清空）则永远不会告警；改为 _balance_initialized 标志更严谨
  FIX-C02: _alerted / _api_error_count 等状态变量在多线程下无锁访问
            （主循环写、检查线程读），存在竞态条件
            → 所有状态变量读写均加 _state_lock 保护
  FIX-C03: stop() 后 _thread 未 join，进程退出时检查线程可能仍在 sleep
            → stop() 中 join(timeout=5) 等待线程退出
  FIX-C04: notifier.send() 若抛出异常会导致检查线程崩溃，后续所有告警失效
            → 封装 _safe_notify()，捕获所有异常
"""
import logging
import threading
import time
from typing import Optional

from core.config import Config

logger = logging.getLogger("health")


class HealthChecker:
    """
    后台健康检查线程，定期检测系统状态并触发告警。
    """

    def __init__(self, notifier):
        self.notifier = notifier

        # [FIX-C02] 所有可变状态统一由 _state_lock 保护
        self._state_lock = threading.Lock()
        self._last_heartbeat: float = time.time()
        self._last_balance: float = 0.0
        self._balance_initialized: bool = False   # [FIX-C01]
        self._circuit_broken: bool = False
        self._ws_connected: bool = True
        self._api_error_count: int = 0

        # 告警状态（防止重复告警）
        self._alerted: dict = {
            "heartbeat": False,
            "balance": False,
            "circuit": False,
            "ws": False,
            "api_error": False,
        }

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """启动健康检查后台线程"""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._check_loop, daemon=True, name="HealthChecker"
        )
        self._thread.start()
        logger.info("HealthChecker 已启动")

    def stop(self):
        """停止健康检查线程"""
        self._stop_event.set()
        # [FIX-C03] join 等待线程退出，避免进程退出时仍有告警发送
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    # ── 状态更新接口（由主循环调用）──────────────────────────────────

    def heartbeat(self):
        """每轮轮询完成后调用，更新心跳时间"""
        with self._state_lock:
            self._last_heartbeat = time.time()
            self._alerted["heartbeat"] = False  # 心跳恢复，重置告警状态

    def update_balance(self, balance: float):
        with self._state_lock:
            self._last_balance = balance
            self._balance_initialized = True   # [FIX-C01] 标记余额已初始化
            if balance > Config.HEALTH_BALANCE_MIN_USDT:
                self._alerted["balance"] = False  # 余额恢复

    def update_circuit(self, broken: bool):
        with self._state_lock:
            if not broken:
                self._alerted["circuit"] = False  # 熔断恢复
            self._circuit_broken = broken

    def update_ws(self, connected: bool):
        with self._state_lock:
            if connected:
                self._alerted["ws"] = False
            self._ws_connected = connected

    def record_api_error(self):
        with self._state_lock:
            self._api_error_count += 1

    def reset_api_errors(self):
        with self._state_lock:
            self._api_error_count = 0
            self._alerted["api_error"] = False

    # ── 内部检查循环 ──────────────────────────────────────────────────

    def _check_loop(self):
        while not self._stop_event.wait(Config.HEALTH_CHECK_INTERVAL):
            self._check_heartbeat()
            self._check_balance()
            self._check_circuit()
            self._check_ws()
            self._check_api_errors()

    def _check_heartbeat(self):
        with self._state_lock:
            elapsed = time.time() - self._last_heartbeat
            timeout = Config.HEALTH_HEARTBEAT_TIMEOUT
            already_alerted = self._alerted["heartbeat"]

        if elapsed > timeout and not already_alerted:
            with self._state_lock:
                self._alerted["heartbeat"] = True
            msg = (
                f"⚠️ 【心跳超时告警】\n"
                f"引擎已超过 {elapsed / 60:.1f} 分钟未响应\n"
                f"阈值: {timeout / 60:.0f} 分钟\n"
                f"请检查服务器状态！"
            )
            logger.error(msg)
            self._safe_notify(msg)

    def _check_balance(self):
        with self._state_lock:
            initialized = self._balance_initialized
            balance = self._last_balance
            min_bal = Config.HEALTH_BALANCE_MIN_USDT
            already_alerted = self._alerted["balance"]

        # [FIX-C01] 未初始化时跳过检查，避免误告警
        if not initialized:
            return

        if balance < min_bal and not already_alerted:
            with self._state_lock:
                self._alerted["balance"] = True
            msg = (
                f"⚠️ 【余额不足告警】\n"
                f"当前余额: {balance:.2f} USDT\n"
                f"安全线: {min_bal:.2f} USDT\n"
                f"请及时补充保证金！"
            )
            logger.warning(msg)
            self._safe_notify(msg)

    def _check_circuit(self):
        with self._state_lock:
            broken = self._circuit_broken
            already_alerted = self._alerted["circuit"]

        if broken and not already_alerted:
            with self._state_lock:
                self._alerted["circuit"] = True
            msg = (
                f"🔴 【熔断触发告警】\n"
                f"最大回撤超过 {Config.MAX_DRAWDOWN_PCT * 100:.0f}%\n"
                f"系统已暂停开仓，{Config.CIRCUIT_BREAKER_MINUTES} 分钟后自动恢复\n"
                f"请检查市场状况！"
            )
            logger.warning(msg)
            self._safe_notify(msg)

    def _check_ws(self):
        with self._state_lock:
            connected = self._ws_connected
            already_alerted = self._alerted["ws"]

        if not connected and not already_alerted:
            with self._state_lock:
                self._alerted["ws"] = True
            msg = (
                f"⚠️ 【WebSocket 断线告警】\n"
                f"实时数据流已断开，正在尝试重连\n"
                f"期间将降级为轮询模式"
            )
            logger.warning(msg)
            self._safe_notify(msg)

    def _check_api_errors(self):
        with self._state_lock:
            count = self._api_error_count
            threshold = Config.HEALTH_API_ERROR_THRESHOLD
            already_alerted = self._alerted["api_error"]

        if count >= threshold and not already_alerted:
            with self._state_lock:
                self._alerted["api_error"] = True
            msg = (
                f"⚠️ 【API 错误告警】\n"
                f"连续 API 错误: {count} 次\n"
                f"阈值: {threshold} 次\n"
                f"请检查网络连接和 API Key 状态！"
            )
            logger.error(msg)
            self._safe_notify(msg)

    def _safe_notify(self, msg: str):
        """[FIX-C04] 安全调用 notifier，捕获所有异常防止检查线程崩溃"""
        try:
            self.notifier.send(msg)
        except Exception as e:
            logger.warning(f"[HealthChecker] 告警发送失败: {e}")
