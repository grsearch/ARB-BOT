# BSC ↔ Binance Perpetual Arbitrage Bot

**正基差套利机器人**：监控币安合约 24h 涨幅榜 Top N，与 PancakeSwap V3 现货价格对比，当永续 > 现货 + 阈值时，币安开空 + BSC 买现货对冲。

## 核心特性

- **三级筛选扫描**：币安官方 BSC 白名单 → Top Gainers → PancakeSwap 池子 TVL，每 15 分钟刷新
- **双通道价格流**：Birdeye WebSocket 主 + 链上 `slot0` 兜底（>3s 无更新自动切链上）
- **低延迟 DEX 执行**：Keep-Alive RPC session、Nonce 预缓存、Gas 预热、永久 approve、V3 `exactInputSingle` 直调
- **精细时间戳**：每笔交易记录 `signal → cex_sent → cex_filled → dex_sent → dex_confirmed` 五个时间点
- **准确 PnL**：含币安 taker fee (0.045%)、V3 pool fee、BSC gas，区分 Gross / Net
- **Dashboard**：FastAPI + Alpine.js 单页，实时基差、池子 TVL/手续费、延迟分阶段、点击交易行展开时间线
- **安全**：`DRY_RUN` 默认开启、紧急平仓保护、延迟超限自动强平

## 架构

```
┌─────────────────┐     ┌──────────────────┐     ┌──────────────┐
│  Binance WS     │     │  Birdeye WS      │     │  BSC RPC     │
│  bookTicker     │     │  + on-chain slot0│     │  (签名+sendTx)│
└────────┬────────┘     └────────┬─────────┘     └──────┬───────┘
         │                       │                       │
         └──────────┬────────────┴───────────┬───────────┘
                    ▼                        ▼
            ┌──────────────┐        ┌─────────────────┐
            │  ArbEngine   │───────▶│ CEX Executor    │
            │  (state mc)  │        │ DEX Executor    │
            └───────┬──────┘        └─────────────────┘
                    ▼
            ┌──────────────┐
            │   SQLite     │
            │  (trades,    │
            │   candidates,│
            │   events)    │
            └──────┬───────┘
                   ▼
            ┌──────────────┐
            │ FastAPI      │◀──── Browser Dashboard
            │ + WS push    │
            └──────────────┘
```

## 快速开始

```bash
git clone <repo>
cd arb_bot
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && vim .env     # 填入密钥，保持 DRY_RUN=true
python -m backend.main
# 浏览器打开 http://localhost:8000
```

## 部署到 AWS Tokyo

见 [DEPLOY.md](DEPLOY.md)。若使用 Binance AI PRO 代理部署，直接复制 [AI_PRO_DEPLOY_PROMPT.md](AI_PRO_DEPLOY_PROMPT.md) 给它。

## 项目结构

```
arb_bot/
├── backend/
│   ├── config.py            # 全局配置 + 热更新参数
│   ├── abi.py               # 精简 ABI
│   ├── db.py                # SQLite 持久化
│   ├── scanner.py           # 三级筛选扫描器
│   ├── cex_feed.py          # 币安 WS bookTicker
│   ├── dex_feed.py          # Birdeye WS + 链上 slot0
│   ├── cex_executor.py      # 币安合约下单
│   ├── dex_executor.py      # V3 SwapRouter 极速执行
│   ├── engine.py            # 套利决策状态机
│   ├── api.py               # FastAPI Dashboard 后端
│   └── main.py              # 启动入口
├── frontend/
│   └── index.html           # Dashboard (Alpine.js + Tailwind, 无构建)
├── data/                    # SQLite 数据库（持久化）
├── logs/
├── .env.example
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── DEPLOY.md                # 部署与测试文档
├── AI_PRO_DEPLOY_PROMPT.md  # Binance AI PRO 部署指令
└── README.md
```

## 关键参数（.env 或 Dashboard 可调）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `ENTRY_THRESHOLD` | 0.025 | 基差 ≥ 2.5% 开仓 |
| `EXIT_THRESHOLD` | 0.005 | 基差 ≤ 0.5% 平仓 |
| `POSITION_USDT` | 500 | 单笔仓位 |
| `MAX_SLIPPAGE` | 0.012 | DEX 最大滑点 1.2% |
| `LEVERAGE` | 3 | 合约杠杆 |
| `MAX_CONCURRENT_POSITIONS` | 2 | 最多同时持仓数 |
| `MAX_EXEC_LATENCY_MS` | 2000 | 执行超时强平 |
| `MIN_POOL_TVL_USD` | 100000 | 池子 TVL 下限 |
| `TOP_N_GAINERS` | 10 | 涨幅榜扫描数量 |
| `SCAN_INTERVAL_SEC` | 900 | 15 分钟扫一次 |
| `DRY_RUN` | true | **首次部署必须保持 true** |

## 风险提示

1. **单腿风险**：CEX 成交但 DEX 失败 → 代码有紧急平仓，但极端情况仍可能亏损
2. **滑点**：低流动性 Alt 币 DEX 成交价可能差 2-5%
3. **合约风险**：扫描到的池子不会自动检测貔貅盘，建议初期用白名单 (`MANUAL_OVERRIDE`)
4. **法律合规**：某些地区禁止衍生品，自行评估

## 许可

MIT

## 免责声明

本软件仅供学习研究。使用者对所有交易行为及资金损失自行负责。DYOR，不构成投资建议。
