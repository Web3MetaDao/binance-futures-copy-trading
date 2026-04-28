"""
engine.py — 风控执行引擎
负责：信号差分 → Smart Money 评分 → 绩效检查 → 预算计算 → 下单 → 止损止盈挂单

复利模式（真复利）：
  每笔平仓后立即从交易所拉取最新余额，作为下一笔开仓的基础资金。
  仓位 = 当前实时余额 × CAPITAL_RATIO × RISK_MULTIPLIER
  与回测模块逻辑完全一致，盈利自动扩大仓位，亏损自动缩小仓位。

审计修复记录（v2）：
  [FIX-01] _compound_base 为 None 时 Notifier 格式化字符串崩溃 → 加 None 守卫
  [FIX-02] 平仓 PnL 计算未扣除手续费 → 扣除双边 taker fee (0.04% × 2)
  [FIX-03] 平仓后 total_notional 未更新 → 平仓后立即减去对应名义仓位
  [FIX-04] 平仓后 my_pos 快照未清除已平仓 symbol → 添加 closed_symbols 集合
  [FIX-05] _place_sl_tp 中 cancel_all_orders 异常未捕获正确类型 → 改为 BinanceError
  [FIX-06] 开仓记录 trade_log 未写入 source_chain → 补充 source_chain 字段
  [FIX-07] _perf_factor 逻辑：pl_ratio 超上限也返回 1.0，应视为异常降权 → 修正
"""
import time
import logging
from decimal import Decimal
from typing import Optional

from core.config import Config
from core.binance_client import BinanceFuturesClient, BinanceError
from core.smart_money import SmartMoneyScorer
from core.performance import PerformanceTracker
from utils.state import StateManager
from utils.logger import trade_log
from utils.notifier import Notifier

logger = logging.getLogger("engine")

# 双边 taker 手续费（开仓 + 平仓），用于实盘 PnL 修正
_TAKER_FEE_RATE = 0.0004  # 0.04% per side
_ROUND_TRIP_FEE = _TAKER_FEE_RATE * 2


