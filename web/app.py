"""
web/app.py — Flask Web 控制面板后端
提供 REST API 供前端展示实时仓位、收益曲线、交易日志、绩效指标及参数配置。
"""
import json
import os
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, abort
from flask_cors import CORS

from core.config import Config
from utils.logger import read_trade_logs
from utils.state import StateManager

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

# 全局运行状态（由 main.py 注入）
_runtime = {
    "balance": 0.0,
    "positions": [],
    "perf": {},
    "signals": {},
    "circuit_broken": False,
    "last_update": 0,
    "running": False,
    "router_status": {},  # 双链路路由器状态
}


def update_runtime(balance, positions, perf, signals, circuit_broken, router_status=None):
    _runtime["balance"] = balance
    _runtime["positions"] = positions
    _runtime["perf"] = perf
    _runtime["signals"] = signals
    _runtime["circuit_broken"] = circuit_broken
    _runtime["last_update"] = time.time()
    _runtime["running"] = True
    if router_status is not None:
        _runtime["router_status"] = router_status


# ── API 路由 ──────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    return jsonify({
        "running": _runtime["running"],
        "balance": _runtime["balance"],
        "circuit_broken": _runtime["circuit_broken"],
        "last_update": _runtime["last_update"],
        "perf": _runtime["perf"],
    })


@app.route("/api/positions")
def api_positions():
    return jsonify(_runtime["positions"])


@app.route("/api/signals")
def api_signals():
    return jsonify(_runtime["signals"])


@app.route("/api/trades")
def api_trades():
    limit = int(request.args.get("limit", 100))
    return jsonify(read_trade_logs(limit))


@app.route("/api/equity")
def api_equity():
    """从交易日志重建权益曲线"""
    logs = read_trade_logs(500)
    curve = []
    balance = _runtime["balance"]
    for log in reversed(logs):
        if log.get("action") == "CLOSE" and "pnl_pct" in log:
            curve.append({
                "ts": log["ts"],
                "symbol": log["symbol"],
                "pnl_pct": log["pnl_pct"],
            })
    return jsonify(curve)


@app.route("/api/config", methods=["GET"])
def api_config_get():
    """返回当前可调配置"""
    return jsonify({
        "POLL_INTERVAL": Config.POLL_INTERVAL,
        "CAPITAL_RATIO": Config.CAPITAL_RATIO,
        "RISK_MULTIPLIER": Config.RISK_MULTIPLIER,
        "MAX_NOTIONAL_PER_SYMBOL": Config.MAX_NOTIONAL_PER_SYMBOL,
        "MAX_TOTAL_NOTIONAL": Config.MAX_TOTAL_NOTIONAL,
        "STOP_LOSS_PCT": Config.STOP_LOSS_PCT,
        "TAKE_PROFIT_PCT": Config.TAKE_PROFIT_PCT,
        "MAX_DRAWDOWN_PCT": Config.MAX_DRAWDOWN_PCT,
        "SMART_MONEY_ENABLED": Config.SMART_MONEY_ENABLED,
        "SMART_MONEY_MIN_SCORE": Config.SMART_MONEY_MIN_SCORE,
        "FIXED_FOLLOWER_ENABLED": Config.FIXED_FOLLOWER_ENABLED,
        "FIXED_FOLLOWER_NOTIONAL_USDT": Config.FIXED_FOLLOWER_NOTIONAL_USDT,
        "COMPOUND_ENABLED": Config.COMPOUND_ENABLED,
        "COMPOUND_REINVEST_RATIO": Config.COMPOUND_REINVEST_RATIO,
        "TELEGRAM_ENABLED": Config.TELEGRAM_ENABLED,
    })


@app.route("/api/config", methods=["POST"])
def api_config_post():
    """动态更新部分配置（运行时生效，不写入 .env）"""
    secret = request.headers.get("X-Secret", "")
    if secret != Config.WEB_SECRET:
        abort(403)
    data = request.json or {}
    float_keys = ["CAPITAL_RATIO", "RISK_MULTIPLIER", "MAX_NOTIONAL_PER_SYMBOL",
                  "MAX_TOTAL_NOTIONAL", "STOP_LOSS_PCT", "TAKE_PROFIT_PCT",
                  "MAX_DRAWDOWN_PCT", "SMART_MONEY_MIN_SCORE",
                  "FIXED_FOLLOWER_NOTIONAL_USDT", "COMPOUND_REINVEST_RATIO"]
    bool_keys = ["SMART_MONEY_ENABLED", "FIXED_FOLLOWER_ENABLED",
                 "COMPOUND_ENABLED", "TELEGRAM_ENABLED"]
    int_keys = ["POLL_INTERVAL"]
    for k, v in data.items():
        if k in float_keys:
            setattr(Config, k, float(v))
        elif k in bool_keys:
            setattr(Config, k, bool(v))
        elif k in int_keys:
            setattr(Config, k, int(v))
    return jsonify({"ok": True})


@app.route("/api/router")
def api_router():
    """返回双链路路由器状态"""
    return jsonify(_runtime.get("router_status", {}))


@app.route("/api/leaders")
def api_leaders():
    return jsonify(Config.get_leaders())


# ── 前端静态文件 ──────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


def run_web(host: str = None, port: int = None):
    app.run(host=host or Config.WEB_HOST,
            port=port or Config.WEB_PORT,
            debug=False, use_reloader=False)
