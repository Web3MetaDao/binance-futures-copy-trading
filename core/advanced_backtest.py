"""
advanced_backtest.py — 高级回测框架

基于向量化计算实现专业回测，支持：
  1. 参数网格搜索（寻找最优参数组合）
  2. 完整绩效指标（夏普率/卡玛比率/最大回撤/月度收益热力图）
  3. Kelly 仓位模拟
  4. 手续费 + 滑点模型
  5. 结果可视化（HTML 报告）

依赖：numpy, pandas, matplotlib（无需 vectorbt，纯 numpy 实现向量化）
"""
import csv
import itertools
import json
import logging
import os
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Tuple

import numpy as np

from core.config import Config

logger = logging.getLogger("advanced_backtest")


# ── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass
class BacktestParams:
    capital_ratio: float = 0.1
    risk_multiplier: float = 1.0
    stop_loss_pct: float = 0.02
    take_profit_pct: float = 0.04
    fee_bps: float = 4.0       # 手续费（基点）
    slippage_bps: float = 2.0  # 滑点（基点）
    kelly_enabled: bool = False
    kelly_cap: float = 0.3
    compound: bool = True


@dataclass
class BacktestResult:
    params: BacktestParams
    total_trades: int = 0
    win_trades: int = 0
    win_rate: float = 0.0
    total_pnl_pct: float = 0.0
    max_drawdown: float = 0.0
    sharpe: float = 0.0
    calmar: float = 0.0        # 卡玛比率 = 年化收益 / 最大回撤
    pl_ratio: float = 0.0
    final_capital: float = 0.0
    monthly_returns: dict = field(default_factory=dict)
    equity_curve: list = field(default_factory=list)


# ── 核心回测引擎 ──────────────────────────────────────────────────────────────

def _run_single(trades_data: list, params: BacktestParams,
                initial_capital: float = 1000.0) -> BacktestResult:
    """
    单次回测运行（向量化计算）
    trades_data: [{"ts": "2024-01-01", "side": "LONG", "pnl_pct": 0.015}, ...]
    """
    result = BacktestResult(params=params)
    if not trades_data:
        return result

    capital = initial_capital
    equity_curve = [capital]
    peak = capital
    max_dd = 0.0
    pnl_list = []
    monthly_pnl = {}

    fee_factor = params.fee_bps / 10000
    slip_factor = params.slippage_bps / 10000
    cost_factor = fee_factor * 2 + slip_factor * 2  # 双边手续费 + 双边滑点

    for trade in trades_data:
        raw_pnl = float(trade.get("pnl_pct", 0))
        # 扣除手续费和滑点
        net_pnl = raw_pnl - cost_factor

        # 仓位比例（Kelly 或固定）
        if params.kelly_enabled and len(pnl_list) >= 10:
            wins = sum(1 for p in pnl_list if p > 0)
            losses = [abs(p) for p in pnl_list if p < 0]
            gains = [p for p in pnl_list if p > 0]
            wr = wins / len(pnl_list)
            avg_win = sum(gains) / len(gains) if gains else 0.01
            avg_loss = sum(losses) / len(losses) if losses else 0.01
            pl_r = avg_win / avg_loss
            q = 1 - wr
            f = (wr * pl_r - q) / pl_r
            size = max(0.0, min(f * 0.5, params.kelly_cap))  # 半 Kelly
        else:
            size = params.capital_ratio * params.risk_multiplier

        # 实际 PnL（基于仓位比例）
        trade_pnl = capital * size * net_pnl

        if params.compound:
            capital += trade_pnl
        else:
            # [FIX-H03] 非复利模式：每笔 PnL 基于初始资金，资金不滚动
            # pnl_list 已含本笔之前的记录，加上本笔 trade_pnl
            capital = initial_capital + sum(p * initial_capital * size for p in pnl_list) + trade_pnl

        capital = max(capital, 0.01)  # 防止破产
        equity_curve.append(capital)
        pnl_list.append(net_pnl)

        # 最大回撤
        if capital > peak:
            peak = capital
        dd = (peak - capital) / peak
        if dd > max_dd:
            max_dd = dd

        # 月度收益
        ts_str = str(trade.get("ts", ""))
        month_key = ts_str[:7] if len(ts_str) >= 7 else "unknown"
        monthly_pnl[month_key] = monthly_pnl.get(month_key, 0.0) + trade_pnl / initial_capital

    # 统计指标
    total = len(pnl_list)
    wins = sum(1 for p in pnl_list if p > 0)
    gains = [p for p in pnl_list if p > 0]
    losses = [abs(p) for p in pnl_list if p < 0]

    win_rate = wins / total if total > 0 else 0.0
    avg_win = sum(gains) / len(gains) if gains else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.001
    pl_ratio = avg_win / avg_loss if avg_loss > 0 else 0.0

    # 夏普率（假设无风险利率 = 0）
    if len(pnl_list) > 1:
        mean_r = np.mean(pnl_list)
        std_r = np.std(pnl_list)
        # [FIX-H02] pnl_list 是按笔收益，不适合用 sqrt(252) 年化
        # 改为原始 Sharpe（按笔），调用方可根据实际交易频率自行年化
        sharpe = (mean_r / std_r) if std_r > 0 else 0.0
    else:
        sharpe = 0.0

    # 卡玛比率
    total_return = (capital - initial_capital) / initial_capital
    calmar = total_return / max_dd if max_dd > 0 else 0.0

    result.total_trades = total
    result.win_trades = wins
    result.win_rate = win_rate
    result.total_pnl_pct = total_return * 100
    result.max_drawdown = max_dd * 100
    result.sharpe = sharpe
    result.calmar = calmar
    result.pl_ratio = pl_ratio
    result.final_capital = capital
    result.monthly_returns = monthly_pnl
    result.equity_curve = equity_curve

    return result


