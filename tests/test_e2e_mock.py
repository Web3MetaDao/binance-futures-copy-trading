"""
test_e2e_mock.py — 端到端 Mock 运行验证
模拟完整的信号抓取 → 评分 → 执行 → 状态持久化流程，无需真实 API Key。
"""
import sys
import os
import json
import tempfile
import threading
import time
from decimal import Decimal
from unittest.mock import MagicMock, patch, PropertyMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.config import Config
from core.performance import PerformanceTracker
from core.backtest import run_backtest
from core.smart_money_source import DualSourceRouter, SmartMoneySource
from utils.state import StateManager
from utils.logger import trade_log, read_trade_logs

PASS = []
FAIL = []


def ok(name):
    PASS.append(name)
    print(f"  ✅ {name}")


def fail(name, reason):
    FAIL.append(name)
    print(f"  ❌ {name}: {reason}")


# ─────────────────────────────────────────────────────────────────────────────
# 1. PerformanceTracker 完整性
# ─────────────────────────────────────────────────────────────────────────────
print("\n[1] PerformanceTracker")
try:
    pt = PerformanceTracker(window=10)
    assert pt.sample_count == 0
    assert pt.pl_ratio() == 1.5  # 无数据默认值
    assert pt.sharpe() == 1.5
    assert pt.win_rate() == 0.0
    for r in [0.05, -0.02, 0.08, -0.01, 0.03]:
        pt.record(r)
    assert pt.sample_count == 5
    assert pt.win_rate() == 0.6
    assert pt.pl_ratio() > 0
    d = pt.dump()
    pt2 = PerformanceTracker()
    pt2.load(d)
    assert pt2.sample_count == 5
    ok("PerformanceTracker 基本运算 + 序列化/反序列化")
except Exception as e:
    fail("PerformanceTracker", e)

# ─────────────────────────────────────────────────────────────────────────────
# 2. StateManager 原子写入
# ─────────────────────────────────────────────────────────────────────────────
print("\n[2] StateManager")
try:
    with tempfile.TemporaryDirectory() as d:
        sm = StateManager(os.path.join(d, "state.json"))
        data = {"balance": 1234.56, "positions": ["BTCUSDT"], "nested": {"a": 1}}
        sm.save(data)
        loaded = sm.load()
        assert loaded == data, f"期望 {data} 但得到 {loaded}"
        ok("StateManager 原子写入/读取")

        # 模拟损坏文件
        path = os.path.join(d, "state.json")
        with open(path, "w") as f:
            f.write("{broken json")
        result = sm.load()
        assert result == {}, f"损坏文件应返回空字典，但得到 {result}"
        ok("StateManager 损坏文件降级处理")
except Exception as e:
    fail("StateManager", e)

