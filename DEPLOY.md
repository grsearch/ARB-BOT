# 部署与测试指南

本文档有两部分：
- **第一部分**：给人类开发者的完整部署指南
- **第二部分**（在文档末尾）：给 Binance AI PRO 的精简任务指令，可直接复制粘贴给它

---

## 第一部分：人类开发者指南

### 一、服务器选型（关键！）

| 项 | 推荐 | 原因 |
|----|------|------|
| 云服务商 | **AWS EC2 Tokyo (ap-northeast-1)** | 币安撮合在东京AWS，CEX API延迟可压至5-15ms |
| 实例类型 | `t3.small` 或 `t3.medium` | 2 vCPU / 2-4 GB RAM 足够 |
| OS | Ubuntu 22.04 LTS (x86_64) | |
| 存储 | 20 GB gp3 | |
| 安全组 | 只开 `22/tcp`（SSH）、`8000/tcp`（Dashboard，可选） | 其他端口不开 |

> ⚠️ 避免欧美机房。币安API往返150-300ms，延迟这么高做套利必亏。

### 二、BSC RPC 端点

需要**付费私有节点**，放亚洲区域：
- [QuickNode](https://www.quicknode.com) 新加坡/东京
- [GetBlock](https://getblock.io/) 新加坡
- [NodeReal](https://nodereal.io/) 新加坡

公共节点（`bsc-dataseed.binance.org` 等）限速严重，不要拿来当主RPC。

### 三、本地运行

```bash
# 1. 克隆仓库（从GitHub）
git clone <你的仓库URL>
cd arb_bot

# 2. Python 3.11+
python3.11 -m venv .venv
source .venv/bin/activate

# 3. 依赖
pip install -r requirements.txt

# 4. 配置
cp .env.example .env
vim .env   # 填入你的密钥

# 5. ⚠️ 首次运行确保 DRY_RUN=true
python -m backend.main
```

访问 `http://localhost:8000` 查看Dashboard。

### 四、Docker 运行

```bash
cp .env.example .env && vim .env
docker compose up -d --build
docker compose logs -f
```

### 五、三阶段测试流程

#### 阶段 1：DRY_RUN 观察（至少24小时）
- `.env` 保持 `DRY_RUN=true`
- 不真实下单，只记录"如果开仓会怎样"
- 观察 Dashboard 上：
  - **延迟分布**（p50 / p90 / p99）——决定你的VPS和RPC够不够快
  - **候选池**——扫描器是否找得到有TVL的标的
  - **理论PnL**——策略是否真的能赚钱

#### 阶段 2：小额真实（3-7 天）
```
DRY_RUN=false
POSITION_USDT=100
MAX_CONCURRENT_POSITIONS=1
ENTRY_THRESHOLD=0.03
MAX_EXEC_LATENCY_MS=1500
```
- BSC 钱包：200 USDT + 0.05 BNB（手续费）
- 币安合约：500 USDT
- 观察前 20 笔交易的净PnL（含手续费）

#### 阶段 3：逐步放量
每放大一倍观察一周，看：
- 单笔滑点是否恶化
- 池子TVL是否够支撑更大仓位（一般仓位 ≤ TVL × 0.5%）

---

## 第二部分：给 Binance AI PRO 的任务指令

> 复制下面整段发给 Binance AI PRO。它会自动执行。

```
我给你一个 GitHub 仓库，请帮我在 AWS Tokyo EC2 实例上部署并运行，然后用
localtunnel 暴露 Dashboard 的公网 URL 给我访问。

=== 背景 ===
这是一个 BSC <-> Binance 永续合约的套利机器人。核心：
- Python 3.11 + FastAPI
- Dashboard 在 http://localhost:8000
- 启动命令: python -m backend.main
- 配置通过 .env 文件

=== 前置条件（你已有）===
- AWS Tokyo EC2 Ubuntu 22.04 实例 SSH 访问
- 用户持有必要的 API 密钥（会在对话中给你）

=== 你要执行的步骤 ===

步骤 1: 系统环境
  sudo apt-get update
  sudo apt-get install -y python3.11 python3.11-venv python3-pip git curl build-essential
  # 如没有 python3.11，装 python3 + venv 也行
  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
  sudo apt-get install -y nodejs
  sudo npm install -g localtunnel

步骤 2: 克隆代码
  cd ~
  git clone <用户提供的仓库URL>  arb_bot
  cd arb_bot

步骤 3: Python 虚拟环境 + 依赖
  python3.11 -m venv .venv   # 或 python3 -m venv .venv
  source .venv/bin/activate
  pip install --upgrade pip
  pip install -r requirements.txt

步骤 4: 配置 .env
  cp .env.example .env
  # 向用户询问以下密钥，逐个填入 .env：
  #   BINANCE_API_KEY
  #   BINANCE_API_SECRET
  #   BSC_RPC_HTTP    （例如 QuickNode 新加坡节点）
  #   WALLET_PRIVATE_KEY
  #   WALLET_ADDRESS
  #   BIRDEYE_API_KEY
  # ⚠️ 首次部署务必保持 DRY_RUN=true

步骤 5: 启动 bot（后台持久运行）
  # 使用 systemd 持久化
  cat > /tmp/arb-bot.service <<'EOF'
[Unit]
Description=Arb Bot
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

  sudo mv /tmp/arb-bot.service /etc/systemd/system/arb-bot.service
  sudo systemctl daemon-reload
  sudo systemctl enable arb-bot
  sudo systemctl start arb-bot
  sleep 5
  sudo systemctl status arb-bot   # 确认 active (running)

步骤 6: 用 localtunnel 暴露 Dashboard
  # 固定子域名便于记忆，例如 arbbot-xxx
  # 使用 nohup 后台运行，保留 URL
  SUBDOMAIN="arbbot-$(date +%s | tail -c 5)"
  nohup lt --port 8000 --subdomain "$SUBDOMAIN" > /home/ubuntu/arb_bot/logs/tunnel.log 2>&1 &
  sleep 3
  cat /home/ubuntu/arb_bot/logs/tunnel.log

  # 告知用户 URL：https://$SUBDOMAIN.loca.lt
  # 注意 localtunnel 首次访问会要求输入 "tunnel password"：
  # 密码就是 EC2 实例的公网 IP，运行：
  curl ifconfig.me
  # 告诉用户这个 IP 就是 localtunnel 的访问密码

  # 给用户的完整访问说明示例：
  # "URL: https://arbbot-1234.loca.lt
  #  如果浏览器出现 Friendly Reminder 页面，点击 'Click to Continue'
  #  如果要密码，密码是: <EC2公网IP>"

步骤 7: 保持 localtunnel 连接
  # localtunnel 有时会断。安装 PM2 保证重连：
  sudo npm install -g pm2
  # 先杀掉之前的 lt 进程
  pkill -f "lt --port 8000" || true
  pm2 start "lt --port 8000 --subdomain $SUBDOMAIN" --name arb-tunnel
  pm2 save
  pm2 startup systemd -u ubuntu --hp /home/ubuntu   # 输出的命令用 sudo 执行一次

步骤 8: 汇报给用户
  - 服务状态: sudo systemctl status arb-bot
  - Dashboard URL: https://$SUBDOMAIN.loca.lt
  - localtunnel 密码: <EC2公网IP>
  - 日志文件路径:
      /home/ubuntu/arb_bot/logs/bot.log  (主程序)
      /home/ubuntu/arb_bot/logs/bot.err  (错误)
      /home/ubuntu/arb_bot/logs/tunnel.log (tunnel)
  - 数据库路径: /home/ubuntu/arb_bot/data/arb.db

=== 常见问题处理 ===

如果 systemctl status 显示 failed:
  tail -50 /home/ubuntu/arb_bot/logs/bot.err

如果 tunnel 断开:
  pm2 restart arb-tunnel
  pm2 logs arb-tunnel --lines 20

如果 web3.py 报 "geth_poa_middleware" 缺失，代码已自动兼容 v6/v7。
如果 Python 3.11 装不上，用 python3 (3.10) 也行，修改 systemd 的 ExecStart 路径即可。

=== 不要做的事 ===
1. 不要把 DRY_RUN 改成 false，除非用户明确说可以
2. 不要修改 .env 里除密钥外的参数（那些参数用户通过 Dashboard 调整）
3. 不要 sudo rm -rf 之类的危险操作
4. 不要往代码里硬编码密钥
5. 所有密钥只写入 .env 文件，不要 echo 到日志
```

---

## 常见坑

### 1. `Too little received`（DEX revert）
- 原因：你估算价 vs 实际swap时池子已经动了
- 对策：降低 POSITION_USDT 或提高 MAX_SLIPPAGE（但不要超过 2%）

### 2. `nonce too low`
- 代码里有 `_reset_nonce_from_chain()` 兜底
- 重启 systemctl restart arb-bot 可手动修复

### 3. Birdeye WS 掉线
- 已有自动重连。超过 3 秒无更新，链上 slot0 会接管

### 4. 池子TVL不够导致滑点大
- 原因：仓位相对池子太大
- 经验法则：仓位 USDT ≤ TVL × 0.5%
- 在 Dashboard Config 里调高 min_pool_tvl_usd

### 5. 币安合约"单边持仓模式 vs 双边持仓模式"
- 代码默认假设单边持仓（one-way mode）
- 如果你的账户是双边（hedge mode），需要手动切回单边，或在 `cex_executor.py` 下单时加 `positionSide` 参数

### 6. 貔貅币（只能买不能卖）
- 代码不会自动识别，依赖你的 `MANUAL_OVERRIDE` 白名单
- 一般流动性 > 100k USDT 的 Top Gainer 不会是貔貅，但还是建议先用白名单验证

---

## 风险提示

1. **单腿风险**：CEX 成交但 DEX 失败 → 紧急平CEX逻辑会触发，但极端情况下仍可能亏损
2. **滑点**：低流动性 Alt 币 DEX 成交价可能差 2-5%
3. **Gas 飙升**：代码自动 *1.3 加价，但拥堵时还是可能慢
4. **合约风险**：自动扫描的池子没做貔貅/增税判断，建议初期只用白名单
5. **交易所风险**：爆仓线你要自己管
6. **法律合规**：某些地区禁止衍生品，请自行评估
