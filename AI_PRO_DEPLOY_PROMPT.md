# 给 Binance AI PRO 的部署指令（复制整段发给它）

---

请帮我在 AWS Tokyo EC2 Ubuntu 22.04 服务器上部署一个 Python 套利机器人，并用 **Cloudflare Tunnel** 暴露 Dashboard 给我访问。

## 我会提供

1. GitHub 仓库 URL：`<我稍后告诉你>`
2. EC2 SSH 访问（你已经有了）
3. 当你要 .env 里的密钥时，我会逐个告诉你：
   - BINANCE_API_KEY / BINANCE_API_SECRET
   - BSC_RPC_HTTP（我的 QuickNode/GetBlock 节点URL）
   - WALLET_PRIVATE_KEY / WALLET_ADDRESS
   - BIRDEYE_API_KEY

## 请按以下步骤执行，每步完成后告诉我结果

### Step 1: 系统依赖

```bash
sudo apt-get update
sudo apt-get install -y python3.11 python3.11-venv python3-pip git curl build-essential
# 若 python3.11 不可用，用 python3 (3.10+) 也行

# Node.js 20 (用于 pm2 进程守护)
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs
sudo npm install -g pm2

# Cloudflared（Cloudflare Tunnel 客户端，原生支持 WebSocket）
curl -L --output /tmp/cloudflared.deb \
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i /tmp/cloudflared.deb
cloudflared --version   # 确认安装成功
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
chmod 600 .env
```

然后逐个询问我密钥，用 sed 写入。例如：

```bash
# 我告诉你 BINANCE_API_KEY 值为 xxx 后执行：
sed -i "s|^BINANCE_API_KEY=.*|BINANCE_API_KEY=xxx|" .env
# 依次处理：
# BINANCE_API_SECRET, BSC_RPC_HTTP, WALLET_PRIVATE_KEY,
# WALLET_ADDRESS, BIRDEYE_API_KEY
```

⚠️ 所有密钥**只写入 .env 文件**，不要 echo 到日志或终端
⚠️ 保持 `.env` 里 `DRY_RUN=true`（首次部署）

### Step 4: 配置 systemd 服务（bot 主程序）

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

### Step 5: 用 Cloudflare Tunnel + pm2 暴露 Dashboard

Cloudflare Quick Tunnel **原生支持 WebSocket**，免登录，5秒开通。

```bash
# 先杀掉可能存在的旧隧道
pm2 delete arb-tunnel 2>/dev/null || true

# 启动 quick tunnel
pm2 start "cloudflared tunnel --no-autoupdate --url http://localhost:8000" --name arb-tunnel

# 等 cloudflared 初始化
sleep 10

# 从日志提取 URL（cloudflared 会输出 https://xxx.trycloudflare.com）
CF_URL=$(pm2 logs arb-tunnel --lines 50 --nostream 2>/dev/null | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | head -1)

# 开机自启
sudo env PATH=$PATH:/usr/bin pm2 startup systemd -u ubuntu --hp /home/ubuntu
pm2 save

echo ""
echo "================================================"
echo "✅ 部署完成"
echo ""
echo "Dashboard URL:  $CF_URL"
echo ""
echo "✅ WebSocket 原生支持（Dashboard 右上角应显示 LIVE (WS) 绿灯）"
echo "✅ 无需密码，直接访问"
echo "⚠️ Quick Tunnel URL 每次重启会变。需要固定URL就绑自己的域名"
echo "================================================"
```

### Step 6: 验证 WS 连接

```bash
# 让用户打开 Dashboard URL，在浏览器开发者工具里看 Console 有没有WS报错。
# 正常情况下 Dashboard 右上角圆点应该是绿色闪烁 "LIVE (WS)"

# 在服务器上也可以测：
curl -s $CF_URL/api/stats | head -5

# 看 pm2 tunnel 日志确认连接
pm2 logs arb-tunnel --lines 20 --nostream
```

### Step 7: 汇报给我

完成后告诉我以下信息：

- [ ] `sudo systemctl status arb-bot` 显示 active/running ？
- [ ] bot.log 里最后 10 行内容（贴给我看）
- [ ] Dashboard URL（trycloudflare.com 的）
- [ ] Dashboard 右上角状态是 "LIVE (WS)" 绿灯 还是 "LIVE (polling 3s)" 橙灯？

## 故障排查（我要求时再执行）

```bash
# bot 日志
tail -100 /home/ubuntu/arb_bot/logs/bot.log
tail -100 /home/ubuntu/arb_bot/logs/bot.err

# tunnel 日志
pm2 logs arb-tunnel --lines 50 --nostream

# 重启 bot
sudo systemctl restart arb-bot

# 重启 tunnel（注意：会换新 URL）
pm2 restart arb-tunnel
sleep 10
pm2 logs arb-tunnel --lines 30 --nostream | grep trycloudflare

# 查看最近交易
sqlite3 /home/ubuntu/arb_bot/data/arb.db \
  "SELECT id, symbol, status, entry_basis_pct, exec_latency_ms_open, realized_pnl_usdt FROM trades ORDER BY id DESC LIMIT 10;"

# 查看扫描到的候选池
sqlite3 /home/ubuntu/arb_bot/data/arb.db \
  "SELECT symbol, pool_fee_pct, pool_tvl_usd, change_24h_pct FROM candidates;"
```

## 重要纪律

1. ❌ **不要把 DRY_RUN 改成 false**。这是首次部署，必须模拟运行
2. ❌ **不要修改代码**，除非日志明确报错需要修 bug
3. ❌ **不要把密钥 echo 到终端/日志**。只能 sed 写入 .env
4. ❌ **不要 `sudo rm -rf`** 等危险操作
5. ❌ **不要用 root 运行 bot**。必须是 ubuntu 用户
6. ✅ 每步执行后**等我确认再继续**下一步
7. ✅ 遇到报错，把完整日志贴给我，**不要自己擅自修改 .env 或代码**

---

开始吧，完成 Step 1 后告诉我。