# ── 参数网格搜索 ──────────────────────────────────────────────────────────────

def grid_search(trades_data: list, param_grid: dict,
                initial_capital: float = 1000.0,
                sort_by: str = "sharpe") -> List[BacktestResult]:
    """
    参数网格搜索
    param_grid 示例：
    {
        "capital_ratio": [0.05, 0.1, 0.15],
        "risk_multiplier": [0.8, 1.0, 1.2],
        "stop_loss_pct": [0.015, 0.02, 0.025],
    }
    返回按 sort_by 排序的结果列表（最优在前）
    """
    keys = list(param_grid.keys())
    values = list(param_grid.values())
    combinations = list(itertools.product(*values))

    logger.info(f"参数网格搜索: {len(combinations)} 个组合")

    results = []
    for combo in combinations:
        params_dict = dict(zip(keys, combo))
        params = BacktestParams(**{
            k: v for k, v in params_dict.items()
            if k in BacktestParams.__dataclass_fields__
        })
        result = _run_single(trades_data, params, initial_capital)
        results.append(result)

    # 排序
    sort_key_map = {
        "sharpe": lambda r: r.sharpe,
        "calmar": lambda r: r.calmar,
        "total_pnl_pct": lambda r: r.total_pnl_pct,
        "max_drawdown": lambda r: -r.max_drawdown,  # 越小越好
    }
    sort_fn = sort_key_map.get(sort_by, sort_key_map["sharpe"])
    results.sort(key=sort_fn, reverse=True)

    logger.info(
        f"最优参数: {asdict(results[0].params)} "
        f"夏普={results[0].sharpe:.2f} "
        f"卡玛={results[0].calmar:.2f} "
        f"ROI={results[0].total_pnl_pct:.1f}%"
    )
    return results


# ── 报告生成 ──────────────────────────────────────────────────────────────────

