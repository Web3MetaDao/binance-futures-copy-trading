"""
notifier.py — Telegram 通知模块
支持发送文本消息到 Telegram Bot，失败时降级为日志输出。
"""
import logging
import requests

from core.config import Config

logger = logging.getLogger("notifier")


class Notifier:
    def __init__(self):
        self.enabled = Config.TELEGRAM_ENABLED
        self.token = Config.TELEGRAM_BOT_TOKEN
        self.chat_id = Config.TELEGRAM_CHAT_ID

    def send(self, message: str):
        if not self.enabled:
            logger.info(f"[Notify] {message}")
            return
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            resp = requests.post(url, json={
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML",
            }, timeout=10)
            if resp.status_code != 200:
                logger.warning(f"Telegram 发送失败: {resp.text}")
        except Exception as e:
            logger.warning(f"Telegram 异常: {e}")

    def send_summary(self, balance: float, perf_summary: dict, positions: list):
        """发送每日汇总报告"""
        pos_str = "\n".join(
            f"  {p['symbol']} {p['side']} x{p['leverage']} @ {p['entry_price']}"
            for p in positions[:10]
        ) or "  无持仓"
        msg = (
            f"📊 <b>跟单系统日报</b>\n"
            f"余额: <b>{balance:.2f} USDT</b>\n"
            f"胜率: {perf_summary.get('win_rate', 0)*100:.1f}%  "
            f"盈亏比: {perf_summary.get('pl_ratio', 0):.2f}  "
            f"夏普: {perf_summary.get('sharpe', 0):.2f}\n"
            f"总收益: {perf_summary.get('total_return', 0)*100:.2f}%\n"
            f"当前持仓:\n{pos_str}"
        )
        self.send(msg)
