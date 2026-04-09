# 🤖 Memecoin Trading Bot v3 — Quantitative Edition

A real-time memecoin scanner and trading signal bot built in Python. Monitors live DEX pairs across multiple chains, scores tokens using a quantitative engine, and sends trade alerts via Telegram.

---

## Features

- **Quantitative Scoring Engine** — signals are scored using RSI momentum, Volume Z-Score, Buy Pressure Index, Liquidity Depth, and ATR volatility penalty
- **Rug Detection** — built-in rug risk analyzer blocks high-risk tokens before entry
- **Multi-chain Support** — scans Solana, Ethereum, BSC, Polygon, Fantom, and Avalanche
- **Telegram Alerts** — real-time entry, TP, SL, and rug block notifications
- **Position Management** — automatic TP1/TP2/TP3 and stop-loss tracking with trade timeout
- **Three Trading Profiles** — BALANCED, AGGRESSIVE, CONSERVATIVE
- **Memory Management** — TTL-based cleanup to prevent memory leaks on long runs
- **Trade Logging** — all closed trades saved to `bot_trades.json` with full PnL history

---

## How It Works

1. Fetches live pairs from DexScreener API (trending, new, pump, hot, gainers)
2. Filters tokens by liquidity, volume, market cap, chain, and age
3. Scores each token out of 100 using the quantitative engine
4. Blocks high rug-risk tokens automatically
5. Opens a virtual position when score exceeds the entry threshold
6. Monitors price and fires TP/SL alerts to Telegram in real time

---

## Setup

### Requirements
```bash
python -m pip install requests colorama
```

### Configuration

Copy `.env.example` to `.env` and fill in your keys:

```
GROQ_API_KEY=your_groq_api_key
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id
```

### Run
```bash
python bot.py
```

---

## Trading Profiles

| Profile | Entry Score | Stop Loss | Max Positions | Max Rug Risk |
|---|---|---|---|---|
| BALANCED | 70 | 10% | 5 | 45% |
| AGGRESSIVE | 55 | 8% | 10 | 55% |
| CONSERVATIVE | 80 | 12% | 3 | 35% |

Change the profile in `bot.py`:
```python
PROFILE = "BALANCED"  # BALANCED | AGGRESSIVE | CONSERVATIVE
```

---

## Take Profit & Stop Loss

| Level | Default |
|---|---|
| TP1 | +25% |
| TP2 | +50% |
| TP3 | +100% |
| Stop Loss | -10% |

---

## Files

| File | Description |
|---|---|
| `bot.py` | Main trading bot |
| `prediction_bot.py` | Stock analysis bot powered by Groq AI |
| `world_affairs_bot.py` | World affairs analysis bot |
| `dex_auto_scanner.py` | DEX pair auto scanner |
| `dex_monitor.py` | DEX price monitor |
| `requirements.txt` | Python dependencies |
| `bot_trades.json` | Trade history log (auto-generated) |

---

## Disclaimer

This bot is for **educational and research purposes only**. It does not execute real trades. Nothing here constitutes financial advice. Always do your own research before making any investment decisions.