def generate_report(result: BacktestResult, output_path: str = None) -> str:
    """生成 HTML 回测报告"""
    if output_path is None:
        output_path = os.path.join(Config.LOG_DIR, "backtest_report.html")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # 月度收益热力图数据
    monthly_data = result.monthly_returns
    months = sorted(monthly_data.keys())
    monthly_values = [round(monthly_data[m] * 100, 2) for m in months]

    # 权益曲线数据
    equity = result.equity_curve

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>回测报告 — 币安跟单系统 Pro</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  body {{ font-family: 'Segoe UI', sans-serif; background: #0f0f1a; color: #e0e0e0; margin: 0; padding: 20px; }}
  h1 {{ color: #f0b90b; }}
  .grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin: 20px 0; }}
  .card {{ background: #1a1a2e; border-radius: 12px; padding: 20px; text-align: center; }}
  .card .val {{ font-size: 2em; font-weight: bold; color: #f0b90b; }}
  .card .label {{ color: #888; font-size: 0.9em; margin-top: 6px; }}
  .chart-box {{ background: #1a1a2e; border-radius: 12px; padding: 20px; margin: 16px 0; }}
  .positive {{ color: #26a69a; }}
  .negative {{ color: #ef5350; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ padding: 8px 12px; border-bottom: 1px solid #333; text-align: left; }}
  th {{ color: #f0b90b; }}
</style>
</head>
<body>
<h1>📊 回测报告 — 币安跟单系统 Pro</h1>

<div class="grid">
  <div class="card">
    <div class="val {'positive' if result.total_pnl_pct >= 0 else 'negative'}">{result.total_pnl_pct:+.1f}%</div>
    <div class="label">总收益率</div>
  </div>
  <div class="card">
    <div class="val">{result.sharpe:.2f}</div>
    <div class="label">夏普率</div>
  </div>
  <div class="card">
    <div class="val negative">-{result.max_drawdown:.1f}%</div>
    <div class="label">最大回撤</div>
  </div>
  <div class="card">
    <div class="val">{result.calmar:.2f}</div>
    <div class="label">卡玛比率</div>
  </div>
  <div class="card">
    <div class="val">{result.win_rate*100:.1f}%</div>
    <div class="label">胜率</div>
  </div>
  <div class="card">
    <div class="val">{result.pl_ratio:.2f}</div>
    <div class="label">盈亏比</div>
  </div>
  <div class="card">
    <div class="val">{result.total_trades}</div>
    <div class="label">总交易笔数</div>
  </div>
  <div class="card">
    <div class="val">{result.final_capital:.0f}</div>
    <div class="label">最终资金 (USDT)</div>
  </div>
</div>

<div class="chart-box">
  <h3>权益曲线</h3>
  <canvas id="equityChart" height="80"></canvas>
</div>

<div class="chart-box">
  <h3>月度收益率 (%)</h3>
  <canvas id="monthlyChart" height="60"></canvas>
</div>

<div class="chart-box">
  <h3>回测参数</h3>
  <table>
    <tr><th>参数</th><th>值</th></tr>
    <tr><td>资金比例</td><td>{result.params.capital_ratio}</td></tr>
    <tr><td>风险倍数</td><td>{result.params.risk_multiplier}</td></tr>
    <tr><td>止损比例</td><td>{result.params.stop_loss_pct*100:.1f}%</td></tr>
    <tr><td>止盈比例</td><td>{result.params.take_profit_pct*100:.1f}%</td></tr>
    <tr><td>手续费</td><td>{result.params.fee_bps} bps</td></tr>
    <tr><td>滑点</td><td>{result.params.slippage_bps} bps</td></tr>
    <tr><td>Kelly 仓位</td><td>{'启用' if result.params.kelly_enabled else '关闭'}</td></tr>
    <tr><td>复利模式</td><td>{'启用' if result.params.compound else '关闭'}</td></tr>
  </table>
</div>

<script>
const equityData = {json.dumps(equity)};
const months = {json.dumps(months)};
const monthlyVals = {json.dumps(monthly_values)};

new Chart(document.getElementById('equityChart'), {{
  type: 'line',
  data: {{
    labels: equityData.map((_, i) => i),
    datasets: [{{ label: '权益 (USDT)', data: equityData,
      borderColor: '#f0b90b', backgroundColor: 'rgba(240,185,11,0.1)',
      fill: true, tension: 0.3, pointRadius: 0 }}]
  }},
  options: {{ plugins: {{ legend: {{ labels: {{ color: '#e0e0e0' }} }} }},
    scales: {{ x: {{ ticks: {{ color: '#888' }} }}, y: {{ ticks: {{ color: '#888' }} }} }} }}
}});

new Chart(document.getElementById('monthlyChart'), {{
  type: 'bar',
  data: {{
    labels: months,
    datasets: [{{ label: '月度收益率 (%)',
      data: monthlyVals,
      backgroundColor: monthlyVals.map(v => v >= 0 ? '#26a69a' : '#ef5350') }}]
  }},
  options: {{ plugins: {{ legend: {{ labels: {{ color: '#e0e0e0' }} }} }},
    scales: {{ x: {{ ticks: {{ color: '#888' }} }}, y: {{ ticks: {{ color: '#888' }} }} }} }}
}});
</script>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    logger.info(f"回测报告已生成: {output_path}")
    return output_path


# ── CLI 入口 ──────────────────────────────────────────────────────────────────

def run_from_csv(csv_file: str, initial_capital: float = 1000.0,
                 grid: bool = False, output_dir: str = None) -> BacktestResult:
    """从 CSV 文件运行回测"""
    trades = []
    with open(csv_file, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            trades.append(row)

    if grid:
        # 参数网格搜索
        param_grid = {
            "capital_ratio": [0.05, 0.08, 0.10, 0.12, 0.15],
            "risk_multiplier": [0.8, 1.0, 1.2],
            "stop_loss_pct": [0.015, 0.02, 0.025],
            "take_profit_pct": [0.03, 0.04, 0.05, 0.06],
        }
        results = grid_search(trades, param_grid, initial_capital)
        best = results[0]
        print(f"\n{'='*60}")
        print(f"最优参数组合（按夏普率排序）:")
        print(f"  capital_ratio:    {best.params.capital_ratio}")
        print(f"  risk_multiplier:  {best.params.risk_multiplier}")
        print(f"  stop_loss_pct:    {best.params.stop_loss_pct}")
        print(f"  take_profit_pct:  {best.params.take_profit_pct}")
        print(f"\n绩效指标:")
        print(f"  ROI:          {best.total_pnl_pct:+.1f}%")
        print(f"  夏普率:       {best.sharpe:.2f}")
        print(f"  卡玛比率:     {best.calmar:.2f}")
        print(f"  最大回撤:     -{best.max_drawdown:.1f}%")
        print(f"  胜率:         {best.win_rate*100:.1f}%")
        print(f"  盈亏比:       {best.pl_ratio:.2f}")
        print(f"  最终资金:     {best.final_capital:.2f} USDT")
        print(f"{'='*60}\n")

        out = output_dir or Config.LOG_DIR
        report_path = generate_report(best, os.path.join(out, "backtest_report.html"))
        print(f"HTML 报告: {report_path}")
        return best
    else:
        params = BacktestParams(
            capital_ratio=Config.CAPITAL_RATIO,
            risk_multiplier=Config.RISK_MULTIPLIER,
            fee_bps=Config.BACKTEST_FEE_BPS,
            slippage_bps=Config.BACKTEST_SLIPPAGE_BPS,
            compound=Config.COMPOUND_ENABLED,
        )
        result = _run_single(trades, params, initial_capital)
        print(f"\n回测结果: ROI={result.total_pnl_pct:+.1f}% "
              f"夏普={result.sharpe:.2f} 最大回撤=-{result.max_drawdown:.1f}%\n")
        out = output_dir or Config.LOG_DIR
        generate_report(result, os.path.join(out, "backtest_report.html"))
        return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="高级回测框架")
    parser.add_argument("--file", default="data/backtest_sample.csv")
    parser.add_argument("--capital", type=float, default=1000.0)
    parser.add_argument("--grid", action="store_true", help="启用参数网格搜索")
    parser.add_argument("--output", default=None, help="报告输出目录")
    args = parser.parse_args()
    run_from_csv(args.file, args.capital, args.grid, args.output)
