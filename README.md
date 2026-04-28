# 币安跟单系统 Pro

> 基于公开实盘链接的多交易员跟单系统，支持 Smart Money 评分、完善风控、复利/固定跟单、真实回测、Telegram 通知及 Web 可视化控制面板。

---

## 功能特性

| 模块 | 功能 |
|------|------|
| **多交易员信号聚合** | 支持多个公开实盘链接，按权重聚合信号，方向冲突时按净权重决策 |
| **Smart Money 备用链路** | 主链路失败时自动切换到 Binance 排行榜聪明钱；支持 fallback（切换）/ merge（融合）双模式 |
| **Smart Money 评分** | 综合资金费率、OI 变化、头部多空比、ATR 波动率、成交量确认打分 |
| **完善风控** | 止损/止盈挂单、最大回撤熔断、空仓快照保护、API 指数退避重试 |
| **真复利/固定跟单** | 真复利模式：每笔平仓后立即更新基础资金，盈利自动扩仓、亏损自动缩仓；可叠加绩效软降杠杆 |
| **绩效追踪** | 滚动计算盈亏比、夏普率，绩效不达标时自动降仓或暂停开仓 |
| **真实回测** | 含手续费（bps）、滑点（bps）、复利/固定模式，输出完整报告 |
| **Telegram 通知** | 开仓/平仓/熔断/每日汇总实时推送 |
| **Web 控制面板** | 实时仓位、收益曲线、信号列表、交易日志、运行时参数配置 |
| **状态持久化** | 重启后自动恢复持仓快照、绩效窗口、初始余额 |
| **服务器部署** | 支持 systemd 一键部署和 Docker Compose 部署 |

---

## 目录结构

```
binance-copytrader-pro/
├── core/
│   ├── config.py          # 配置加载（.env / 环境变量）
│   ├── binance_client.py  # Binance USDT-M Futures 客户端
│   ├── lead_source.py        # 多交易员公开实盘抓取与信号聚合
│   ├── smart_money_source.py # Smart Money 备用信号链路（排行榜抓取+双链路路由）
│   ├── smart_money.py        # Smart Money 评分引擎
│   ├── engine.py          # 风控执行引擎
│   ├── performance.py     # 绩效追踪模块
│   └── backtest.py        # 真实回测模块
├── web/
│   ├── app.py             # Flask Web 控制面板后端
│   └── static/
│       └── index.html     # 前端单页面（无需 Node.js）
├── utils/
│   ├── logger.py          # 结构化 JSON 日志
│   ├── notifier.py        # Telegram 通知
│   └── state.py           # 状态持久化
├── scripts/
│   ├── deploy.sh          # 一键部署脚本（Ubuntu）
│   └── binance-copytrader.service  # systemd 服务文件
├── data/
│   └── backtest_sample.csv  # 示例回测数据
├── logs/                  # 运行日志（自动创建）
├── main.py                # 主入口
├── Dockerfile             # Docker 镜像
├── docker-compose.yml     # Docker Compose 配置
├── requirements.txt       # Python 依赖
└── .env.example           # 配置模板
```

---

## 快速开始

### 方式一：直接运行（Python）

```bash
# 1. 安装依赖
pip3 install -r requirements.txt

# 2. 配置
cp .env.example .env
nano .env   # 填入 API Key、交易员链接等

# 3. 运行
python3 main.py
```

访问 Web 面板：`http://localhost:5000`

### 方式二：Docker Compose（推荐服务器部署）

```bash
cp .env.example .env
nano .env   # 填入配置

docker compose up -d
docker compose logs -f
```

### 方式三：systemd 一键部署

```bash
bash scripts/deploy.sh
sudo nano /opt/binance-copytrader-pro/.env   # 填入配置
sudo systemctl start binance-copytrader
sudo journalctl -u binance-copytrader -f
```

---

## 配置说明

### 多交易员配置

