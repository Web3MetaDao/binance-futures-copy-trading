"""
main.py — 主入口
启动跟单引擎（后台线程）+ Web 控制面板（主线程）

信号链路架构：
  主链路：MultiLeaderAggregator（公开实盘持仓抓取）
    ↓ 失败/无信号 (连续 SM_FALLBACK_THRESHOLD 次)
  备用链路：SmartMoneySource（Binance 排行榜聪明钱）
    ↓ 两者均失败
  跳过本轮

模式配置（SM_SIGNAL_MERGE_MODE）：
  fallback — 主链路优先，失败时切换到备用（默认）
  merge    — 主备信号融合，备用信号降权后补充主链路
"""
import logging
import threading
import time
import datetime

import utils.logger  # 初始化日志系统

from core.config import Config
from core.binance_client import BinanceFuturesClient
from core.lead_source import MultiLeaderAggregator
from core.smart_money_source import SmartMoneySource, DualSourceRouter
from core.engine import CopyEngine
from web.app import run_web, update_runtime
from utils.notifier import Notifier

logger = logging.getLogger("main")


def trading_loop():
    client = BinanceFuturesClient()
    notifier = Notifier()
    engine = CopyEngine(client, notifier)

    # ── 双链路初始化 ──────────────────────────────────────────────
    primary_source = MultiLeaderAggregator()
    fallback_source = SmartMoneySource()
    router = DualSourceRouter(primary_source, fallback_source)

    logger.info("跟单引擎启动")
    logger.info(
        f"信号链路模式: {Config.SM_SIGNAL_MERGE_MODE} | "
        f"备用链路: {'已启用' if Config.SM_SOURCE_ENABLED else '已禁用'} | "
        f"切换阈值: {Config.SM_FALLBACK_THRESHOLD} 次"
    )
    notifier.send(
        f"🚀 币安跟单系统 Pro 已启动\n"
        f"信号模式: {Config.SM_SIGNAL_MERGE_MODE.upper()}\n"
        f"主链路: {len(primary_source.trackers)} 名交易员\n"
        f"备用链路: Smart Money TOP {Config.SM_SOURCE_TOP_N}"
    )

    daily_report_hour = -1

    while True:
        try:
            # ── 通过路由器获取信号（自动处理主备切换/融合）──────────
            signals = router.fetch_signals()

            # 记录当前使用的链路
            if signals:
                chains = set(s.get("source_chain", "primary") for s in signals.values())
                chain_str = "+".join(sorted(chains))
                logger.debug(f"本轮信号来源: {chain_str} ({len(signals)} 个交易对)")

            # ── 执行跟单逻辑 ──────────────────────────────────────
            engine.run_once(signals)

            # ── 更新 Web 面板数据 ─────────────────────────────────
            try:
                balance = client.get_balance()
                positions_raw = client.get_positions()
                positions = [
                    {
                        "symbol": p["symbol"],
                        "positionAmt": p["positionAmt"],
                        "entryPrice": p["entryPrice"],
                        "markPrice": p["markPrice"],
                        "unRealizedProfit": p["unRealizedProfit"],
                        "leverage": p.get("leverage", 1),
                    }
                    for p in positions_raw
                ]

                # 附加链路状态到 Web 面板
                router_status = {
                    "mode": Config.SM_SIGNAL_MERGE_MODE,
                    "primary_fail_count": router.primary_fail_count,
                    "fallback_threshold": Config.SM_FALLBACK_THRESHOLD,
                    "sm_source_enabled": Config.SM_SOURCE_ENABLED,
                    "sm_top_n": Config.SM_SOURCE_TOP_N,
                    "sm_sort_type": Config.SM_SOURCE_SORT_TYPE,
                }

                update_runtime(
                    balance=balance,
                    positions=positions,
                    perf=engine.perf.summary(),
                    signals=signals,
                    circuit_broken=engine._circuit_broken,
                    router_status=router_status,
                )

                # ── 每日汇总报告（UTC 0 点）──────────────────────
                now_hour = datetime.datetime.utcnow().hour
                if now_hour == 0 and daily_report_hour != 0:
                    daily_report_hour = 0
                    notifier.send_summary(balance, engine.perf.summary(), positions)
                elif now_hour != 0:
                    daily_report_hour = now_hour

            except Exception as e:
                logger.warning(f"Web 数据更新失败: {e}")

        except Exception as e:
            logger.error(f"主循环异常: {e}", exc_info=True)

        time.sleep(Config.POLL_INTERVAL)


def main():
    # 启动交易线程
    t = threading.Thread(target=trading_loop, daemon=True, name="TradingLoop")
    t.start()
    logger.info(f"Web 面板启动: http://{Config.WEB_HOST}:{Config.WEB_PORT}")

    # 主线程运行 Web 服务
    run_web()


if __name__ == "__main__":
    main()
