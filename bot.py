#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════╗
║        MEMECOIN TRADING BOT v3 - QUANTITATIVE EDITION   ║
║  Fixes: rate limiting, memory leaks, sound import,      ║
║  rug threshold tightened, token cleanup, stability       ║
╚══════════════════════════════════════════════════════════╝

Requirements:
    python -m pip install requests colorama

Run:
    python prediction_bot.py
"""

import sys
import io
import os
import math
import time
import json
import threading
import statistics
import requests
from datetime import datetime, timedelta
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

# ── Windows UTF-8 fix ─────────────────────────────────────────────────────────
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# ── Sound (Windows only, graceful fallback) ───────────────────────────────────
try:
    import winsound
    SOUND_AVAILABLE = True
except ImportError:
    SOUND_AVAILABLE = False

# ── Colorama ──────────────────────────────────────────────────────────────────
try:
    from colorama import Fore, Style, init
    init(autoreset=True)
except ImportError:
    class _Dummy:
        def __getattr__(self, _): return ""
    Fore = Style = _Dummy()


# ══════════════════════════════════════════════════════════════════════════════
#  ⚙️  CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════
class Config:
    # ── Profile ───────────────────────────────────────────────────────────────
    PROFILE = "BALANCED"          # BALANCED | AGGRESSIVE | CONSERVATIVE

    # ── Scanning ──────────────────────────────────────────────────────────────
    SCAN_INTERVAL        = 3      # seconds between full scan cycles
    MAX_WORKERS          = 20     # reduced from 50 to avoid API rate limiting
    API_DELAY            = 0.15   # seconds between API calls
    MAX_PAIRS_PER_SCAN   = 500    # cap pairs processed per cycle

    # ── Positions ─────────────────────────────────────────────────────────────
    MAX_POSITIONS        = 5
    POSITION_SIZE_USD    = 100
    MAX_TRADE_DURATION   = 60     # minutes before timeout close

    # ── Liquidity Filters ────────────────────────────────────────────────────
    MIN_LIQUIDITY        = 5_000
    MAX_LIQUIDITY        = 750_000
    MIN_VOLUME_1H        = 5_000

    # ── Memecoin Filters ─────────────────────────────────────────────────────
    MIN_MCAP             = 500
    MAX_MCAP             = 10_000_000
    MAX_TOKEN_AGE_HOURS  = 720
    ALLOWED_CHAINS       = {"solana", "ethereum", "bsc", "polygon", "fantom", "avalanche"}

    # ── Scoring ───────────────────────────────────────────────────────────────
    ENTRY_SCORE_THRESHOLD       = 70
    OPPORTUNITY_SCORE_THRESHOLD = 35
    MAX_RUG_RISK_FOR_ENTRY      = 45   # tightened from original 60

    # ── Quantitative Parameters ───────────────────────────────────────────────
    VOLUME_HISTORY_WINDOW       = 12
    Z_SCORE_BREAKOUT_THRESHOLD  = 1.5
    ATR_VOLATILITY_PENALTY_MAX  = 15

    # ── TP / SL ───────────────────────────────────────────────────────────────
    TP1_PERCENT          = 25
    TP2_PERCENT          = 50
    TP3_PERCENT          = 100
    STOP_LOSS_PERCENT    = 10
    CLOSE_ON_TP3         = True

    # ── Memory Management ─────────────────────────────────────────────────────
    ALERTED_TOKEN_TTL_MINUTES   = 60
    OPPORTUNITY_TTL_MINUTES     = 5

    # ── Alerts ────────────────────────────────────────────────────────────────
    ENABLE_SOUND         = True
    ENABLE_TELEGRAM      = True
    TELEGRAM_BOT_TOKEN   = "8715857172:AAFl5n3jvX93RPk88jmvI5skhkc5lrWkWaU"  # ⚠️ get new one from @BotFather
    TELEGRAM_CHAT_ID     = "7154373034"

    # ── Dashboard ─────────────────────────────────────────────────────────────
    SHOW_ALL_OPPORTUNITIES  = True
    DASHBOARD_EVERY_N_SCANS = 3
    LOG_FILE                = "bot_trades.json"

    @classmethod
    def apply_profile(cls):
        if cls.PROFILE == "AGGRESSIVE":
            cls.ENTRY_SCORE_THRESHOLD = 55
            cls.TP1_PERCENT, cls.TP2_PERCENT, cls.TP3_PERCENT = 20, 50, 120
            cls.STOP_LOSS_PERCENT  = 8
            cls.MAX_POSITIONS      = 10
            cls.MAX_RUG_RISK_FOR_ENTRY = 55
        elif cls.PROFILE == "CONSERVATIVE":
            cls.ENTRY_SCORE_THRESHOLD = 80
            cls.TP1_PERCENT, cls.TP2_PERCENT, cls.TP3_PERCENT = 30, 60, 150
            cls.STOP_LOSS_PERCENT  = 12
            cls.MAX_POSITIONS      = 3
            cls.MAX_RUG_RISK_FOR_ENTRY = 35

Config.apply_profile()


# ══════════════════════════════════════════════════════════════════════════════
#  📐 QUANTITATIVE ENGINE
# ══════════════════════════════════════════════════════════════════════════════
class QuantEngine:
    def __init__(self, window: int = Config.VOLUME_HISTORY_WINDOW):
        self.window = window
        self._vol_hist:   dict = defaultdict(lambda: deque(maxlen=window))
        self._price_hist: dict = defaultdict(lambda: deque(maxlen=window))
        self._lock = Lock()

    def update(self, addr: str, vol_1h: float, price: float):
        with self._lock:
            self._vol_hist[addr].append((datetime.now(), vol_1h))
            self._price_hist[addr].append((datetime.now(), price))

    def purge(self, addr: str):
        with self._lock:
            self._vol_hist.pop(addr, None)
            self._price_hist.pop(addr, None)

    def volume_zscore(self, addr: str, current_vol: float) -> float:
        with self._lock:
            hist = [v for _, v in self._vol_hist[addr]]
        if len(hist) < 3:
            return 0.0
        try:
            mu = statistics.mean(hist)
            sigma = statistics.stdev(hist)
            return 0.0 if sigma == 0 else (current_vol - mu) / sigma
        except Exception:
            return 0.0

    def rsi_momentum(self, addr: str) -> float:
        with self._lock:
            prices = [p for _, p in self._price_hist[addr]]
        if len(prices) < 3:
            return 50.0
        changes = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
        gains  = [c for c in changes if c > 0]
        losses = [-c for c in changes if c < 0]
        avg_gain = statistics.mean(gains) if gains else 0.0
        avg_loss = statistics.mean(losses) if losses else 0.0
        if avg_loss == 0:
            return 100.0
        return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))

    def atr_volatility(self, addr: str) -> float:
        with self._lock:
            prices = [p for _, p in self._price_hist[addr]]
        if len(prices) < 2:
            return 0.0
        moves = [
            abs(prices[i] - prices[i - 1]) / prices[i - 1]
            for i in range(1, len(prices)) if prices[i - 1] > 0
        ]
        return statistics.mean(moves) if moves else 0.0

    def volume_acceleration(self, addr: str) -> float:
        with self._lock:
            hist = [v for _, v in self._vol_hist[addr]]
        if len(hist) < 3:
            return 0.0
        n     = len(hist)
        x_bar = (n - 1) / 2.0
        y_bar = statistics.mean(hist)
        num   = sum((i - x_bar) * (hist[i] - y_bar) for i in range(n))
        den   = sum((i - x_bar) ** 2 for i in range(n))
        if den == 0 or y_bar == 0:
            return 0.0
        return max(-1.0, min(1.0, (num / den) / y_bar))

    @staticmethod
    def rsi_to_score(rsi: float) -> float:
        if rsi < 40:       return 0.0
        elif rsi <= 65:    return (rsi - 40.0) / 25.0 * 20.0
        elif rsi <= 75:    return 20.0 + (rsi - 65.0) / 10.0 * 5.0
        elif rsi <= 85:    return 25.0 - (rsi - 75.0) / 10.0 * 10.0
        else:              return 10.0

    @staticmethod
    def liquidity_depth_score(vol_1h: float, liq: float) -> float:
        if liq <= 0 or vol_1h <= 0:
            return 0.0
        ratio = vol_1h / liq
        score = 15.0 * math.exp(-0.5 * ((ratio - 3.0) / 2.0) ** 2)
        return max(0.0, min(15.0, score))

    @staticmethod
    def buy_pressure_index(buys: int, sells: int, change_5m: float, change_1h: float) -> float:
        if sells == 0:
            sells = 1
        ratio_score = 1.0 - 1.0 / (1.0 + (buys / sells) ** 0.7)
        momentum = 0.0
        if change_1h > 10 and change_5m > 0:   momentum = 1.0
        elif change_1h > 5 or change_5m > 2:   momentum = 0.5
        return round(min(25.0, ((ratio_score * 0.70) + (momentum * 0.30)) * 25.0), 2)

    @staticmethod
    def timeframe_confluence(c5m: float, c1h: float, c24h: float) -> float:
        bullish = sum([c5m > 1.5, c1h > 5.0, c24h > 2.0])
        if bullish == 3:   return 1.25
        elif bullish == 2: return 1.10
        return 1.0


quant = QuantEngine()


# ══════════════════════════════════════════════════════════════════════════════
#  🛡️  RUG DETECTOR
# ══════════════════════════════════════════════════════════════════════════════
class RugDetector:

    @staticmethod
    def analyze(data: dict) -> float:
        risk = 0.0
        if data["buys"] > 0 and data["sells"] > 0:
            sr = data["sells"] / data["buys"]
            if sr > 3:       risk += 30
            elif sr > 2:     risk += 20
            elif sr > 1.5:   risk += 10
        if data["liq"] > 0 and data["vol_1h"] > 0:
            r = data["vol_1h"] / data["liq"]
            if r > 20:   risk += 25
            elif r > 15: risk += 15
            elif r > 10: risk += 5
        if data["change_1h"] < -20:    risk += 20
        elif data["change_1h"] < -10:  risk += 10
        if data["buys"] + data["sells"] > 500: risk += 15
        if data["liq"] < 2_000:        risk += 10
        age = data["token_age_hours"]
        if age < 1:    risk += 10
        elif age < 6:  risk += 5
        if data["mcap"] and data["mcap"] < 1_000: risk += 5
        atr = quant.atr_volatility(data["token_addr"])
        if atr > 0.30:   risk += 10
        elif atr > 0.20: risk += 5
        return min(risk, 100.0)

    @staticmethod
    def is_safe(data: dict) -> bool:
        return RugDetector.analyze(data) < Config.MAX_RUG_RISK_FOR_ENTRY


# ══════════════════════════════════════════════════════════════════════════════
#  💹 POSITION
# ══════════════════════════════════════════════════════════════════════════════
class Position:
    def __init__(self, token_addr: str, symbol: str, name: str, price: float):
        self.token_addr    = token_addr
        self.symbol        = symbol
        self.name          = name
        self.entry_price   = price
        self.current_price = price
        self.entry_time    = datetime.now()
        self.tp1_hit = self.tp2_hit = self.tp3_hit = False

    def update(self, price: float):
        self.current_price = price

    def pnl(self) -> float:
        if self.entry_price == 0:
            return 0.0
        return (self.current_price - self.entry_price) / self.entry_price * 100.0

    def check_tp(self, price: float) -> list:
        alerts = []
        tp1 = self.entry_price * (1 + Config.TP1_PERCENT / 100)
        tp2 = self.entry_price * (1 + Config.TP2_PERCENT / 100)
        tp3 = self.entry_price * (1 + Config.TP3_PERCENT / 100)
        if price >= tp3 and not self.tp3_hit:
            self.tp3_hit = True; alerts.append(("TP3", tp3))
        elif price >= tp2 and not self.tp2_hit:
            self.tp2_hit = True; alerts.append(("TP2", tp2))
        elif price >= tp1 and not self.tp1_hit:
            self.tp1_hit = True; alerts.append(("TP1", tp1))
        return alerts

    def check_sl(self, price: float):
        sl = self.entry_price * (1 - Config.STOP_LOSS_PERCENT / 100)
        return price <= sl, sl

    def timed_out(self) -> bool:
        return (datetime.now() - self.entry_time).total_seconds() / 60 > Config.MAX_TRADE_DURATION


# ══════════════════════════════════════════════════════════════════════════════
#  🗃️  BOT STATE
# ══════════════════════════════════════════════════════════════════════════════
class BotState:
    def __init__(self):
        self.lock              = Lock()
        self.active_positions  = {}
        self.opportunities     = {}
        self.token_cache       = {}
        self.alerted_tokens    = {}        # addr -> datetime (TTL-based)
        self.trade_log         = self._load_log()
        self.start_time        = datetime.now()
        self.stats = {
            "total_scans": 0, "signals": 0, "entries": 0,
            "tp_hits": 0, "sl_hits": 0, "closed": 0, "rugs_blocked": 0,
        }

    def _load_log(self) -> list:
        if os.path.exists(Config.LOG_FILE):
            try:
                with open(Config.LOG_FILE, "r") as f:
                    return json.load(f)
            except Exception:
                return []
        return []

    def save_log(self):
        try:
            with open(Config.LOG_FILE, "w") as f:
                json.dump(self.trade_log, f, indent=2)
        except Exception:
            pass

    def cache_token(self, addr: str, name: str, symbol: str):
        self.token_cache[addr] = {"name": name, "symbol": symbol}

    def cleanup(self):
        """Expire stale entries to prevent unbounded memory growth."""
        opp_cut     = datetime.now() - timedelta(minutes=Config.OPPORTUNITY_TTL_MINUTES)
        alerted_cut = datetime.now() - timedelta(minutes=Config.ALERTED_TOKEN_TTL_MINUTES)
        with self.lock:
            # Expire opportunities
            stale_opps = [k for k, v in self.opportunities.items()
                          if v.get("timestamp", datetime.now()) < opp_cut]
            for k in stale_opps:
                self.opportunities.pop(k, None)

            # Expire alerted tokens + purge quant history
            stale_alerted = [a for a, ts in self.alerted_tokens.items() if ts < alerted_cut]
            for a in stale_alerted:
                self.alerted_tokens.pop(a, None)
                quant.purge(a)

            # Purge stale token cache
            active = set(self.active_positions) | set(self.opportunities)
            stale_cache = [k for k in self.token_cache if k not in active]
            for k in stale_cache[:100]:
                self.token_cache.pop(k, None)


state = BotState()


# ══════════════════════════════════════════════════════════════════════════════
#  🔔 ALERTS
# ══════════════════════════════════════════════════════════════════════════════
_tg_lock   = Lock()
_api_lock  = Lock()
_last_call = 0.0


def alert_sound(freq: int = 1500, duration: int = 200):
    if Config.ENABLE_SOUND and SOUND_AVAILABLE:
        try:
            winsound.Beep(freq, duration)
        except Exception:
            pass


def send_telegram(msg: str):
    if not (Config.ENABLE_TELEGRAM
            and Config.TELEGRAM_BOT_TOKEN
            and "PASTE" not in Config.TELEGRAM_BOT_TOKEN):
        return
    with _tg_lock:
        try:
            requests.post(
                f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/sendMessage",
                data={"chat_id": Config.TELEGRAM_CHAT_ID, "text": msg},
                timeout=8,
            )
        except Exception:
            pass


def calc_levels(entry: float):
    return (
        entry * (1 + Config.TP1_PERCENT / 100),
        entry * (1 + Config.TP2_PERCENT / 100),
        entry * (1 + Config.TP3_PERCENT / 100),
        entry * (1 - Config.STOP_LOSS_PERCENT / 100),
    )


def notify_entry(data: dict, score: float, rug_risk: float, diag: dict):
    sym, name, price = data["symbol"], data["name"], data["price"]
    tp1, tp2, tp3, sl = calc_levels(price)
    print(
        f"\n{Fore.GREEN}{'━'*90}\n"
        f"{Fore.GREEN}🚀 [ENTRY]  {Fore.CYAN}{sym} — {name}\n"
        f"{Fore.WHITE}   Price: ${price:.10f}  Liq: ${data['liq']:,.0f}  Vol1h: ${data['vol_1h']:,.0f}\n"
        f"{Fore.YELLOW}   Score: {score:.1f}/100  Rug: {rug_risk:.0f}%  "
        f"RSI: {diag['rsi']:.1f}  Z: {diag['z']:+.2f}  ATR: {diag['atr']*100:.1f}%\n"
        f"{Fore.CYAN}   Entry: ${price:.10f}\n"
        f"{Fore.GREEN}   TP1: ${tp1:.10f}  TP2: ${tp2:.10f}  TP3: ${tp3:.10f}\n"
        f"{Fore.RED}   SL:   ${sl:.10f}\n"
        f"{Fore.GREEN}{'━'*90}"
    )
    send_telegram(
        f"🚀 ENTRY: {sym} — {name}\n"
        f"Price: ${price:.10f}\nTP1: ${tp1:.10f} | SL: ${sl:.10f}\n"
        f"Score: {score:.1f} | Rug: {rug_risk:.0f}% | RSI: {diag['rsi']:.1f} | Z: {diag['z']:+.2f}"
    )
    alert_sound(1500)
    state.stats["entries"] += 1


def notify_tp(symbol: str, level: str, price: float, pnl: float):
    print(f"{Fore.GREEN}💰 [TP HIT]  {Fore.CYAN}{symbol}  {level}  ${price:.10f}  PnL: {pnl:+.2f}%")
    send_telegram(f"💰 {level} HIT: {symbol}\nPrice: ${price:.10f}\nProfit: {pnl:+.2f}%")
    alert_sound(1200)
    state.stats["tp_hits"] += 1


def notify_sl(symbol: str, price: float, pnl: float):
    print(f"{Fore.RED}💀 [SL HIT]  {Fore.CYAN}{symbol}  ${price:.10f}  Loss: {pnl:+.2f}%")
    send_telegram(f"💀 SL HIT: {symbol}\nPrice: ${price:.10f}\nLoss: {pnl:+.2f}%")
    alert_sound(800)
    state.stats["sl_hits"] += 1


def notify_rug(symbol: str, name: str, risk: float):
    print(f"{Fore.RED}⚠️  [RUG BLOCKED]  {Fore.CYAN}{symbol} — {name}  Risk: {risk:.0f}%")
    send_telegram(f"⚠️ RUG BLOCKED: {symbol} {name}\nRisk: {risk:.0f}%")
    alert_sound(600)
    state.stats["rugs_blocked"] += 1


# ══════════════════════════════════════════════════════════════════════════════
#  📡 RATE-LIMITED API
# ══════════════════════════════════════════════════════════════════════════════
def _get(url: str, timeout: int = 6) -> dict | None:
    global _last_call
    with _api_lock:
        wait = Config.API_DELAY - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
    try:
        r = requests.get(url, timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def fetch_pairs(query: str = "trending") -> list:
    data = _get(f"https://api.dexscreener.com/latest/dex/search?q={query}")
    return data.get("pairs", []) if data else []


# ══════════════════════════════════════════════════════════════════════════════
#  🔬 SIGNAL SCORING
# ══════════════════════════════════════════════════════════════════════════════
def parse_pair(pair: dict) -> dict | None:
    try:
        if pair.get("chainId", "").lower() not in Config.ALLOWED_CHAINS:
            return None
        addr   = pair.get("baseToken", {}).get("address", "")
        symbol = pair.get("baseToken", {}).get("symbol", "?")
        name   = pair.get("baseToken", {}).get("name", "Unknown")
        price  = float(pair.get("priceUsd", 0) or 0)
        if not addr or price <= 0:
            return None

        liq        = float(pair.get("liquidity", {}).get("usd", 0) or 0)
        vol_5m     = float(pair.get("volume", {}).get("m5", 0) or 0)
        vol_1h     = float(pair.get("volume", {}).get("h1", 0) or 0)
        change_5m  = float(pair.get("priceChange", {}).get("m5", 0) or 0)
        change_1h  = float(pair.get("priceChange", {}).get("h1", 0) or 0)
        change_24h = float(pair.get("priceChange", {}).get("d1", 0) or 0)
        mcap       = float(pair.get("marketCapUsd", 0) or 0)
        txns       = pair.get("txns", {}).get("h1", {})
        buys       = int(txns.get("buys", 0))
        sells      = int(txns.get("sells", 1))

        state.cache_token(addr, name, symbol)

        token_age_hours = 0.0
        created_at = pair.get("pairCreatedAt", 0)
        if created_at:
            try:
                ts = created_at / 1000 if created_at > 1e10 else created_at
                token_age_hours = (datetime.now() - datetime.fromtimestamp(ts)).total_seconds() / 3600
            except Exception:
                pass

        return {
            "token_addr": addr, "symbol": symbol, "name": name,
            "price": price, "liq": liq, "vol_5m": vol_5m, "vol_1h": vol_1h,
            "change_5m": change_5m, "change_1h": change_1h, "change_24h": change_24h,
            "mcap": mcap, "buys": buys, "sells": sells,
            "buys_sells": buys / sells if sells > 0 else 0.0,
            "pair_addr": pair.get("pairAddress", ""),
            "token_age_hours": token_age_hours,
        }
    except Exception:
        return None


def score_signal(data: dict) -> float:
    if not data:
        return 0.0
    if not (Config.MIN_LIQUIDITY <= data["liq"] <= Config.MAX_LIQUIDITY):
        return 0.0
    if data["vol_1h"] < Config.MIN_VOLUME_1H:
        return 0.0
    if data["change_24h"] < 2.0:
        return 0.0
    if data["mcap"] and not (Config.MIN_MCAP <= data["mcap"] <= Config.MAX_MCAP):
        return 0.0
    if data["token_age_hours"] > Config.MAX_TOKEN_AGE_HOURS:
        return 0.0

    addr = data["token_addr"]
    quant.update(addr, data["vol_1h"], data["price"])

    rsi     = quant.rsi_momentum(addr)
    z       = quant.volume_zscore(addr, data["vol_1h"])
    atr     = quant.atr_volatility(addr)
    vol_acc = quant.volume_acceleration(addr)

    rsi_pts = QuantEngine.rsi_to_score(rsi)

    thr = Config.Z_SCORE_BREAKOUT_THRESHOLD
    if z >= thr:   z_pts = min(20.0, 8.0 + (z - thr) * 4.0)
    elif z > 0:    z_pts = z / thr * 8.0
    else:          z_pts = 0.0

    bp_pts  = QuantEngine.buy_pressure_index(
        data["buys"], data["sells"], data["change_5m"], data["change_1h"]
    )
    liq_pts = QuantEngine.liquidity_depth_score(data["vol_1h"], data["liq"])

    if atr > 0.35:    atr_pen = Config.ATR_VOLATILITY_PENALTY_MAX
    elif atr > 0.20:  atr_pen = Config.ATR_VOLATILITY_PENALTY_MAX * (atr - 0.20) / 0.15
    else:             atr_pen = 0.0

    age = data["token_age_hours"]
    mat_pts = 5.0 if 2 <= age <= 48 else (2.5 if 1 <= age < 2 else 0.0)
    acc_pts = max(0.0, vol_acc * 5.0)

    raw = rsi_pts + z_pts + bp_pts + liq_pts - atr_pen + mat_pts + acc_pts
    raw *= QuantEngine.timeframe_confluence(data["change_5m"], data["change_1h"], data["change_24h"])
    raw *= max(0.0, 1.0 - RugDetector.analyze(data) / 150.0)

    return round(max(0.0, min(100.0, raw)), 2)


def get_diag(addr: str, vol_1h: float) -> dict:
    return {
        "rsi":     quant.rsi_momentum(addr),
        "z":       quant.volume_zscore(addr, vol_1h),
        "atr":     quant.atr_volatility(addr),
        "vol_acc": quant.volume_acceleration(addr),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  💼 POSITION MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════
def open_position(data: dict) -> bool:
    with state.lock:
        if len(state.active_positions) >= Config.MAX_POSITIONS:
            return False
        if data["token_addr"] in state.active_positions:
            return False
        state.active_positions[data["token_addr"]] = Position(
            data["token_addr"], data["symbol"], data["name"], data["price"]
        )
    return True


def close_position(addr: str, reason: str, price: float):
    with state.lock:
        pos = state.active_positions.pop(addr, None)
    if not pos:
        return
    pnl = pos.pnl()
    state.trade_log.append({
        "symbol": pos.symbol, "name": pos.name,
        "entry": pos.entry_price, "exit": price, "pnl": pnl,
        "entry_time": pos.entry_time.isoformat(),
        "exit_time": datetime.now().isoformat(),
        "reason": reason,
    })
    state.save_log()
    state.stats["closed"] += 1
    color = Fore.GREEN if pnl > 0 else Fore.RED
    print(f"{color}[CLOSED]  {pos.symbol}  {reason}  PnL: {pnl:+.2f}%")
    send_telegram(f"📋 CLOSED: {pos.symbol}\nReason: {reason}\nPnL: {pnl:+.2f}%")


def update_positions(data: dict):
    addr  = data["token_addr"]
    price = data["price"]
    with state.lock:
        pos = state.active_positions.get(addr)
    if not pos:
        return
    pos.update(price)
    for level, _ in pos.check_tp(price):
        notify_tp(pos.symbol, level, price, pos.pnl())
        if level == "TP3" and Config.CLOSE_ON_TP3:
            close_position(addr, "TP3", price)
            return
    hit_sl, _ = pos.check_sl(price)
    if hit_sl:
        notify_sl(pos.symbol, price, pos.pnl())
        close_position(addr, "SL", price)
        return
    if pos.timed_out():
        close_position(addr, "Timeout", price)


# ══════════════════════════════════════════════════════════════════════════════
#  🔍 SCAN ENGINE
# ══════════════════════════════════════════════════════════════════════════════
def process_pair(pair: dict):
    try:
        data = parse_pair(pair)
        if not data:
            return

        update_positions(data)

        score    = score_signal(data)
        rug_risk = RugDetector.analyze(data)
        priority = score - (rug_risk * 0.3)
        diag     = get_diag(data["token_addr"], data["vol_1h"])

        if score >= Config.OPPORTUNITY_SCORE_THRESHOLD:
            tp1, tp2, tp3, sl = calc_levels(data["price"])
            with state.lock:
                state.opportunities[data["token_addr"]] = {
                    "symbol": data["symbol"], "name": data["name"],
                    "price": data["price"], "score": score, "rug_risk": rug_risk,
                    "priority": priority, "liq": data["liq"], "vol_1h": data["vol_1h"],
                    "buys_sells": data["buys_sells"],
                    **diag,
                    "tp1": tp1, "tp2": tp2, "tp3": tp3, "sl": sl,
                    "timestamp": datetime.now(),
                }

        if rug_risk > 70 and score > 40:
            notify_rug(data["symbol"], data["name"], rug_risk)

        addr = data["token_addr"]
        with state.lock:
            already_alerted = addr in state.alerted_tokens
            already_open    = addr in state.active_positions

        if (not already_alerted and not already_open
                and score >= Config.ENTRY_SCORE_THRESHOLD
                and rug_risk < Config.MAX_RUG_RISK_FOR_ENTRY
                and RugDetector.is_safe(data)):
            with state.lock:
                state.alerted_tokens[addr] = datetime.now()
            notify_entry(data, score, rug_risk, diag)
            if open_position(data):
                state.stats["signals"] += 1

    except Exception:
        pass


def scan_all(pairs: list):
    with ThreadPoolExecutor(max_workers=Config.MAX_WORKERS) as ex:
        list(ex.map(process_pair, pairs))


# ══════════════════════════════════════════════════════════════════════════════
#  📊 DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════
def print_dashboard():
    with state.lock:
        positions = dict(state.active_positions)
        opps      = dict(state.opportunities)

    if positions:
        print(Fore.LIGHTBLUE_EX + f"\n{'═'*110}")
        print(f"  📊 ACTIVE POSITIONS ({len(positions)}/{Config.MAX_POSITIONS})")
        print(f"{'═'*110}")
        for addr, pos in positions.items():
            pnl     = pos.pnl()
            color   = Fore.GREEN if pnl > 0 else Fore.RED
            elapsed = (datetime.now() - pos.entry_time).total_seconds() / 60
            tp1, tp2, tp3, sl = calc_levels(pos.entry_price)
            print(
                f"  {Fore.CYAN}{pos.symbol:<8} ({pos.name[:14]:<14})  "
                f"Entry: ${pos.entry_price:.8f}  Cur: ${pos.current_price:.8f}  "
                f"{color}PnL: {pnl:+.2f}%  "
                f"{Fore.WHITE}TP1: ${tp1:.8f}  SL: ${sl:.8f}  ⏱ {elapsed:.0f}m"
            )
        print(f"{Fore.LIGHTBLUE_EX}{'═'*110}\n")

    if opps and Config.SHOW_ALL_OPPORTUNITIES:
        sorted_opps = sorted(opps.items(), key=lambda x: x[1]["priority"], reverse=True)[:15]
        print(Fore.LIGHTYELLOW_EX + f"\n{'═'*170}")
        print(f"  💡 TOP OPPORTUNITIES  (Score | Rug% | RSI | Vol-Z | ATR% | Accel)")
        print(f"{'═'*170}")
        for addr, o in sorted_opps:
            sc    = Fore.GREEN if o["score"] > 60 else Fore.YELLOW if o["score"] > 40 else Fore.CYAN
            rc    = Fore.RED if o["rug_risk"] > 70 else Fore.YELLOW if o["rug_risk"] > 40 else Fore.GREEN
            rsi_c = Fore.GREEN if 55 <= o["rsi"] <= 75 else Fore.YELLOW
            z_c   = Fore.GREEN if o["z"] > 1.5 else Fore.WHITE
            print(
                f"  {Fore.CYAN}{o['symbol']:<10} ({o['name'][:12]:<12})  "
                f"{sc}Score:{o['score']:>6.1f}{Fore.WHITE}  "
                f"{rc}Rug:{o['rug_risk']:>5.0f}%{Fore.WHITE}  "
                f"{rsi_c}RSI:{o['rsi']:>5.1f}{Fore.WHITE}  "
                f"{z_c}Z:{o['z']:>+5.2f}{Fore.WHITE}  "
                f"ATR:{o['atr']*100:>4.1f}%  Acc:{o['vol_acc']:>+5.2f}  "
                f"{Fore.CYAN}${o['price']:.10f}  "
                f"TP1:${o['tp1']:.8f}  {Fore.RED}SL:${o['sl']:.8f}"
            )
        print(f"{Fore.LIGHTYELLOW_EX}{'═'*170}\n")


def print_stats():
    log = state.trade_log
    if not log:
        return
    total     = len(log)
    wins      = sum(1 for t in log if t["pnl"] > 0)
    total_pnl = sum(t["pnl"] for t in log)
    avg_pnl   = total_pnl / total if total else 0
    elapsed   = (datetime.now() - state.start_time).total_seconds() / 3600
    rate      = total / elapsed if elapsed > 0 else 0
    print(Fore.LIGHTBLUE_EX + f"\n{'═'*70}")
    print(f"  📈 SESSION STATS")
    print(f"{'═'*70}")
    print(f"  Trades: {total}  Wins: {wins} ({wins/total*100:.0f}%)  Losses: {total-wins}")
    print(f"  Total PnL: {total_pnl:+.2f}%  Avg PnL: {avg_pnl:+.2f}%  Rate: {rate:.1f}/hr")
    print(
        f"  Scans: {state.stats['total_scans']}  Entries: {state.stats['entries']}  "
        f"TP: {state.stats['tp_hits']}  SL: {state.stats['sl_hits']}  "
        f"Rugs blocked: {state.stats['rugs_blocked']}"
    )
    print(f"{Fore.LIGHTBLUE_EX}{'═'*70}\n")


# ══════════════════════════════════════════════════════════════════════════════
#  🚀 MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    print(Fore.LIGHTGREEN_EX + f"\n{'═'*100}")
    print(f"  🤖 MEMECOIN TRADING BOT v3 — QUANTITATIVE EDITION  |  Profile: {Config.PROFILE}")
    print(f"{'═'*100}")
    print(f"{Fore.CYAN}  Scoring: RSI · Volume Z-Score · Buy Pressure · Liquidity Depth · ATR Penalty")
    print(
        f"  Entry: {Config.ENTRY_SCORE_THRESHOLD}/100  "
        f"Max positions: {Config.MAX_POSITIONS}  "
        f"Max rug: {Config.MAX_RUG_RISK_FOR_ENTRY}%"
    )
    print(
        f"  TP: {Config.TP1_PERCENT}% / {Config.TP2_PERCENT}% / {Config.TP3_PERCENT}%  "
        f"SL: {Config.STOP_LOSS_PERCENT}%  "
        f"Timeout: {Config.MAX_TRADE_DURATION}min"
    )
    tg_status  = "✅ ON" if Config.ENABLE_TELEGRAM and "PASTE" not in Config.TELEGRAM_BOT_TOKEN else "⚠️ TOKEN NOT SET"
    snd_status = "✅ ON" if Config.ENABLE_SOUND and SOUND_AVAILABLE else "❌ OFF"
    print(f"  Telegram: {tg_status}  |  Sound: {snd_status}")
    print(f"{Fore.LIGHTGREEN_EX}{'═'*100}\n")

    queries    = ["trending", "new", "pump", "hot", "gainers"]
    scan_count = 0

    try:
        while True:
            try:
                all_pairs = []
                for q in queries:
                    all_pairs.extend(fetch_pairs(q))

                unique = list(
                    {p.get("pairAddress"): p for p in all_pairs if p.get("pairAddress")}.values()
                )[:Config.MAX_PAIRS_PER_SCAN]

                scan_all(unique)
                state.stats["total_scans"] += 1
                scan_count += 1

                if scan_count % Config.DASHBOARD_EVERY_N_SCANS == 0:
                    print_dashboard()

                if scan_count % 10 == 0:
                    state.cleanup()

                with state.lock:
                    active = len(state.active_positions)
                    opp_c  = len(state.opportunities)

                print(
                    f"{Fore.LIGHTBLACK_EX}[{datetime.now().strftime('%H:%M:%S')}]  "
                    f"Scan #{state.stats['total_scans']}  "
                    f"Pairs: {len(unique)}  "
                    f"Active: {active}/{Config.MAX_POSITIONS}  "
                    f"Opps: {opp_c}{Fore.RESET}"
                )

                time.sleep(Config.SCAN_INTERVAL)

            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"{Fore.RED}⚠️  Error: {e}")
                time.sleep(5)

    except KeyboardInterrupt:
        print(f"\n{Fore.RED}⛔  Bot stopped\n")
    finally:
        print_stats()


if __name__ == "__main__":
    main()