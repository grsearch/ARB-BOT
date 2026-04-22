# 给 Binance AI PRO 的部署指令（复制整段发给它）

---

请帮我在 AWS Tokyo EC2 Ubuntu 22.04 服务器上部署一个 Python 套利机器人，并用 localtunnel 暴露 Dashboard 给我访问。

## 我会提供

1. GitHub 仓库 URL：`<我稍后告诉你>`
2. EC2 SSH 访问（你已经有了）
3. 当你要 .env 里的密钥时，我会逐个告诉你：
   - BINANCE_API_KEY / BINANCE_API_SECRET
   - BSC_RPC_HTTP
   - WALLET_PRIVATE_KEY / WALLET_ADDRESS
   - BIRDEYE_API_KEY

## 请按以下步骤执行，每步完成后告诉我结果

### Step 1: 系统依赖
```bash
sudo apt-get update
sudo apt-get install -y python3.11 python3.11-venv python3-pip git curl build-essential
# 若 python3.11 不可用，改用 python3 (应为 3.10+)
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs
sudo npm install -g localtunnel pm2
```

### Step 2: 克隆并安装
```bash
cd ~
git clone <GitHub URL> arb_bot
cd arb_bot
python3.11 -m venv .venv || python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
mkdir -p data logs
```

### Step 3: 向我要密钥，写入 .env
```bash
cp .env.example .env
# 告诉我："请提供 BINANCE_API_KEY"，我回复后，你用 sed 写入：
# sed -i 's|^BINANCE_API_KEY=.*|BINANCE_API_KEY=用户回复的值|' .env
# 然后依次询问 BINANCE_API_SECRET, BSC_RPC_HTTP, WALLET_PRIVATE_KEY,
# WALLET_ADDRESS, BIRDEYE_API_KEY
# ⚠️ 所有密钥只写入 .env，不要 echo 到日志或终端
# ⚠️ 保持 .env 里 DRY_RUN=true（这是首次部署）
chmod 600 .env
```

### Step 4: 配置 systemd 服务

```bash
sudo tee /etc/systemd/system/arb-bot.service > /dev/null <<'EOF'
[Unit]
Description=BSC-Binance Arb Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/arb_bot
Environment="PATH=/home/ubuntu/arb_bot/.venv/bin"
ExecStart=/home/ubuntu/arb_bot/.venv/bin/python -m backend.main
Restart=always
RestartSec=5
StandardOutput=append:/home/ubuntu/arb_bot/logs/bot.log
StandardError=append:/home/ubuntu/arb_bot/logs/bot.err

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable arb-bot
sudo systemctl start arb-bot
sleep 5
sudo systemctl status arb-bot --no-pager | head -20
tail -30 /home/ubuntu/arb_bot/logs/bot.log
```

期望看到 "active (running)" 和日志里有 "===== Bot starting ====="。

### Step 5: 用 localtunnel + pm2 暴露 Dashboard

```bash
SUBDOMAIN="arbbot-$(head -c 8 /dev/urandom | base64 | tr -dc 'a-z0-9' | head -c 6)"
echo "Subdomain: $SUBDOMAIN"

# 先杀掉可能存在的旧进程
pm2 delete arb-tunnel 2>/dev/null || true
pkill -f "lt --port 8000" 2>/dev/null || true

# 用 pm2 守护 localtunnel
pm2 start "lt --port 8000 --subdomain $SUBDOMAIN" --name arb-tunnel

# 开机自启
sudo env PATH=$PATH:/usr/bin pm2 startup systemd -u ubuntu --hp /home/ubuntu
pm2 save

sleep 5
pm2 logs arb-tunnel --lines 10 --nostream

# 取 EC2 公网 IP (就是 localtunnel 的访问密码)
PUBLIC_IP=$(curl -s ifconfig.me)
echo ""
echo "================================"
echo "✅ 部署完成"
echo ""
echo "Dashboard URL:  https://$SUBDOMAIN.loca.lt"
echo "Tunnel 密码:    $PUBLIC_IP"
echo "================================"
echo ""
echo "首次访问说明："
echo "1. 浏览器打开 URL，如果出现 'Friendly Reminder' 页面，点击 'Click to Continue'"
echo "2. 如果要输入密码/tunnel password, 输入 $PUBLIC_IP"
echo "3. 进入 Dashboard 后，初始是 DRY_RUN 模式（模拟交易），不会真实下单"
echo ""
```

### Step 6: 汇报给我（完成后告诉我以下信息）

- [ ] systemd 状态（active/running？）
- [ ] bot.log 里最后10行（确认没错）
- [ ] Dashboard URL
- [ ] localtunnel 密码（EC2 公网 IP）

## 重要纪律

1. ❌ **不要把 DRY_RUN 改成 false**。这是首次部署，必须模拟运行
2. ❌ **不要修改代码**，除非日志明确报错需要修 bug
3. ❌ **不要把密钥 echo 到终端**。只能 sed 写入 .env
4. ❌ **不要 `sudo rm -rf`** 等危险操作
5. ❌ **不要用 root 运行 bot**。必须是 ubuntu 用户
6. ✅ 每步执行后**等我确认再继续**下一步
7. ✅ 如果遇到报错，把完整日志发给我，**不要自己擅自修改 .env 或代码**

## 故障排查命令（只有我要求才执行）

```bash
# 查看 bot 日志
tail -100 /home/ubuntu/arb_bot/logs/bot.log

# 查看错误
tail -100 /home/ubuntu/arb_bot/logs/bot.err

# 查看 tunnel 日志
pm2 logs arb-tunnel --lines 50 --nostream

# 重启 bot
sudo systemctl restart arb-bot

# 重启 tunnel
pm2 restart arb-tunnel

# 查数据库
sqlite3 /home/ubuntu/arb_bot/data/arb.db "SELECT symbol, status, realized_pnl_usdt, exec_latency_ms_open FROM trades ORDER BY id DESC LIMIT 10;"
```

---

开始吧，完成 Step 1 后告诉我。
