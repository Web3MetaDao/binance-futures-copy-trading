# 部署与运维手册 (Deployment Guide)

本文档详细说明了如何在生产环境中部署、配置和运维 **币安跟单系统 Pro V2**。

## 1. 环境准备

### 1.1 硬件要求
- **CPU**: 2 核及以上
- **内存**: 4GB 及以上（推荐 8GB 以支持 Redis 和多账户并发）
- **磁盘**: 20GB SSD
- **网络**: 必须能够稳定访问 Binance API（`api.binance.com`, `fapi.binance.com`, `fstream.binance.com`），推荐使用 AWS 东京 (ap-northeast-1) 或香港节点。

### 1.2 软件依赖
- **操作系统**: Ubuntu 20.04 / 22.04 LTS
- **Python**: 3.9+ (推荐 3.11)
- **Docker**: 20.10+ (如使用 Docker 部署)
- **Redis**: 6.0+ (可选，用于消息队列解耦)

---

## 2. 部署方式

系统支持三种部署方式，请根据您的运维习惯选择其一。

### 方式一：Docker Compose 一键部署（推荐）

这是最简单、最稳定的部署方式，内置了 Redis 和应用容器。

```bash
# 1. 解压项目
tar -xzf binance-copytrader-pro-v2.tar.gz
cd binance-copytrader-pro

# 2. 配置环境变量
cp .env.example .env
nano .env  # 填入您的 API Key 和交易员链接

# 3. 启动服务
docker compose up -d

# 4. 查看日志
docker compose logs -f app
```

### 方式二：Systemd 守护进程部署

适合不希望使用 Docker，希望直接在宿主机运行的用户。

```bash
# 1. 解压并进入目录
tar -xzf binance-copytrader-pro-v2.tar.gz
cd binance-copytrader-pro

# 2. 安装依赖
sudo apt update && sudo apt install -y python3-pip python3-venv redis-server
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. 配置环境变量
cp .env.example .env
nano .env

# 4. 执行一键部署脚本
bash scripts/deploy.sh

# 5. 查看状态与日志
sudo systemctl status binance-copytrader
journalctl -u binance-copytrader -f
```

### 方式三：直接运行（仅限测试/开发）

```bash
pip3 install -r requirements.txt
python3 main.py
```

---

## 3. 核心配置说明 (`.env`)

以下是 V2 版本新增和核心的配置项说明：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `BINANCE_TESTNET` | `false` | 是否使用测试网。**强烈建议首次部署设为 true**。 |
| `COMPOUND_ENABLED` | `true` | 是否开启真复利模式（平仓后立即用最新余额更新基础资金）。 |
| `WS_ENABLED` | `true` | 是否开启 WebSocket 实时推送（毫秒级延迟）。 |
| `REDIS_ENABLED` | `true` | 是否启用 Redis 消息队列。若为 false，将自动降级为内存队列。 |
| `SM_SOURCE_ENABLED` | `true` | 是否启用 Smart Money 备用信号链路。 |
| `SM_SIGNAL_MERGE_MODE` | `fallback` | `fallback` (主链路失败时切换) 或 `merge` (主备融合)。 |
| `PROMETHEUS_PORT` | `8000` | Prometheus 指标暴露端口。 |

---

## 4. 多账户配置

V2 版本支持同时管理多个 Binance 子账户。

1. 复制模板文件：
   ```bash
   cp logs/accounts.example.json logs/accounts.json
   ```
2. 编辑 `accounts.json`，为每个账户配置独立的 API Key、资金比例和跟单目标。
3. 重启服务，系统将自动为每个启用的账户启动独立的交易引擎。

---

## 5. 监控与告警

### 5.1 Prometheus + Grafana
系统在 `http://<服务器IP>:8000/metrics` 暴露了标准 Prometheus 指标。
您可以在 Grafana 中导入这些指标，监控：
- `copytrader_active_positions` (当前持仓数)
- `copytrader_daily_pnl` (今日盈亏)
- `copytrader_api_latency_seconds` (API 延迟)
- `copytrader_error_total` (各类错误计数)

### 5.2 Telegram 告警
在 `.env` 中配置 `TG_BOT_TOKEN` 和 `TG_CHAT_ID` 后，系统会在以下情况自动推送告警：
- 账户触发最大回撤熔断
- 连续 N 次 API 调用失败
- WebSocket 连接断开且重连失败
- 每日 00:00 推送全天收益汇总

---

## 6. 常见问题排查 (FAQ)

**Q: 启动后提示 "Timestamp for this request is outside of the recvWindow"**
A: 服务器时间与 Binance 服务器时间不同步。请执行 `sudo apt install ntpdate && sudo ntpdate time.windows.com` 同步时间。

**Q: WebSocket 经常断开**
A: 检查服务器网络是否稳定。系统已内置指数退避重连机制，短时间断开会自动恢复。如果频繁断开，建议更换网络环境更好的服务器。

**Q: 如何查看历史交易记录？**
A: V2 版本已将交易记录迁移至 SQLite 数据库。您可以使用任何 SQLite 客户端打开 `logs/copytrader.db`，查询 `trades` 表。Web 面板 (`http://<服务器IP>:5000`) 也提供了可视化的交易日志查看功能。
