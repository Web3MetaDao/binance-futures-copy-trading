"""
backtest.py — 真实回测模块
支持 CSV 历史交易数据回测，含手续费、滑点、复利/固定跟单模式。
CSV 格式: symbol,side,entry_price,exit_price,notional_usdt,timestamp
"""
import argparse
import csv
import json
import math
from decimal import Decimal
from pathlib import Path

from core.config import Config
from core.performance import PerformanceTracker


def run_backtest(csv_file: str, initial_capital: float = 1000.0) -> dict:
    path = Path(csv_file)
    if not path.exists():
        raise FileNotFoundError(f"回测文件不存在: {csv_file}")

    perf = PerformanceTracker(window=9999)
    capital = initial_capital
    equity_curve = [initial_capital]
    trades = []

    fee_rate = Config.BACKTEST_FEE_BPS / 10000
    slip_rate = Config.BACKTEST_SLIPPAGE_BPS / 10000
    cost_rate = fee_rate + slip_rate

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            symbol = row.get("symbol", "UNKNOWN")
            side = row.get("side", "LONG").upper()
            entry = float(row.get("entry_price", 0))
            exit_ = float(row.get("exit_price", 0))
            ts = row.get("timestamp", "")

            if entry <= 0 or exit_ <= 0:
                continue

            # 计算名义仓位
            if Config.FIXED_FOLLOWER_ENABLED:
                notional = Config.FIXED_FOLLOWER_NOTIONAL_USDT
            else:
                if Config.COMPOUND_ENABLED:
                    notional = capital * Config.CAPITAL_RATIO * Config.RISK_MULTIPLIER
                else:
                    notional = initial_capital * Config.CAPITAL_RATIO * Config.RISK_MULTIPLIER
                notional = min(notional, Config.MAX_NOTIONAL_PER_SYMBOL)

            # 计算 PnL
            if side == "LONG":
                raw_pnl_pct = (exit_ - entry) / entry
            else:
                raw_pnl_pct = (entry - exit_) / entry

            gross_pnl = notional * raw_pnl_pct
            cost = notional * cost_rate * 2  # 开仓+平仓
            net_pnl = gross_pnl - cost
            net_pnl_pct = net_pnl / capital if capital > 0 else 0

            capital += net_pnl
            perf.record(net_pnl_pct)
            equity_curve.append(round(capital, 4))

            trades.append({
                "symbol": symbol,
                "side": side,
                "entry": entry,
                "exit": exit_,
                "notional": round(notional, 2),
                "gross_pnl": round(gross_pnl, 4),
                "cost": round(cost, 4),
                "net_pnl": round(net_pnl, 4),
                "net_pnl_pct": round(net_pnl_pct * 100, 4),
                "capital_after": round(capital, 4),
                "timestamp": ts,
            })

    # 最大回撤
    peak = initial_capital
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak
        if dd > max_dd:
            max_dd = dd

    result = {
        "initial_capital": initial_capital,
        "final_capital": round(capital, 4),
        "roi_pct": round((capital - initial_capital) / initial_capital * 100, 4),
        "total_trades": len(trades),
        "win_rate_pct": round(perf.win_rate() * 100, 2),
        "pl_ratio": round(perf.pl_ratio(), 4),
        "sharpe": round(perf.sharpe(), 4),
        "max_drawdown_pct": round(max_dd * 100, 4),
        "fee_bps": Config.BACKTEST_FEE_BPS,
        "slippage_bps": Config.BACKTEST_SLIPPAGE_BPS,
        "equity_curve": equity_curve,
        "trades": trades,
    }
    return result


def main():
    parser = argparse.ArgumentParser(description="币安跟单系统回测")
    parser.add_argument("--file", required=True, help="CSV 回测数据文件路径")
    parser.add_argument("--capital", type=float, default=1000.0, help="初始资金 USDT")
    parser.add_argument("--output", default=None, help="输出 JSON 文件路径（可选）")
    args = parser.parse_args()

    result = run_backtest(args.file, args.capital)

    print("\n" + "=" * 50)
    print("  币安跟单系统 — 回测报告")
    print("=" * 50)
    print(f"  初始资金:     {result['initial_capital']:.2f} USDT")
    print(f"  最终资金:     {result['final_capital']:.2f} USDT")
    print(f"  ROI:          {result['roi_pct']:.2f}%")
    print(f"  总交易笔数:   {result['total_trades']}")
    print(f"  胜率:         {result['win_rate_pct']:.2f}%")
    print(f"  盈亏比:       {result['pl_ratio']:.4f}")
    print(f"  夏普率:       {result['sharpe']:.4f}")
    print(f"  最大回撤:     {result['max_drawdown_pct']:.2f}%")
    print(f"  手续费:       {result['fee_bps']} bps")
    print(f"  滑点:         {result['slippage_bps']} bps")
    print("=" * 50 + "\n")

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"回测结果已保存至: {args.output}")


if __name__ == "__main__":
    main()
