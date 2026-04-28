#!/bin/bash
# ════════════════════════════════════════════════════════
#  币安跟单系统 Pro — 一键部署脚本（Ubuntu 22.04）
#  用法: bash scripts/deploy.sh
# ════════════════════════════════════════════════════════
set -e

INSTALL_DIR="/opt/binance-copytrader-pro"
SERVICE_NAME="binance-copytrader"
PYTHON="python3"

echo "======================================"
echo "  币安跟单系统 Pro — 部署开始"
echo "======================================"

# 1. 安装依赖
echo "[1/5] 安装系统依赖..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-pip git

# 2. 复制文件
echo "[2/5] 安装到 $INSTALL_DIR ..."
sudo mkdir -p "$INSTALL_DIR"
sudo cp -r . "$INSTALL_DIR/"
sudo mkdir -p "$INSTALL_DIR/logs" "$INSTALL_DIR/data"

# 3. 安装 Python 依赖
echo "[3/5] 安装 Python 依赖..."
sudo pip3 install -q -r "$INSTALL_DIR/requirements.txt"

# 4. 配置文件
if [ ! -f "$INSTALL_DIR/.env" ]; then
    echo "[4/5] 创建配置文件（请编辑 $INSTALL_DIR/.env）..."
    sudo cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    echo "⚠️  请先编辑 $INSTALL_DIR/.env 填入 API Key 和交易员链接，再启动服务！"
else
    echo "[4/5] 配置文件已存在，跳过。"
fi

# 5. 注册 systemd 服务
echo "[5/5] 注册 systemd 服务..."
sudo sed "s|/opt/binance-copytrader-pro|$INSTALL_DIR|g" \
    "$INSTALL_DIR/scripts/binance-copytrader.service" \
    | sudo tee "/etc/systemd/system/$SERVICE_NAME.service" > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

echo ""
echo "======================================"
echo "  部署完成！"
echo "======================================"
echo ""
echo "常用命令："
echo "  启动服务:  sudo systemctl start $SERVICE_NAME"
echo "  查看日志:  sudo journalctl -u $SERVICE_NAME -f"
echo "  停止服务:  sudo systemctl stop $SERVICE_NAME"
echo "  重启服务:  sudo systemctl restart $SERVICE_NAME"
echo ""
echo "Web 面板:   http://<服务器IP>:5000"
echo ""
echo "⚠️  首次使用请先编辑: sudo nano $INSTALL_DIR/.env"