class CopyEngine:
    def __init__(self, client: BinanceFuturesClient, notifier: Notifier = None):
        self.client = client
        self.scorer = SmartMoneyScorer(client)
        self.perf = PerformanceTracker(window=Config.PERFORMANCE_WINDOW)
        self.state = StateManager(Config.STATE_FILE)
        self.notifier = notifier or Notifier()

        # 空仓快照保护计数
        self._empty_count: int = 0
        # 熔断状态
        self._circuit_broken: bool = False
        self._circuit_until: float = 0.0
        # 初始余额（用于熔断回撤计算）
        self._initial_balance: Optional[float] = None
        # 真复利基础余额（每笔平仓后实时更新）
        self._compound_base: Optional[float] = None
        # 上一轮持仓快照 {symbol: side}
        self._prev_positions: dict = {}

        self._load_state()

    # ── 状态持久化 ─────────────────────────────────────────────

    def _load_state(self):
        s = self.state.load()
        self._prev_positions = s.get("prev_positions", {})
        self._initial_balance = s.get("initial_balance")
        self._compound_base = s.get("compound_base")
        self.perf.load(s.get("performance", {}))
        logger.info("State loaded.")

    def _save_state(self):
        self.state.save({
            "prev_positions": self._prev_positions,
            "initial_balance": self._initial_balance,
            "compound_base": self._compound_base,
            "performance": self.perf.dump(),
        })

    # ── 熔断 ──────────────────────────────────────────────────

    def _check_circuit_breaker(self, balance: float) -> bool:
        if self._circuit_broken:
            if time.time() < self._circuit_until:
                remaining = int(self._circuit_until - time.time())
                logger.warning(f"[CIRCUIT BREAKER] 熔断中，剩余 {remaining}s")
                return True
            else:
                self._circuit_broken = False
                logger.info("[CIRCUIT BREAKER] 熔断解除，恢复交易")
                self.notifier.send("✅ 熔断解除，恢复跟单交易")

        if self._initial_balance and balance < self._initial_balance * (1 - Config.MAX_DRAWDOWN_PCT):
            self._circuit_broken = True
            self._circuit_until = time.time() + Config.CIRCUIT_BREAKER_MINUTES * 60
            msg = (f"🚨 最大回撤熔断！当前余额 {balance:.2f} USDT，"
                   f"初始 {self._initial_balance:.2f} USDT，"
                   f"暂停 {Config.CIRCUIT_BREAKER_MINUTES} 分钟")
            logger.error(msg)
            self.notifier.send(msg)
            return True
        return False

    # ── 真复利预算计算 ─────────────────────────────────────────
    #
    # 真复利逻辑：
    #   开仓预算 = _compound_base × CAPITAL_RATIO × RISK_MULTIPLIER
    #   _compound_base 在每笔平仓后立即从交易所拉取最新余额更新
    #   → 盈利后下一笔仓位自动变大，亏损后自动变小
    #   → 与回测模块 capital × CAPITAL_RATIO × RISK_MULTIPLIER 完全一致
    #
    # 固定跟单员模式（FIXED_FOLLOWER_ENABLED=true）：
    #   忽略复利，每笔固定 FIXED_FOLLOWER_NOTIONAL_USDT

    def _calc_budget(self, score: float) -> float:
        if Config.FIXED_FOLLOWER_ENABLED:
            base = Config.FIXED_FOLLOWER_NOTIONAL_USDT
        else:
            # 真复利：使用最新更新的复利基础余额
            base_balance = self._compound_base or self._initial_balance or 0.0
            base = base_balance * Config.CAPITAL_RATIO * Config.RISK_MULTIPLIER

        # 按 Smart Money 分数缩放（分数越高，仓位越大，最低 0.3 倍）
        scaled = base * max(0.3, score)
        return min(scaled, Config.MAX_NOTIONAL_PER_SYMBOL)

    def _refresh_compound_base(self):
        """平仓后立即拉取最新余额，更新复利基础资金"""
        try:
            new_balance = self.client.get_balance()
            old_base = self._compound_base or self._initial_balance or new_balance
            self._compound_base = new_balance
            logger.info(
                f"[复利更新] 基础资金: {old_base:.2f} → {new_balance:.2f} USDT "
                f"({'↑' if new_balance >= old_base else '↓'}"
                f"{abs(new_balance - old_base):.2f})"
            )
        except BinanceError as e:
            logger.warning(f"[复利更新] 获取余额失败，保持原值: {e}")

    # ── 止损止盈挂单 ───────────────────────────────────────────

    def _place_sl_tp(self, symbol: str, side: str, qty: Decimal, entry_price: float):
        """在开仓后挂止损和止盈单"""
        # [FIX-05] 改为捕获 BinanceError，记录警告但不中断流程
        try:
            self.client.cancel_all_orders(symbol)
        except BinanceError as e:
            logger.warning(f"[{symbol}] 撤销旧止损止盈单失败，继续挂新单: {e}")

        close_side = "SELL" if side == "LONG" else "BUY"
        try:
            if side == "LONG":
                sl_price = self.client.normalize_price(symbol, entry_price * (1 - Config.STOP_LOSS_PCT))
                tp_price = self.client.normalize_price(symbol, entry_price * (1 + Config.TAKE_PROFIT_PCT))
            else:
                sl_price = self.client.normalize_price(symbol, entry_price * (1 + Config.STOP_LOSS_PCT))
                tp_price = self.client.normalize_price(symbol, entry_price * (1 - Config.TAKE_PROFIT_PCT))

            self.client.place_stop_order(symbol, close_side, qty, sl_price)
            logger.info(f"[{symbol}] 止损单已挂 @ {sl_price}")

            self.client.place_take_profit_order(symbol, close_side, qty, tp_price)
            logger.info(f"[{symbol}] 止盈单已挂 @ {tp_price}")
        except BinanceError as e:
            logger.warning(f"[{symbol}] 止损/止盈挂单失败: {e}")

    # ── 主执行循环 ─────────────────────────────────────────────

    def run_once(self, new_signals: dict):
        """
        new_signals: DualSourceRouter.fetch_signals() 的输出
        {symbol: {"side", "weight", "sources", "avg_leverage", "avg_notional",
                  "source_chain": "primary"/"smart_money"}}
        """
        try:
            balance = self.client.get_balance()
        except BinanceError as e:
            logger.error(f"获取余额失败: {e}")
            return

        # 首次运行：初始化初始余额和复利基础资金
        if self._initial_balance is None:
            self._initial_balance = balance
            self._compound_base = balance
            logger.info(f"初始余额记录: {balance:.2f} USDT（复利基础资金同步初始化）")

        # 熔断检查
        if self._check_circuit_breaker(balance):
            return

        # 绩效检查（软降杠杆）
        perf_factor = self._perf_factor()

        # 获取当前持仓
        try:
            my_positions_raw = self.client.get_positions()
        except BinanceError as e:
            logger.error(f"获取持仓失败: {e}")
            return

        my_pos = {p["symbol"]: p for p in my_positions_raw}

        # 空仓快照保护
        if not new_signals and self._prev_positions:
            self._empty_count += 1
            if self._empty_count < Config.EMPTY_SNAPSHOT_TOLERANCE:
                logger.warning(f"[空仓保护] 第 {self._empty_count} 次空快照，跳过本轮")
                return
            logger.info("[空仓保护] 连续空快照超阈值，认定真实平仓")
        else:
            self._empty_count = 0

        # 计算总名义仓位（[FIX-03] 后续平仓/开仓时实时维护）
        total_notional = sum(
            abs(float(p["positionAmt"])) * float(p["markPrice"])
            for p in my_positions_raw
        )

        # ── 平仓：我有但信号没有 ──────────────────────────────
        closed_any = False
        closed_symbols: set = set()  # [FIX-04] 记录已平仓 symbol

        for symbol, my_p in list(my_pos.items()):
            if symbol not in new_signals:
                amt = float(my_p["positionAmt"])
                if amt == 0:
                    continue
                close_side = "SELL" if amt > 0 else "BUY"
                qty = self.client.normalize_quantity(symbol, abs(amt))
                if qty is None:
                    continue
                try:
                    self.client.place_market_order(symbol, close_side, qty, reduce_only=True)
                    entry = float(my_p["entryPrice"])
                    mark = float(my_p["markPrice"])
                    raw_pnl_pct = (mark - entry) / entry * (1 if amt > 0 else -1)
                    # [FIX-02] 扣除双边 taker 手续费
                    pnl_pct = raw_pnl_pct - _ROUND_TRIP_FEE
                    self.perf.record(pnl_pct)
                    # [FIX-03] 平仓后实时更新 total_notional
                    total_notional = max(0.0, total_notional - abs(amt) * mark)
                    trade_log("CLOSE", symbol, close_side, float(qty), mark, pnl_pct=pnl_pct)
                    self.notifier.send(
                        f"📤 平仓 {symbol} {close_side} {qty} @ {mark:.4f} | PnL: {pnl_pct:.2%}"
                    )
                    logger.info(f"[CLOSE] {symbol} {close_side} qty={qty} pnl={pnl_pct:.2%}")
                    closed_any = True
                    closed_symbols.add(symbol)  # [FIX-04]
                except BinanceError as e:
                    logger.error(f"[CLOSE] {symbol} 失败: {e}")

        # ── 真复利：有平仓发生时立即更新基础资金 ──────────────
        if closed_any and Config.COMPOUND_ENABLED and not Config.FIXED_FOLLOWER_ENABLED:
            self._refresh_compound_base()

        # [FIX-04] 从当前持仓快照移除已平仓的 symbol，防止开仓阶段误判
        for sym in closed_symbols:
            my_pos.pop(sym, None)

        # ── 开仓：信号有但我没有 ──────────────────────────────
        for symbol, sig in new_signals.items():
            # 白名单/黑名单过滤
            if Config.SYMBOL_WHITELIST and symbol not in Config.SYMBOL_WHITELIST:
                continue
            if symbol in Config.SYMBOL_BLACKLIST:
                continue

            side = sig["side"]
            weight = sig["weight"]

            # 已有同向仓位 → 跳过
            if symbol in my_pos:
                existing_amt = float(my_pos[symbol]["positionAmt"])
                existing_side = "LONG" if existing_amt > 0 else "SHORT"
                if existing_side == side:
                    continue
                # 反向 → 先平仓
                if Config.NO_REVERSE:
                    continue
                close_side = "SELL" if existing_amt > 0 else "BUY"
                qty = self.client.normalize_quantity(symbol, abs(existing_amt))
                if qty:
                    try:
                        self.client.place_market_order(symbol, close_side, qty, reduce_only=True)
                        # 反手平仓后也更新复利基础资金
                        if Config.COMPOUND_ENABLED and not Config.FIXED_FOLLOWER_ENABLED:
                            self._refresh_compound_base()
                    except BinanceError as e:
                        logger.error(f"[REVERSE CLOSE] {symbol}: {e}")
                        continue

            # Smart Money 评分
            sm = self.scorer.score(symbol, side, signal_weight=weight)
            if sm["skip"]:
                logger.info(f"[SKIP] {symbol} {side}: {sm['reason']}")
                continue

            # 绩效因子
            if perf_factor == 0:
                logger.info(f"[SKIP] {symbol}: 绩效不达标，暂停开仓")
                continue

            # 总名义仓位限制
            if total_notional >= Config.MAX_TOTAL_NOTIONAL:
                logger.warning(
                    f"[SKIP] {symbol}: 总名义仓位 {total_notional:.0f} >= 上限 {Config.MAX_TOTAL_NOTIONAL}"
                )
                continue

            # 真复利预算计算（使用最新 _compound_base）
            budget = self._calc_budget(sm["score"]) * perf_factor

            # 获取标记价格
            try:
                mark = self.client.get_mark_price(symbol)
            except BinanceError as e:
                logger.error(f"[{symbol}] 获取标记价格失败: {e}")
                continue

            raw_qty = budget / mark
            qty = self.client.normalize_quantity(symbol, raw_qty)
            if qty is None:
                logger.warning(f"[{symbol}] 数量不足最小下单量，跳过")
                continue

            order_side = "BUY" if side == "LONG" else "SELL"
            try:
                self.client.place_market_order(symbol, order_side, qty)
                total_notional += float(qty) * mark
                # [FIX-01] 安全格式化 _compound_base（可能为 None）
                compound_display = f"{self._compound_base:.2f}" if self._compound_base is not None else "N/A"
                # [FIX-06] 记录 source_chain 字段
                trade_log("OPEN", symbol, order_side, float(qty), mark,
                          score=sm["score"], weight=weight, sources=sig["sources"],
                          compound_base=self._compound_base,
                          source_chain=sig.get("source_chain", "primary"))
                self.notifier.send(
                    f"📥 开仓 {symbol} {order_side} {qty} @ {mark:.4f} | "
                    f"评分:{sm['score']:.2f} 复利基础:{compound_display}USDT 来源:{sig['sources']}"
                )
                logger.info(
                    f"[OPEN] {symbol} {order_side} qty={qty} score={sm['score']:.2f} "
                    f"compound_base={compound_display}"
                )

                # 挂止损止盈
                self._place_sl_tp(symbol, side, qty, mark)

            except BinanceError as e:
                logger.error(f"[OPEN] {symbol} 失败: {e}")

        # 更新快照并保存状态
        self._prev_positions = {sym: sig["side"] for sym, sig in new_signals.items()}
        self._save_state()

    def _perf_factor(self) -> float:
        """
        绩效软降杠杆：
        - 绩效优秀（在目标区间内）→ 1.0（满仓）
        - 绩效一般（达到最低目标 80%）→ 0.5（半仓）
        - 绩效差（低于最低目标 80%）→ 0.0（暂停开仓）

        [FIX-07] 修正：pl_ratio 超过上限也视为异常（可能是过拟合或极端行情），
                 降为半仓而非满仓，避免在极端行情中过度加仓。
        """
        if self.perf.sample_count < 5:
            return 1.0  # 样本不足，不限制

        pl = self.perf.pl_ratio()
        sharpe = self.perf.sharpe()

        pl_in_range = Config.TARGET_PL_RATIO_MIN <= pl <= Config.TARGET_PL_RATIO_MAX
        sharpe_in_range = Config.TARGET_SHARPE_MIN <= sharpe <= Config.TARGET_SHARPE_MAX

        # [FIX-07] 超出上限也降权（异常高收益可能是幸存者偏差）
        pl_above_max = pl > Config.TARGET_PL_RATIO_MAX
        sharpe_above_max = sharpe > Config.TARGET_SHARPE_MAX

        if pl_in_range and sharpe_in_range:
            return 1.0
        if pl_above_max or sharpe_above_max:
            return 0.5  # 超出上限，保守处理
        if pl >= Config.TARGET_PL_RATIO_MIN * 0.8 and sharpe >= Config.TARGET_SHARPE_MIN * 0.8:
            return 0.5
        return 0.0
