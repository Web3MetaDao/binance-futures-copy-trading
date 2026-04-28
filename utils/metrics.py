"""
metrics.py — Prometheus 监控指标暴露模块
暴露 /metrics 端点，供 Prometheus 抓取，配合 Grafana 可视化。

指标列表：
  - copytrader_balance_usdt          账户余额
  - copytrader_open_positions_total  当前持仓数
  - copytrader_trades_open_total     累计开仓笔数（counter）
  - copytrader_trades_close_total    累计平仓笔数（counter）
  - copytrader_pnl_pct               最近一笔 PnL%（按 symbol）
  - copytrader_win_rate              滚动胜率
  - copytrader_sharpe                滚动夏普率
  - copytrader_pl_ratio              滚动盈亏比
  - copytrader_circuit_broken        熔断状态（0/1）
  - copytrader_api_errors_total      API 错误计数（counter）
  - copytrader_signal_score          最近信号评分（按 symbol）
  - copytrader_ws_connected          WebSocket 连接状态（0/1）
  - copytrader_poll_latency_seconds  每轮轮询耗时

审计修复记录：
  FIX-B01: generate_prometheus_text 在 _lock 持有期间拼接大量字符串，
            若调用方耗时会导致其他线程长时间阻塞
            → 先在锁内拷贝数据快照，锁外拼接字符串
  FIX-B02: labeled gauge 的 label 值未做转义，若 symbol 含双引号会破坏
            Prometheus 文本格式（注入风险）
            → 对 label 值进行转义
  FIX-B03: _counters 使用 dict.get(name, 0) + delta 模式，若 name 不存在
            会静默创建新键，导致 /metrics 输出不一致
            → 改为只允许更新预定义的 counter 键，未知键记录 warning
"""
import logging
import threading
from typing import Dict

logger = logging.getLogger("metrics")

# ── 内部指标存储 ──────────────────────────────────────────────────────────────
_lock = threading.Lock()

_gauges: Dict[str, float] = {
    "balance_usdt": 0.0,
    "open_positions_total": 0.0,
    "win_rate": 0.0,
    "sharpe": 0.0,
    "pl_ratio": 0.0,
    "circuit_broken": 0.0,
    "ws_connected": 0.0,
    "poll_latency_seconds": 0.0,
}

# [FIX-B03] 预定义所有合法 counter 键，防止动态创建导致输出不一致
_COUNTER_KEYS = {"trades_open_total", "trades_close_total", "api_errors_total"}
_counters: Dict[str, int] = {k: 0 for k in _COUNTER_KEYS}

_labeled_gauges: Dict[str, Dict[str, float]] = {
    "signal_score": {},   # symbol -> score
    "pnl_pct": {},        # symbol -> last pnl_pct
}


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _escape_label_value(value: str) -> str:
    """[FIX-B02] 转义 Prometheus label 值中的特殊字符"""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


# ── 更新接口 ──────────────────────────────────────────────────────────────────

def set_gauge(name: str, value: float):
    with _lock:
        if name in _gauges:
            _gauges[name] = float(value)
        else:
            logger.debug(f"[metrics] 未知 gauge 键: {name}，已忽略")


def inc_counter(name: str, delta: int = 1):
    # [FIX-B03] 只允许更新预定义的 counter 键
    with _lock:
        if name in _COUNTER_KEYS:
            _counters[name] += delta
        else:
            logger.warning(f"[metrics] 未知 counter 键: {name}，已忽略")


def set_labeled_gauge(metric: str, label: str, value: float):
    with _lock:
        if metric not in _labeled_gauges:
            _labeled_gauges[metric] = {}
        _labeled_gauges[metric][label] = float(value)


def update_from_runtime(balance: float, positions: list, perf: dict,
                        circuit_broken: bool, ws_connected: bool = False,
                        poll_latency: float = 0.0):
    """由主循环调用，批量更新运行时指标"""
    with _lock:
        _gauges["balance_usdt"] = float(balance)
        _gauges["open_positions_total"] = float(len(positions))
        _gauges["win_rate"] = float(perf.get("win_rate", 0.0))
        _gauges["sharpe"] = float(perf.get("sharpe", 0.0))
        _gauges["pl_ratio"] = float(perf.get("pl_ratio", 0.0))
        _gauges["circuit_broken"] = 1.0 if circuit_broken else 0.0
        _gauges["ws_connected"] = 1.0 if ws_connected else 0.0
        _gauges["poll_latency_seconds"] = float(poll_latency)


def record_trade(action: str, symbol: str, pnl_pct: float = None):
    """记录交易事件"""
    with _lock:
        if action == "OPEN":
            _counters["trades_open_total"] += 1
        elif action == "CLOSE":
            _counters["trades_close_total"] += 1
            if pnl_pct is not None:
                _labeled_gauges["pnl_pct"][symbol] = float(pnl_pct)


def record_signal_score(symbol: str, score: float):
    """记录信号评分"""
    with _lock:
        _labeled_gauges["signal_score"][symbol] = float(score)


def record_api_error():
    """记录 API 错误"""
    with _lock:
        _counters["api_errors_total"] += 1


# ── Prometheus 文本格式生成 ──────────────────────────────────────────────────

def generate_prometheus_text() -> str:
    """
    生成 Prometheus exposition format 文本
    [FIX-B01] 先在锁内拷贝数据快照，锁外拼接字符串，减少锁持有时间
    """
    # 在锁内做浅拷贝
    with _lock:
        gauges_snap = dict(_gauges)
        counters_snap = dict(_counters)
        labeled_snap = {
            metric: dict(labels)
            for metric, labels in _labeled_gauges.items()
        }

    # 锁外拼接（不再持有 _lock）
    lines = []
    prefix = "copytrader"

    for name, value in gauges_snap.items():
        metric_name = f"{prefix}_{name}"
        lines.append(f"# HELP {metric_name} {name.replace('_', ' ')}")
        lines.append(f"# TYPE {metric_name} gauge")
        lines.append(f"{metric_name} {value}")

    for name, value in counters_snap.items():
        metric_name = f"{prefix}_{name}"
        lines.append(f"# HELP {metric_name} {name.replace('_', ' ')}")
        lines.append(f"# TYPE {metric_name} counter")
        lines.append(f"{metric_name}_total {value}")

    for metric, labels in labeled_snap.items():
        metric_name = f"{prefix}_{metric}"
        lines.append(f"# HELP {metric_name} {metric.replace('_', ' ')} by symbol")
        lines.append(f"# TYPE {metric_name} gauge")
        for label, value in labels.items():
            # [FIX-B02] 转义 label 值
            safe_label = _escape_label_value(label)
            lines.append(f'{metric_name}{{symbol="{safe_label}"}} {value}')

    return "\n".join(lines) + "\n"