在 `.env` 中设置 `LEADERS_JSON`，支持多个交易员，每个交易员可设置不同权重：

```env
LEADERS_JSON=[
  {"name":"主力交易员","url":"https://app.binance.com/uni-qr/cpro/lanaai?l=zh-CN&r=G2QKVGHN&uc=app_square_share_link&us=copylink","weight":1.0},
  {"name":"辅助交易员","url":"https://app.binance.com/uni-qr/cpro/xxxxx?r=XXXXXXXX","weight":0.5}
]
```

**信号聚合规则：**
- 同一交易对多个交易员方向一致 → 权重累加，置信度更高
- 方向冲突 → 按净权重决定方向（净权重为 0 则跳过）

### 风控参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `STOP_LOSS_PCT` | 0.05 | 止损比例（5%），开仓后自动挂止损单 |
| `TAKE_PROFIT_PCT` | 0.10 | 止盈比例（10%），开仓后自动挂止盈单 |
| `MAX_DRAWDOWN_PCT` | 0.15 | 最大回撤 15% 触发熔断，暂停交易 |
| `CIRCUIT_BREAKER_MINUTES` | 60 | 熔断暂停时间（分钟） |
| `EMPTY_SNAPSHOT_TOLERANCE` | 3 | 连续空快照容忍次数（防误平仓） |

### Smart Money 评分

评分由以下 5 个维度加权计算（总分 0~1）：

| 维度 | 权重 | 说明 |
|------|------|------|
| 资金费率 | 20% | 做多时负费率有利，做空时正费率有利 |
| OI 变化 | 25% | 持仓量增加表明趋势确认 |
| 头部多空比 | 25% | 头部账户方向与信号一致得高分 |
| ATR 波动率 | 15% | 高波动时降分，极端波动直接跳过 |
| 成交量确认 | 15% | 放量突破时提高跟随权重 |

### 绩效软降杠杆

| 绩效状态 | 仓位比例 |
|----------|----------|
| 盈亏比 & 夏普率均在目标区间 | 100%（满仓） |
| 绩效达到目标的 80% | 50%（半仓） |
| 绩效低于目标的 80% | 0%（暂停开仓） |

---

## 回测

```bash
# 使用示例数据回测
python3 -m core.backtest --file data/backtest_sample.csv --capital 1000

# 输出结果到 JSON 文件
python3 -m core.backtest --file data/backtest_sample.csv --capital 1000 --output data/backtest_result.json
```

**CSV 格式：**

```csv
symbol,side,entry_price,exit_price,notional_usdt,timestamp
BTCUSDT,LONG,65000,67500,200,2024-01-01 10:00:00
ETHUSDT,SHORT,3200,3050,150,2024-01-02 11:00:00
```

---

## Telegram 通知设置

1. 在 Telegram 中找 `@BotFather`，创建 Bot 获取 Token
2. 向 Bot 发送任意消息，访问 `https://api.telegram.org/bot<TOKEN>/getUpdates` 获取 Chat ID
3. 在 `.env` 中设置：

```env
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

---

## Web 控制面板

访问 `http://<服务器IP>:5000`，功能包括：

- **实时余额与绩效指标**（夏普率、盈亏比、胜率、总收益）
- **收益曲线图**（近期交易累计 PnL）
- **当前持仓**（方向、数量、开仓价、标记价、未实现盈亏）
- **聚合信号**（交易对、方向、置信度、来源交易员）
- **交易日志**（最近 50 笔操作记录）
- **运行时参数配置**（无需重启即可调整风控参数）

---

## 风险提示

> **本系统仅供学习研究使用，不构成投资建议。加密货币合约交易存在极高风险，可能导致全部本金损失。请在充分了解风险的前提下谨慎使用，建议先在测试网（`BINANCE_TESTNET=true`）验证后再上线。**

---

## 依赖

- Python 3.11+
- requests
- flask
- flask-cors
- pandas / numpy
- python-dotenv