# ─────────────────────────────────────────────────────────────────────────────
# 3. trade_log / read_trade_logs
# ─────────────────────────────────────────────────────────────────────────────
print("\n[3] Logger")
try:
    import utils.logger as ul
    with tempfile.TemporaryDirectory() as d:
        ul._TRADE_LOG_FILE = os.path.join(d, "trades.jsonl")
        for i in range(10):
            ul.trade_log("OPEN", "BTCUSDT", "BUY", 0.001 * (i + 1), 65000 + i * 100,
                         score=0.8, source_chain="primary")
        ul.trade_log("CLOSE", "BTCUSDT", "SELL", 0.005, 66000, pnl_pct=0.015)
        logs = ul.read_trade_logs(limit=5)
        assert len(logs) == 5, f"期望 5 条，得到 {len(logs)}"
        assert logs[0]["action"] == "CLOSE", "最新一条应为 CLOSE"
        assert logs[0]["pnl_pct"] == 0.015
        ok("trade_log 写入 + read_trade_logs 逐行读取（limit）")

        # 并发写入测试
        results = []
        def write_logs(n):
            for _ in range(n):
                ul.trade_log("OPEN", "ETHUSDT", "BUY", 0.01, 3000)
        threads = [threading.Thread(target=write_logs, args=(20,)) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        all_logs = ul.read_trade_logs(limit=9999)
        # 11 条原有 + 100 条并发 = 111 条
        assert len(all_logs) == 111, f"并发写入后期望 111 条，得到 {len(all_logs)}"
        # 验证每行都是合法 JSON（无交错）
        for log in all_logs:
            assert isinstance(log, dict)
        ok("trade_log 并发写入安全性（5线程×20条）")
except Exception as e:
    fail("Logger", e)

# ─────────────────────────────────────────────────────────────────────────────
# 4. DualSourceRouter 双链路切换逻辑
# ─────────────────────────────────────────────────────────────────────────────
print("\n[4] DualSourceRouter")
try:
    Config.SM_SOURCE_ENABLED = True
    Config.SM_SIGNAL_MERGE_MODE = "fallback"
    Config.SM_FALLBACK_THRESHOLD = 3
    Config.SM_MERGE_WEIGHT_FACTOR = 0.6

    class MockPrimary:
        trackers = []
        def __init__(self, signals, raise_exc=False):
            self._signals = signals
            self._raise = raise_exc
        def fetch_all(self):
            if self._raise:
                raise RuntimeError("主链路模拟异常")
            return self._signals

    class MockFallback:
        def fetch_all(self):
            return {
                "BTCUSDT": {"side": "LONG", "weight": 0.8, "sources": ["SM1"],
                            "avg_leverage": 10, "avg_notional": 500, "from_smart_money": True},
                "ETHUSDT": {"side": "SHORT", "weight": 0.6, "sources": ["SM2"],
                            "avg_leverage": 5, "avg_notional": 200, "from_smart_money": True},
            }

    # 测试 4a：主链路正常
    r = DualSourceRouter(MockPrimary({"SOLUSDT": {"side": "LONG", "weight": 1.0,
                                                   "sources": ["lanaai"], "avg_leverage": 20,
                                                   "avg_notional": 1000}}), MockFallback())
    sigs = r.fetch_signals()
    assert len(sigs) == 1 and "SOLUSDT" in sigs
    assert sigs["SOLUSDT"]["source_chain"] == "primary"
    ok("DualSourceRouter fallback模式：主链路正常时只用主链路")

    # 测试 4b：主链路连续空信号超阈值，切换备用
    r2 = DualSourceRouter(MockPrimary({}), MockFallback())
    r2._primary_fail_threshold = 2
    for _ in range(3):
        sigs2 = r2.fetch_signals()
    assert len(sigs2) == 2
    assert all(s["source_chain"] == "smart_money" for s in sigs2.values())
    ok("DualSourceRouter fallback模式：连续空信号超阈值切换备用")

    # 测试 4c：fallback 模式备用信号不降权
    assert sigs2["BTCUSDT"]["weight"] == 0.8, f"fallback模式不应降权，但 weight={sigs2['BTCUSDT']['weight']}"
    ok("DualSourceRouter fallback模式：备用信号不降权（FIX-14）")

    # 测试 4d：merge 模式信号融合
    Config.SM_SIGNAL_MERGE_MODE = "merge"
    r3 = DualSourceRouter(MockPrimary({"SOLUSDT": {"side": "LONG", "weight": 1.0,
                                                    "sources": ["lanaai"], "avg_leverage": 20,
                                                    "avg_notional": 1000}}), MockFallback())
    sigs3 = r3.fetch_signals()
    assert "SOLUSDT" in sigs3 and "BTCUSDT" in sigs3 and "ETHUSDT" in sigs3
    assert sigs3["SOLUSDT"]["source_chain"] == "primary"
    assert sigs3["BTCUSDT"]["source_chain"] == "smart_money"
    assert abs(sigs3["BTCUSDT"]["weight"] - 0.8 * 0.6) < 0.001, \
        f"merge模式备用信号应降权，但 weight={sigs3['BTCUSDT']['weight']}"
    ok("DualSourceRouter merge模式：主备信号融合 + 备用降权")

    # 测试 4e：主链路异常
    Config.SM_SIGNAL_MERGE_MODE = "fallback"
    r4 = DualSourceRouter(MockPrimary({}, raise_exc=True), MockFallback())
    r4._primary_fail_threshold = 1
    sigs4 = r4.fetch_signals()
    assert len(sigs4) == 2
    ok("DualSourceRouter：主链路异常时切换备用")

except Exception as e:
    fail("DualSourceRouter", e)

# ─────────────────────────────────────────────────────────────────────────────
# 5. CopyEngine Mock 端到端
# ─────────────────────────────────────────────────────────────────────────────
print("\n[5] CopyEngine 端到端 Mock")
try:
    with tempfile.TemporaryDirectory() as d:
        Config.STATE_FILE = os.path.join(d, "state.json")
        Config.COMPOUND_ENABLED = True
        Config.FIXED_FOLLOWER_ENABLED = False
        Config.CAPITAL_RATIO = 0.1
        Config.RISK_MULTIPLIER = 1.0
        Config.MAX_NOTIONAL_PER_SYMBOL = 500.0
        Config.MAX_TOTAL_NOTIONAL = 2000.0
        Config.STOP_LOSS_PCT = 0.05
        Config.TAKE_PROFIT_PCT = 0.10
        Config.SMART_MONEY_ENABLED = False  # 跳过 SM 评分，直接通过
        Config.SMART_MONEY_MIN_SCORE = 0.0
        Config.EMPTY_SNAPSHOT_TOLERANCE = 3
        Config.MAX_DRAWDOWN_PCT = 0.15
        Config.CIRCUIT_BREAKER_MINUTES = 60
        Config.NO_REVERSE = True
        Config.SYMBOL_WHITELIST = []
        Config.SYMBOL_BLACKLIST = []
        Config.TARGET_PL_RATIO_MIN = 1.5
        Config.TARGET_PL_RATIO_MAX = 3.0
        Config.TARGET_SHARPE_MIN = 1.5
        Config.TARGET_SHARPE_MAX = 3.0
        Config.PERFORMANCE_WINDOW = 20
        Config.TELEGRAM_ENABLED = False

        # Mock BinanceFuturesClient
        mock_client = MagicMock()
        mock_client.get_balance.return_value = 1000.0
        mock_client.get_positions.return_value = []
        mock_client.get_mark_price.return_value = 65000.0
        mock_client.normalize_quantity.return_value = Decimal("0.001")
        mock_client.normalize_price.return_value = Decimal("65000")
        mock_client.place_market_order.return_value = {"orderId": 12345}
        mock_client.place_stop_order.return_value = {"orderId": 12346}
        mock_client.place_take_profit_order.return_value = {"orderId": 12347}
        mock_client.cancel_all_orders.return_value = {}

        # Mock Notifier
        mock_notifier = MagicMock()

        from core.engine import CopyEngine
        engine = CopyEngine(mock_client, mock_notifier)

        # 首次运行：无持仓，有信号 → 应开仓
        signals = {
            "BTCUSDT": {
                "side": "LONG", "weight": 1.0, "sources": ["lanaai"],
                "avg_leverage": 20, "avg_notional": 1000, "source_chain": "primary"
            }
        }
        engine.run_once(signals)
        assert mock_client.place_market_order.called, "应调用 place_market_order"
        assert mock_client.place_stop_order.called, "应调用 place_stop_order"
        assert mock_client.place_take_profit_order.called, "应调用 place_take_profit_order"
        ok("CopyEngine 首次运行开仓 + 止损止盈挂单")

        # 第二轮：有持仓，信号消失 → 应平仓
        mock_client.get_positions.return_value = [{
            "symbol": "BTCUSDT",
            "positionAmt": "0.001",
            "entryPrice": "65000",
            "markPrice": "66000",
            "unRealizedProfit": "1.0",
            "leverage": "20",
        }]
        mock_client.place_market_order.reset_mock()
        engine.run_once({})  # 空信号，但 EMPTY_SNAPSHOT_TOLERANCE=3，前3次跳过
        engine.run_once({})
        engine.run_once({})
        engine.run_once({})  # 第4次，超过阈值，应平仓
        assert mock_client.place_market_order.called, "应调用 place_market_order 平仓"
        ok("CopyEngine 空快照保护 + 超阈值后平仓")

        # 验证复利基础资金更新
        assert engine._compound_base is not None, "_compound_base 不应为 None"
        ok("CopyEngine 真复利基础资金更新")

        # 验证状态持久化
        state = StateManager(Config.STATE_FILE).load()
        assert "compound_base" in state, "状态文件应包含 compound_base"
        assert "performance" in state, "状态文件应包含 performance"
        ok("CopyEngine 状态持久化（compound_base + performance）")

except Exception as e:
    import traceback
    fail("CopyEngine 端到端 Mock", f"{e}\n{traceback.format_exc()}")

# ─────────────────────────────────────────────────────────────────────────────
# 6. 回测模块完整性
# ─────────────────────────────────────────────────────────────────────────────
print("\n[6] 回测模块")
try:
    Config.COMPOUND_ENABLED = True
    Config.FIXED_FOLLOWER_ENABLED = False
    Config.CAPITAL_RATIO = 0.1
    Config.RISK_MULTIPLIER = 1.0
    Config.MAX_NOTIONAL_PER_SYMBOL = 500.0
    Config.BACKTEST_FEE_BPS = 4.0
    Config.BACKTEST_SLIPPAGE_BPS = 2.0

    result = run_backtest("data/backtest_sample.csv", 1000.0)
    assert result["total_trades"] > 0
    assert "equity_curve" in result
    assert "max_drawdown_pct" in result
    assert result["initial_capital"] == 1000.0
    assert isinstance(result["pl_ratio"], float)
    assert isinstance(result["sharpe"], float)
    ok(f"回测完整运行（{result['total_trades']}笔，ROI={result['roi_pct']:.2f}%，"
       f"最大回撤={result['max_drawdown_pct']:.2f}%）")
except Exception as e:
    fail("回测模块", e)

# ─────────────────────────────────────────────────────────────────────────────
# 7. Web API 接口完整性
# ─────────────────────────────────────────────────────────────────────────────
print("\n[7] Web API 接口")
try:
    from web.app import app, update_runtime
    app.config["TESTING"] = True
    client = app.test_client()

    # 注入测试数据
    update_runtime(
        balance=1234.56,
        positions=[{"symbol": "BTCUSDT", "positionAmt": "0.001"}],
        perf={"samples": 5, "win_rate": 0.6, "pl_ratio": 2.0, "sharpe": 1.8},
        signals={"BTCUSDT": {"side": "LONG", "weight": 0.9}},
        circuit_broken=False,
        router_status={"mode": "fallback", "primary_fail_count": 0},
    )

    endpoints = ["/api/status", "/api/positions", "/api/signals",
                 "/api/trades", "/api/equity", "/api/config", "/api/router",
                 "/api/leaders", "/"]
    for ep in endpoints:
        resp = client.get(ep)
        assert resp.status_code == 200, f"{ep} 返回 {resp.status_code}"
        ok(f"GET {ep} → 200")

    # 测试 POST /api/config（无 secret → 403）
    resp = client.post("/api/config", json={"CAPITAL_RATIO": 0.2},
                       headers={"X-Secret": "wrong"})
    assert resp.status_code == 403
    ok("POST /api/config 无效 secret → 403")

    # 测试 POST /api/config（正确 secret）
    Config.WEB_SECRET = "test_secret"
    resp = client.post("/api/config", json={"CAPITAL_RATIO": 0.2},
                       headers={"X-Secret": "test_secret"})
    assert resp.status_code == 200
    assert Config.CAPITAL_RATIO == 0.2
    ok("POST /api/config 正确 secret → 200 + 配置生效")

except Exception as e:
    import traceback
    fail("Web API", f"{e}\n{traceback.format_exc()}")

# ─────────────────────────────────────────────────────────────────────────────
# 汇总
# ─────────────────────────────────────────────────────────────────────────────
print()
print("=" * 60)
print(f"  测试结果：通过 {len(PASS)} / 失败 {len(FAIL)}")
print("=" * 60)
if FAIL:
    print("失败项目：")
    for f in FAIL:
        print(f"  ❌ {f}")
    sys.exit(1)
else:
    print("✅ 所有测试通过，系统完整性与可运行性验证成功")
