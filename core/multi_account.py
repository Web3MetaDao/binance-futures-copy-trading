"""
multi_account.py — 多账户支持模块

支持配置多个 Binance 子账户，每个账户独立的：
  - API Key / Secret
  - 资金比例（CAPITAL_RATIO）
  - 风险倍数（RISK_MULTIPLIER）
  - 跟单交易员列表
  - 最大名义仓位

账户配置格式（JSON 文件或环境变量）：
[
  {
    "id": "main",
    "api_key": "xxx",
    "api_secret": "yyy",
    "capital_ratio": 0.1,
    "risk_multiplier": 1.0,
    "traders": ["lanaai"],
    "max_notional": 5000
  },
  {
    "id": "sub1",
    "api_key": "aaa",
    "api_secret": "bbb",
    "capital_ratio": 0.05,
    ...
  }
]
"""
import json
import logging
import os
import threading
from typing import Dict, List, Optional

from core.config import Config

logger = logging.getLogger("multi_account")

_ACCOUNTS_FILE = os.path.join(os.path.dirname(Config.LOG_DIR), "accounts.json")


class AccountConfig:
    """单个账户配置"""

    def __init__(self, data: dict):
        self.id: str = data["id"]
        self.api_key: str = data["api_key"]
        self.api_secret: str = data["api_secret"]
        self.capital_ratio: float = float(data.get("capital_ratio", Config.CAPITAL_RATIO))
        self.risk_multiplier: float = float(data.get("risk_multiplier", Config.RISK_MULTIPLIER))
        self.traders: List[str] = data.get("traders", [])
        self.max_notional: float = float(data.get("max_notional", Config.MAX_TOTAL_NOTIONAL))
        self.enabled: bool = bool(data.get("enabled", True))
        self.testnet: bool = bool(data.get("testnet", Config.TESTNET))
        self.leverage: int = int(data.get("leverage", Config.LEVERAGE))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "api_key": (self.api_key[:8] if len(self.api_key) >= 8 else self.api_key[:4]) + "****",  # [FIX-H01] 安全脱敏
            "capital_ratio": self.capital_ratio,
            "risk_multiplier": self.risk_multiplier,
            "traders": self.traders,
            "max_notional": self.max_notional,
            "enabled": self.enabled,
            "testnet": self.testnet,
            "leverage": self.leverage,
        }


class MultiAccountManager:
    """
    多账户管理器
    负责加载、管理多个账户配置，并为每个账户创建独立的引擎实例。
    """

    def __init__(self, accounts_file: str = None):
        self._file = accounts_file or _ACCOUNTS_FILE
        self._accounts: Dict[str, AccountConfig] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        """从文件加载账户配置"""
        # 优先从环境变量读取（JSON 格式）
        env_accounts = os.environ.get("MULTI_ACCOUNTS")
        if env_accounts:
            try:
                data = json.loads(env_accounts)
                self._parse(data)
                logger.info(f"从环境变量加载 {len(self._accounts)} 个账户")
                return
            except Exception as e:
                logger.warning(f"环境变量 MULTI_ACCOUNTS 解析失败: {e}")

        # 从文件读取
        if os.path.exists(self._file):
            try:
                with open(self._file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._parse(data)
                logger.info(f"从文件加载 {len(self._accounts)} 个账户: {self._file}")
                return
            except Exception as e:
                logger.warning(f"账户文件加载失败: {e}")

        # 降级：使用单账户模式（从主配置读取）
        logger.info("未找到多账户配置，使用单账户模式")
        self._accounts["default"] = AccountConfig({
            "id": "default",
            "api_key": Config.API_KEY,
            "api_secret": Config.API_SECRET,
            "capital_ratio": Config.CAPITAL_RATIO,
            "risk_multiplier": Config.RISK_MULTIPLIER,
            "traders": [],
            "max_notional": Config.MAX_TOTAL_NOTIONAL,
            "enabled": True,
            "testnet": Config.TESTNET,
            "leverage": Config.LEVERAGE,
        })

    def _parse(self, data: list):
        with self._lock:
            self._accounts.clear()
            for item in data:
                try:
                    acc = AccountConfig(item)
                    self._accounts[acc.id] = acc
                except Exception as e:
                    logger.warning(f"账户配置解析失败: {e}, data={item}")

    def get_accounts(self) -> List[AccountConfig]:
        """获取所有启用的账户"""
        with self._lock:
            return [a for a in self._accounts.values() if a.enabled]

    def get_account(self, account_id: str) -> Optional[AccountConfig]:
        with self._lock:
            return self._accounts.get(account_id)

    def list_accounts(self) -> List[dict]:
        """列出所有账户（脱敏）"""
        with self._lock:
            return [a.to_dict() for a in self._accounts.values()]

    def save_accounts_template(self):
        """生成账户配置模板文件"""
        template = [
            {
                "id": "main",
                "api_key": "YOUR_MAIN_API_KEY",
                "api_secret": "YOUR_MAIN_API_SECRET",
                "capital_ratio": 0.1,
                "risk_multiplier": 1.0,
                "traders": ["lanaai"],
                "max_notional": 5000,
                "enabled": True,
                "testnet": False,
                "leverage": 5
            },
            {
                "id": "sub1",
                "api_key": "YOUR_SUB_API_KEY",
                "api_secret": "YOUR_SUB_API_SECRET",
                "capital_ratio": 0.05,
                "risk_multiplier": 0.8,
                "traders": ["lanaai"],
                "max_notional": 2000,
                "enabled": False,
                "testnet": True,
                "leverage": 3
            }
        ]
        template_path = self._file.replace(".json", ".example.json")
        with open(template_path, "w", encoding="utf-8") as f:
            json.dump(template, f, indent=2, ensure_ascii=False)
        logger.info(f"账户配置模板已生成: {template_path}")
        return template_path
