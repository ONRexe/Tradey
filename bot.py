"""
╔═══════════════════════════════════════╗
║        CRYPTOMIND TRADING BOT         ║
║   Paper Trading · 24/7 · Lernfähig   ║
╚═══════════════════════════════════════╝
"""

import asyncio
import json
import os
import time
import math
import logging
from datetime import datetime
from collections import deque
import websockets
import aiohttp

# ── Konfiguration ──────────────────────────────────────
INITIAL_CAPITAL    = 500.0        # Startkapital in €
MIN_CONFIDENCE     = 65           # Mindest-Konfidenz für Trade (%)
INVEST_FRACTION    = 0.25         # Max 25% des Kapitals pro Trade
CYCLE_SECONDS      = 300          # Analyse-Zyklus 5 Minuten
MIN_HOLD_SECONDS   = 900          # Mindest-Haltezeit 15 Minuten
MAX_POSITIONS      = 5            # Max gleichzeitige Positionen
TRADING_FEE        = 0.001         # Binance Fee 0.1% pro Trade
STOP_LOSS_PCT      = 0.03          # Stop-Loss bei 3% Verlust
TAKE_PROFIT_PCT    = 0.05          # Take-Profit bei 5% Gewinn
HISTORY_SIZE       = 100          # Preishistorie pro Paar
BRAIN_FILE         = "/data/brain.json" # Lern-Datei
PORTFOLIO_FILE     = "/data/portfolio.json"
LOG_FILE           = "/data/trading.log"

TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

PAIRS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "MATIC/USDT", "DOT/USDT",
    "LINK/USDT", "LTC/USDT", "UNI/USDT", "ATOM/USDT", "TRX/USDT",
    "ARB/USDT", "OP/USDT", "INJ/USDT", "SUI/USDT", "APT/USDT", "SEI/USDT"
]
SYMBOLS = {p: p.replace("/", "").lower() for p in PAIRS}

# ── Persistenter Speicher ─────────────────────────────────
import os
os.makedirs("/data", exist_ok=True)

# ── Logging ────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8")
    ]
)
log = logging.getLogger("CryptoMind")

# ── Indikatoren ────────────────────────────────────────
def calc_rsi(prices: list, period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0
    changes = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    recent = changes[-period:]
    gains = [c for c in recent if c > 0]
    losses = [abs(c) for c in recent if c < 0]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calc_ema(prices: list, period: int) -> list:
    if not prices:
        return []
    k = 2 / (period + 1)
    ema = [prices[0]]
    for p in prices[1:]:
        ema.append(p * k + ema[-1] * (1 - k))
    return ema

def calc_macd_histogram(prices: list) -> float:
    if len(prices) < 26:
        return 0.0
    ema12 = calc_ema(prices, 12)
    ema26 = calc_ema(prices, 26)
    macd_line = [ema12[i] - ema26[i] for i in range(len(ema26))]
    if len(macd_line) < 9:
        return 0.0
    signal = calc_ema(macd_line[-9:], 9)
    return macd_line[-1] - signal[-1]

# ── Neue Indikatoren ──────────────────────────────────────

def calc_bollinger(prices: list, period: int = 20) -> dict:
    """Bollinger Bänder – zeigt ob Preis über/unter normalem Bereich"""
    if len(prices) < period:
        return {"upper": 0, "lower": 0, "mid": 0, "pct": 0.5}
    recent = prices[-period:]
    mid = sum(recent) / period
    std = (sum((p - mid)**2 for p in recent) / period) ** 0.5
    upper = mid + 2 * std
    lower = mid - 2 * std
    price = prices[-1]
    pct = (price - lower) / (upper - lower) if upper != lower else 0.5
    return {"upper": upper, "lower": lower, "mid": mid, "pct": pct}

def calc_stochastic(prices: list, period: int = 14) -> float:
    """Stochastic Oscillator – wie RSI aber anders berechnet (0-100)"""
    if len(prices) < period:
        return 50.0
    recent = prices[-period:]
    low, high = min(recent), max(recent)
    if high == low:
        return 50.0
    return (prices[-1] - low) / (high - low) * 100

def calc_volume_trend(volumes: list) -> float:
    """Volumen-Trend – steigendes Volumen bestätigt Preisbewegung"""
    if len(volumes) < 5:
        return 1.0
    recent = volumes[-5:]
    avg = sum(recent) / len(recent)
    return recent[-1] / avg if avg > 0 else 1.0

def calc_trend_strength(prices: list, period: int = 10) -> float:
    """Trendstärke – wie stark und konsistent ist der aktuelle Trend"""
    if len(prices) < period:
        return 0.0
    recent = prices[-period:]
    ups = sum(1 for i in range(1, len(recent)) if recent[i] > recent[i-1])
    return (ups / (period - 1)) * 2 - 1  # -1 (starker Abwärtstrend) bis +1 (starker Aufwärtstrend)

def calc_price_position(prices: list, period: int = 20) -> float:
    """Wo steht der Preis im Vergleich zu den letzten N Kerzen (0=Tief, 1=Hoch)"""
    if len(prices) < period:
        return 0.5
    recent = prices[-period:]
    low, high = min(recent), max(recent)
    return (prices[-1] - low) / (high - low) if high != low else 0.5

# ── Trend-Erkennung ───────────────────────────────────────

def calc_ema_cross(prices: list) -> str:
    """EMA 9/21 Kreuzung – klassisches Trendsignal"""
    if len(prices) < 22:
        return "neutral"
    from collections import deque
    def ema(data, p):
        k = 2/(p+1)
        e = [data[0]]
        for v in data[1:]: e.append(v*k + e[-1]*(1-k))
        return e
    e9 = ema(prices, 9)
    e21 = ema(prices, 21)
    # Aktuelle Kreuzung
    if e9[-1] > e21[-1] and e9[-2] <= e21[-2]:
        return "cross_up"    # Gerade gekreuzt: bullisch
    if e9[-1] < e21[-1] and e9[-2] >= e21[-2]:
        return "cross_down"  # Gerade gekreuzt: bärisch
    if e9[-1] > e21[-1]:
        return "above"       # EMA9 über EMA21: Aufwärtstrend
    return "below"           # EMA9 unter EMA21: Abwärtstrend

def calc_higher_highs_lower_lows(prices: list, lookback: int = 10) -> str:
    """Erkennt Höhere Hochs / Tiefere Tiefs – Trendbestätigung"""
    if len(prices) < lookback * 2:
        return "neutral"
    first_half = prices[-lookback*2:-lookback]
    second_half = prices[-lookback:]
    high1, low1 = max(first_half), min(first_half)
    high2, low2 = max(second_half), min(second_half)
    if high2 > high1 and low2 > low1:
        return "higher_highs"   # Aufwärtstrend bestätigt
    if high2 < high1 and low2 < low1:
        return "lower_lows"     # Abwärtstrend bestätigt
    return "neutral"

def calc_breakout(prices: list, lookback: int = 20) -> str:
    """Erkennt Ausbrüche über Widerstand oder unter Support"""
    if len(prices) < lookback + 2:
        return "neutral"
    recent = prices[-(lookback+2):-2]  # historische Werte
    resistance = max(recent)
    support = min(recent)
    current = prices[-1]
    prev = prices[-2]
    range_size = resistance - support
    if range_size == 0:
        return "neutral"
    # Ausbruch nur wenn vorher innerhalb der Range
    if current > resistance * 1.005 and prev <= resistance:
        return "breakout_up"
    if current < support * 0.995 and prev >= support:
        return "breakout_down"
    # Konsolidierung: enge Range
    if range_size / prices[-1] < 0.01:
        return "consolidation"
    return "neutral"

def calc_reversal_candle(prices: list) -> str:
    """Erkennt Umkehrkerzen (Hammer / Shooting Star)"""
    if len(prices) < 5:
        return "neutral"
    # Simuliere Kerze aus den letzten 3 Preisen
    body = abs(prices[-1] - prices[-3])
    total_range = max(prices[-3:]) - min(prices[-3:])
    if total_range == 0:
        return "neutral"
    body_ratio = body / total_range
    # Starke Aufwärtsbewegung nach unten = Hammer (bullisch)
    if prices[-1] > prices[-3] and prices[-2] < min(prices[-3], prices[-1]) and body_ratio > 0.5:
        return "hammer"
    # Starke Abwärtsbewegung nach oben = Shooting Star (bärisch)
    if prices[-1] < prices[-3] and prices[-2] > max(prices[-3], prices[-1]) and body_ratio > 0.5:
        return "shooting_star"
    return "neutral"

def calc_trend_momentum(prices: list) -> float:
    """Kombiniert kurzfristiges und langfristiges Momentum"""
    if len(prices) < 20:
        return 0.0
    short_mom = (prices[-1] - prices[-5]) / prices[-5] * 100
    long_mom = (prices[-1] - prices[-20]) / prices[-20] * 100
    # Wenn beide in dieselbe Richtung: starkes Signal
    if short_mom > 0 and long_mom > 0:
        return (short_mom + long_mom) / 2
    if short_mom < 0 and long_mom < 0:
        return (short_mom + long_mom) / 2
    return 0.0  # Gegenläufig = kein klares Signal

# ── Brain (Lern-System) ────────────────────────────────
class Brain:
    DEFAULT_WEIGHTS = {
        # RSI
        "rsi_oversold": 1.0,
        "rsi_low": 1.0,
        "rsi_overbought": 1.0,
        # MACD
        "macd_positive": 1.0,
        "macd_negative": 1.0,
        # Momentum
        "momentum_up": 1.0,
        "momentum_down": 1.0,
        # Bollinger
        "bb_oversold": 1.0,      # Preis unter unterem Band
        "bb_overbought": 1.0,    # Preis über oberem Band
        "bb_mid_cross": 1.0,     # Preis kreuzt Mittellinie
        # Stochastic
        "stoch_oversold": 1.0,   # Stoch < 20
        "stoch_overbought": 1.0, # Stoch > 80
        # Trend
        "trend_strong_up": 1.0,  # Starker Aufwärtstrend
        "trend_strong_down": 1.0,# Starker Abwärtstrend
        # Volumen
        "volume_spike": 1.0,     # Volumen-Spike bestätigt Signal
        # Exit-Optimierung
        "hold_short": 1.0,       # Kurze Haltezeit war profitabel
        "hold_long": 1.0,        # Lange Haltezeit war profitabel
        # Trend-Erkennung
        "trend_ema_cross_up": 1.0,
        "trend_ema_cross_down": 1.0,
        "trend_higher_highs": 1.0,
        "trend_lower_lows": 1.0,
        "trend_breakout_up": 1.0,
        "trend_breakout_down": 1.0,
        "trend_reversal_bull": 1.0,
        "trend_reversal_bear": 1.0,
        "trend_consolidation": 1.0,
        "trend_momentum_confirm": 1.0,
    }

    def __init__(self):
        self.weights = self.DEFAULT_WEIGHTS.copy()
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.total_pnl = 0.0
        self.history = []
        self.load()

    def load(self):
        try:
            if os.path.exists(BRAIN_FILE):
                with open(BRAIN_FILE, "r") as f:
                    data = json.load(f)
                self.weights = data.get("weights", self.DEFAULT_WEIGHTS.copy())
                self.total_trades = data.get("total_trades", 0)
                self.wins = data.get("wins", 0)
                self.losses = data.get("losses", 0)
                self.total_pnl = data.get("total_pnl", 0.0)
                self.history = data.get("history", [])
                log.info(f"🧠 Brain geladen: {self.total_trades} Trades, Winrate: {self.win_rate:.1f}%")
        except Exception as e:
            log.warning(f"Brain laden fehlgeschlagen: {e}")

    def save(self):
        try:
            with open(BRAIN_FILE, "w") as f:
                json.dump({
                    "weights": self.weights,
                    "total_trades": self.total_trades,
                    "wins": self.wins,
                    "losses": self.losses,
                    "total_pnl": self.total_pnl,
                    "history": self.history[-200:]
                }, f, indent=2)
        except Exception as e:
            log.warning(f"Brain speichern fehlgeschlagen: {e}")

    def learn(self, indicators: dict, pnl: float, pair: str, entry: float, exit_price: float, hold_seconds: float = 0):
        won = pnl > 0
        delta = 0.05 if won else -0.04
        for key in indicators:
            if key in self.weights:
                self.weights[key] = max(0.1, min(3.0, self.weights[key] + delta))

        # Lerne optimale Haltezeit
        if hold_seconds > 0:
            if won:
                if hold_seconds < 90:
                    # Kurze Haltezeit war profitabel
                    self.weights["hold_short"] = min(3.0, self.weights.get("hold_short", 1.0) + 0.05)
                else:
                    # Lange Haltezeit war profitabel
                    self.weights["hold_long"] = min(3.0, self.weights.get("hold_long", 1.0) + 0.05)
            else:
                if hold_seconds < 90:
                    self.weights["hold_short"] = max(0.1, self.weights.get("hold_short", 1.0) - 0.04)
                else:
                    self.weights["hold_long"] = max(0.1, self.weights.get("hold_long", 1.0) - 0.04)

        # Schutz: Wenn alle Gewichte zu niedrig → auf 0.5 zurücksetzen
        avg_weight = sum(self.weights.values()) / len(self.weights)
        if avg_weight < 0.3:
            log.warning("⚠️ Brain-Gewichte zu niedrig – teilweiser Reset auf 0.5")
            for key in self.weights:
                self.weights[key] = max(self.weights[key], 0.5)
        self.total_trades += 1
        if won:
            self.wins += 1
        else:
            self.losses += 1
        self.total_pnl += pnl
        self.history.append({
            "pair": pair, "pnl": round(pnl, 4), "won": won,
            "entry": entry, "exit": exit_price,
            "time": datetime.now().strftime("%H:%M:%S %d.%m")
        })
        self.save()
        log.info(f"🧠 Gelernt | {'✅ Gewinn' if won else '❌ Verlust'} | Winrate: {self.win_rate:.1f}% | PnL: €{pnl:+.2f}")

    @property
    def win_rate(self):
        return (self.wins / self.total_trades * 100) if self.total_trades > 0 else 0.0

    def top_weights(self):
        return sorted(self.weights.items(), key=lambda x: x[1], reverse=True)[:3]

# ── Portfolio ──────────────────────────────────────────
class Portfolio:
    def __init__(self):
        self.cash = INITIAL_CAPITAL
        self.positions = {}   # pair -> {amount, avg_price, entry_time, indicators}
        self.trades = []
        self.load()

    def load(self):
        self._session_trades = []  # immer initialisieren
        try:
            if os.path.exists(PORTFOLIO_FILE):
                with open(PORTFOLIO_FILE, "r") as f:
                    data = json.load(f)
                self.cash = data.get("cash", INITIAL_CAPITAL)
                self.positions = data.get("positions", {})
                self.trades = data.get("trades", [])
                self.total_fees = data.get("total_fees", 0.0)
                # Stelle sicher dass alle Positionen eine entry_time haben
                for pair, pos in self.positions.items():
                    if "entry_time" not in pos:
                        pos["entry_time"] = time.time() - MIN_HOLD_SECONDS  # sofort handelbar
                log.info(f"💼 Portfolio geladen: €{self.cash:.2f} Cash, {len(self.positions)} Positionen")
        except Exception as e:
            log.warning(f"Portfolio laden fehlgeschlagen: {e}")

    def save(self):
        try:
            with open(PORTFOLIO_FILE, "w") as f:
                json.dump({
                    "cash": self.cash,
                    "positions": self.positions,
                    "trades": self.trades[-500:],
                    "total_fees": getattr(self, "total_fees", 0.0)
                }, f, indent=2)
        except Exception as e:
            log.warning(f"Portfolio speichern fehlgeschlagen: {e}")

    def total_value(self, prices: dict) -> float:
        value = self.cash
        for pair, pos in self.positions.items():
            if pair in prices:
                value += pos["amount"] * prices[pair]["current"]
        return value

    def buy(self, pair: str, price: float, confidence: int, reason: str, indicators: dict):
        invest = self.cash * min((confidence / 100) * INVEST_FRACTION, INVEST_FRACTION)
        if invest < 1.0 or self.cash < invest:
            return False, 0
        fee = invest * TRADING_FEE
        units = (invest - fee) / price  # Fee reduziert gekaufte Menge
        self.cash -= invest
        self.total_fees = getattr(self, "total_fees", 0.0) + fee
        if pair in self.positions:
            pos = self.positions[pair]
            total_units = pos["amount"] + units
            pos["avg_price"] = (pos["avg_price"] * pos["amount"] + price * units) / total_units
            pos["amount"] = total_units
        else:
            self.positions[pair] = {
                "amount": units,
                "avg_price": price,
                "entry_time": time.time(),
                "indicators": indicators
            }
        self.trades.append({
            "pair": pair, "type": "BUY", "price": price,
            "amount": units, "value": invest,
            "confidence": confidence, "reason": reason,
            "time": datetime.now().strftime("%H:%M:%S")
        })
        self.save()
        return True, invest

    def sell(self, pair: str, price: float, confidence: int, reason: str, force: bool = False):
        if pair not in self.positions:
            return False, 0, 0, {}
        pos = self.positions[pair]
        hold_time = time.time() - pos.get("entry_time", 0)
        # Stop-Loss & Take-Profit bypass hold time check
        if not force and hold_time < MIN_HOLD_SECONDS:
            remaining = int(MIN_HOLD_SECONDS - hold_time)
            log.debug(f"⏳ {pair}: Mindest-Haltezeit nicht erreicht ({remaining}s verbleibend)")
            return False, 0, 0, {}
        fee = pos["amount"] * price * TRADING_FEE
        sale_value = pos["amount"] * price - fee  # Fee abgezogen
        cost_basis = pos["amount"] * pos["avg_price"]
        pnl = sale_value - cost_basis  # echter Gewinn nach Fees
        indicators = pos.get("indicators", {})
        self.total_fees = getattr(self, "total_fees", 0.0) + fee
        self.cash += sale_value
        trade_entry = {
            "pair": pair, "type": "SELL", "price": price,
            "amount": pos["amount"], "value": sale_value,
            "pnl": round(pnl, 4), "confidence": confidence,
            "reason": reason, "time": datetime.now().strftime("%H:%M:%S")
        }
        self.trades.append(trade_entry)
        self._session_trades.append(trade_entry)
        del self.positions[pair]
        self.save()
        return True, sale_value, pnl, indicators

    def check_stop_take(self, pair: str, current_price: float):
        """Prüft Stop-Loss und Take-Profit für eine Position"""
        if pair not in self.positions:
            return None, None
        pos = self.positions[pair]
        avg = pos["avg_price"]
        change_pct = (current_price - avg) / avg
        if change_pct <= -STOP_LOSS_PCT:
            return "STOP_LOSS", change_pct
        if change_pct >= TAKE_PROFIT_PCT:
            return "TAKE_PROFIT", change_pct
        return None, None

# ── Analyse Engine ─────────────────────────────────────
def analyze(pair: str, prices_data: dict, history: list, portfolio: Portfolio, brain: Brain) -> dict:
    p = prices_data.get(pair)
    if not p or len(history) < 3:
        return None

    price = p["current"]
    change = p.get("change", 0)
    has_pos = pair in portfolio.positions
    w = brain.weights

    score = 0.0
    reasons = []
    indicators = {}

    rsi = calc_rsi(history)
    macd_hist = calc_macd_histogram(history)
    bb = calc_bollinger(history)
    stoch = calc_stochastic(history)
    trend = calc_trend_strength(history)
    price_pos = calc_price_position(history)
    vol_data = prices_data.get(pair, {})
    vol_trend = calc_volume_trend([vol_data.get("volume", 1)] * 5)

    # ── RSI ──
    if rsi < 30:
        score += 35 * w.get("rsi_oversold", 1.0)
        reasons.append("RSI stark überverkauft")
        indicators["rsi_oversold"] = True
    elif rsi < 40:
        score += 20 * w.get("rsi_oversold", 1.0)
        reasons.append("RSI überverkauft")
        indicators["rsi_oversold"] = True
    elif rsi < 50:
        score += 10 * w.get("rsi_low", 1.0)
        reasons.append("RSI niedrig")
        indicators["rsi_low"] = True
    elif rsi > 70:
        score -= 35 * w.get("rsi_overbought", 1.0)
        reasons.append("RSI stark überkauft")
        indicators["rsi_overbought"] = True
    elif rsi > 60:
        score -= 20 * w.get("rsi_overbought", 1.0)
        reasons.append("RSI überkauft")
        indicators["rsi_overbought"] = True

    # ── MACD ──
    if macd_hist > 0:
        score += 20 * w.get("macd_positive", 1.0)
        reasons.append("MACD positiv")
        indicators["macd_positive"] = True
    else:
        score -= 20 * w.get("macd_negative", 1.0)
        reasons.append("MACD negativ")
        indicators["macd_negative"] = True

    # ── Bollinger Bänder ──
    if bb["pct"] < 0.1:
        score += 25 * w.get("bb_oversold", 1.0)
        reasons.append("Bollinger überverkauft")
        indicators["bb_oversold"] = True
    elif bb["pct"] > 0.9:
        score -= 25 * w.get("bb_overbought", 1.0)
        reasons.append("Bollinger überkauft")
        indicators["bb_overbought"] = True
    elif 0.45 < bb["pct"] < 0.55:
        score += 5 * w.get("bb_mid_cross", 1.0)
        indicators["bb_mid_cross"] = True

    # ── Stochastic ──
    if stoch < 20:
        score += 20 * w.get("stoch_oversold", 1.0)
        reasons.append("Stoch überverkauft")
        indicators["stoch_oversold"] = True
    elif stoch > 80:
        score -= 20 * w.get("stoch_overbought", 1.0)
        reasons.append("Stoch überkauft")
        indicators["stoch_overbought"] = True

    # ── Trendstärke ──
    if trend > 0.6:
        score += 15 * w.get("trend_strong_up", 1.0)
        reasons.append("Starker Aufwärtstrend")
        indicators["trend_strong_up"] = True
    elif trend < -0.6:
        score -= 15 * w.get("trend_strong_down", 1.0)
        reasons.append("Starker Abwärtstrend")
        indicators["trend_strong_down"] = True

    # ── Momentum (kurzfristig) ──
    if len(history) >= 5:
        momentum = (history[-1] - history[-5]) / history[-5] * 100
        if momentum > 0.3:
            score += 12 * w.get("momentum_up", 1.0)
            indicators["momentum_up"] = True
        elif momentum < -0.3:
            score -= 12 * w.get("momentum_down", 1.0)
            indicators["momentum_down"] = True

    # ── Volumen bestätigt Signal ──
    if vol_trend > 1.5:
        score *= 1.1  # 10% Boost wenn Volumen hoch
        indicators["volume_spike"] = True

    # ── Trend-Erkennung ──
    ema_cross = calc_ema_cross(history)
    hh_ll = calc_higher_highs_lower_lows(history)
    breakout = calc_breakout(history)
    reversal = calc_reversal_candle(history)
    trend_mom = calc_trend_momentum(history)

    # EMA Kreuzung
    if ema_cross == "cross_up":
        score += 30 * w.get("trend_ema_cross_up", 1.0)
        reasons.append("EMA Golden Cross")
        indicators["trend_ema_cross_up"] = True
    elif ema_cross == "cross_down":
        score -= 30 * w.get("trend_ema_cross_down", 1.0)
        reasons.append("EMA Death Cross")
        indicators["trend_ema_cross_down"] = True
    elif ema_cross == "above":
        score += 10 * w.get("trend_ema_cross_up", 1.0)
    elif ema_cross == "below":
        score -= 10 * w.get("trend_ema_cross_down", 1.0)

    # Höhere Hochs / Tiefere Tiefs
    if hh_ll == "higher_highs":
        score += 20 * w.get("trend_higher_highs", 1.0)
        reasons.append("Höhere Hochs")
        indicators["trend_higher_highs"] = True
    elif hh_ll == "lower_lows":
        score -= 20 * w.get("trend_lower_lows", 1.0)
        reasons.append("Tiefere Tiefs")
        indicators["trend_lower_lows"] = True

    # Ausbrüche
    if breakout == "breakout_up":
        score += 35 * w.get("trend_breakout_up", 1.0)
        reasons.append("Ausbruch nach oben!")
        indicators["trend_breakout_up"] = True
    elif breakout == "breakout_down":
        score -= 35 * w.get("trend_breakout_down", 1.0)
        reasons.append("Ausbruch nach unten!")
        indicators["trend_breakout_down"] = True
    elif breakout == "consolidation":
        score *= w.get("trend_consolidation", 1.0) * 0.5  # Signal abschwächen
        indicators["trend_consolidation"] = True

    # Umkehrkerzen
    if reversal == "hammer":
        score += 25 * w.get("trend_reversal_bull", 1.0)
        reasons.append("Hammer-Kerze")
        indicators["trend_reversal_bull"] = True
    elif reversal == "shooting_star":
        score -= 25 * w.get("trend_reversal_bear", 1.0)
        reasons.append("Shooting Star")
        indicators["trend_reversal_bear"] = True

    # Trend-Momentum Bestätigung
    if abs(trend_mom) > 1.0:
        boost = min(abs(trend_mom) * 2, 20)
        if trend_mom > 0:
            score += boost * w.get("trend_momentum_confirm", 1.0)
            indicators["trend_momentum_confirm"] = True
        else:
            score -= boost * w.get("trend_momentum_confirm", 1.0)

    # ── 24h Change ──
    if change > 3:
        score += 10
    elif change < -3:
        score -= 10

    # Position management
    if has_pos and score > 10:
        score -= 15
    if not has_pos and score < -10:
        score = min(score, -5)

    # Signal
    if score >= 10 and not has_pos:
        signal = "BUY"
        confidence = min(51 + int(score / 2), 95)
    elif score <= -10 and has_pos:
        signal = "SELL"
        confidence = min(51 + int(abs(score) / 2), 95)
    elif score >= 6 and not has_pos:
        signal = "BUY"
        confidence = 53 + int(score)
    else:
        signal = "HOLD"
        confidence = 40

    reason = " + ".join(reasons[:2]) if reasons else "Kein Signal"

    return {
        "signal": signal,
        "confidence": confidence,
        "reason": reason[:60],
        "rsi": round(rsi, 1),
        "macd": round(macd_hist, 6),
        "indicators": indicators,
        "price": price
    }

# ── Telegram ───────────────────────────────────────────
async def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        async with aiohttp.ClientSession() as session:
            await session.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "HTML"
            })
    except Exception as e:
        log.warning(f"Telegram Fehler: {e}")

async def send_brain_backup(brain: 'Brain', portfolio: 'Portfolio'):
    """Sendet brain.json und portfolio.json als Datei an Telegram"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        import io
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
        brain_data = json.dumps({
            "weights": brain.weights,
            "total_trades": brain.total_trades,
            "wins": brain.wins,
            "losses": brain.losses,
            "total_pnl": brain.total_pnl,
            "history": brain.history[-200:]
        }, indent=2).encode("utf-8")
        async with aiohttp.ClientSession() as session:
            form = aiohttp.FormData()
            form.add_field("chat_id", TELEGRAM_CHAT_ID)
            form.add_field("caption",
                f"🧠 Brain Backup\nTrades: {brain.total_trades} | Winrate: {brain.win_rate:.1f}%\nPnL gesamt: €{brain.total_pnl:+.2f}\nCash: €{portfolio.cash:.2f}",
                content_type="text/plain")
            form.add_field("parse_mode", "HTML")
            form.add_field("document",
                io.BytesIO(brain_data),
                filename="cryptomind_brain.json",
                content_type="application/json")
            await session.post(url, data=form)
        log.info("🧠 Brain Backup an Telegram gesendet")
    except Exception as e:
        log.warning(f"Brain Backup Fehler: {e}")

# ── Hauptbot ───────────────────────────────────────────
class CryptoMindBot:
    def __init__(self):
        self.prices = {}
        self.history = {pair: deque(maxlen=HISTORY_SIZE) for pair in PAIRS}
        self.portfolio = Portfolio()
        self.brain = Brain()
        self.running = False

    async def connect_binance(self):
        streams = "/".join([f"{SYMBOLS[p]}@ticker" for p in PAIRS])
        url = f"wss://stream.binance.com:9443/stream?streams={streams}"
        log.info("🔌 Verbinde mit Binance WebSocket...")
        while self.running:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    log.info("✅ Binance verbunden – empfange Echtzeit-Preise")
                    await send_telegram("🟢 <b>CryptoMind Bot gestartet</b>\nVerbunden mit Binance · Paper Trading · €500 Startkapital")
                    async for msg in ws:
                        if not self.running:
                            break
                        try:
                            data = json.loads(msg)
                            d = data.get("data", {})
                            if not d.get("s"):
                                continue
                            pair = next((p for p in PAIRS if SYMBOLS[p] == d["s"].lower()), None)
                            if not pair:
                                continue
                            price = float(d["c"])
                            if math.isnan(price):
                                continue
                            self.prices[pair] = {
                                "current": price,
                                "open": float(d.get("o", price)),
                                "high": float(d.get("h", price)),
                                "low": float(d.get("l", price)),
                                "volume": float(d.get("v", 0)),
                                "change": float(d.get("P", 0))
                            }
                            self.history[pair].append(price)
                        except Exception:
                            pass
            except Exception as e:
                log.warning(f"WebSocket Fehler: {e} – reconnect in 5s")
                await asyncio.sleep(5)

    async def trading_loop(self):
        log.info(f"🤖 Trading-Loop gestartet | Zyklus: {CYCLE_SECONDS}s | Min. Konfidenz: {MIN_CONFIDENCE}% | Mindest-Haltezeit: {MIN_HOLD_SECONDS}s")
        await asyncio.sleep(5)  # Warte auf erste Preise

        while self.running:
            cycle_start = time.time()

            for pair in PAIRS:
                if not self.running:
                    break
                history = list(self.history[pair])
                if len(history) < 3:
                    continue

                result = analyze(pair, self.prices, history, self.portfolio, self.brain)
                if not result:
                    continue

                sig = result["signal"]
                conf = result["confidence"]
                reason = result["reason"]
                price = result["price"]

                # ── Stop-Loss / Take-Profit Check ──
                sl_trigger, sl_pct = self.portfolio.check_stop_take(pair, price)
                if sl_trigger:
                    sl_reason = f"🛑 Stop-Loss {sl_pct*100:.1f}%" if sl_trigger == "STOP_LOSS" else f"🎯 Take-Profit +{sl_pct*100:.1f}%"
                    success, value, pnl, indicators = self.portfolio.sell(pair, price, 100, sl_reason, force=True)
                    if success:
                        # Brain lernt aus Stop-Loss/Take-Profit mit extra Gewicht
                        sl_hold = time.time() - self.portfolio.trades[-1].get("entry_time", time.time()) if self.portfolio.trades else 0
                        self.brain.learn(indicators, pnl, pair, self.portfolio.trades[-1]["price"] if self.portfolio.trades else price, price, sl_hold)
                        # Stop-Loss extra bestrafen / Take-Profit extra belohnen
                        if sl_trigger == "STOP_LOSS":
                            for key in indicators:
                                if key in self.brain.weights:
                                    self.brain.weights[key] = max(0.1, self.brain.weights[key] - 0.08)
                            self.brain.save()
                            emoji = "🛑"
                            log.info(f"🛑 STOP {pair:<12} | €{value:.2f} | PnL: {pnl:+.2f}€ | {sl_pct*100:.1f}% Verlust")
                            await send_telegram(f"🛑 <b>STOP-LOSS {pair}</b>\n💰 €{value:.2f} @ ${price:.4f}\n❌ PnL: €{pnl:+.2f} ({sl_pct*100:.1f}%)\n🧠 Brain lernt daraus")
                        else:
                            for key in indicators:
                                if key in self.brain.weights:
                                    self.brain.weights[key] = min(3.0, self.brain.weights[key] + 0.08)
                            self.brain.save()
                            log.info(f"🎯 TAKE {pair:<12} | €{value:.2f} | PnL: {pnl:+.2f}€ | +{sl_pct*100:.1f}% Gewinn")
                            await send_telegram(f"🎯 <b>TAKE-PROFIT {pair}</b>\n💰 €{value:.2f} @ ${price:.4f}\n✅ PnL: €{pnl:+.2f} (+{sl_pct*100:.1f}%)\n🧠 Brain lernt daraus")
                    continue

                # ── Normales Signal ──
                if conf >= MIN_CONFIDENCE and sig == "BUY":
                    if len(self.portfolio.positions) >= MAX_POSITIONS:
                        continue
                    success, invested = self.portfolio.buy(pair, price, conf, reason, result["indicators"])
                    if success:
                        log.info(f"🟢 BUY  {pair:<12} | €{invested:.2f} @ ${price:.4f} | {conf}% | {reason}")
                        await send_telegram(f"🟢 <b>GEKAUFT {pair}</b>\n💰 €{invested:.2f} @ ${price:.4f}\n📊 {reason} | {conf}% Konfidenz\n🛑 Stop: -{STOP_LOSS_PCT*100:.0f}% | 🎯 Profit: +{TAKE_PROFIT_PCT*100:.0f}%")

                elif conf >= MIN_CONFIDENCE and sig == "SELL":
                    pos = self.portfolio.positions.get(pair)
                    hold_secs = time.time() - pos.get("entry_time", time.time()) if pos else 0
                    success, value, pnl, indicators = self.portfolio.sell(pair, price, conf, reason)
                    if success:
                        self.brain.learn(indicators, pnl, pair, self.prices[pair]["current"], price, hold_secs)
                        emoji = "✅" if pnl >= 0 else "❌"
                        log.info(f"🔴 SELL {pair:<12} | €{value:.2f} | PnL: {pnl:+.2f}€ | {conf}% | {reason} | Haltezeit: {int(hold_secs)}s")
                        await send_telegram(f"🔴 <b>VERKAUFT {pair}</b>\n💰 €{value:.2f} @ ${price:.4f}\n{emoji} PnL: €{pnl:+.2f}\n📊 {reason} | {int(hold_secs)}s gehalten")

            # Status alle 60 Zyklen loggen
            if hasattr(self, '_cycle_count'):
                self._cycle_count += 1
            else:
                self._cycle_count = 0

            if self._cycle_count % 60 == 0:
                total = self.portfolio.total_value(self.prices)
                pnl = total - INITIAL_CAPITAL
                log.info(f"📊 Status | Kapital: €{total:.2f} | PnL: {pnl:+.2f}€ | Cash: €{self.portfolio.cash:.2f} | Positionen: {len(self.portfolio.positions)} | Winrate: {self.brain.win_rate:.1f}%")

            elapsed = time.time() - cycle_start
            await asyncio.sleep(max(0, CYCLE_SECONDS - elapsed))

    async def status_report(self):
        """Sendet jede Stunde einen kompakten Status-Report via Telegram"""
        while self.running:
            await asyncio.sleep(60 * 60)
            if not self.running:
                break
            total = self.portfolio.total_value(self.prices)
            start_capital = self.portfolio.trades[0]["value"] if self.portfolio.trades else INITIAL_CAPITAL
            pnl = total - INITIAL_CAPITAL
            pnl_pct = (pnl / INITIAL_CAPITAL) * 100

            # Bester und schlechtester Trade der letzten 15 Minuten
            recent = self.portfolio._session_trades[-50:]  # max letzte 50
            self.portfolio._session_trades = []  # reset für nächsten Zyklus

            best = max(recent, key=lambda t: t["pnl"], default=None)
            worst = min(recent, key=lambda t: t["pnl"], default=None)

            best_txt = f"👍 <b>Bester Trade:</b> {best['pair']} {'+' if best['pnl']>=0 else ''}€{best['pnl']:.2f}" if best else "👍 Kein Trade"
            worst_txt = f"👎 <b>Schlechtester:</b> {worst['pair']} {'+' if worst['pnl']>=0 else ''}€{worst['pnl']:.2f}" if worst else "👎 Kein Trade"

            total_fees = getattr(self.portfolio, "total_fees", 0.0)
            msg = (
                f"⚠️ <b>CryptoMind Status-Report</b>\n\n"
                f"💲 <b>Kapital:</b> €{total:.2f}\n"
                f"{'📈' if pnl>=0 else '📉'} <b>PnL gesamt:</b> €{pnl:+.2f} ({pnl_pct:+.1f}%)\n"
                f"💸 <b>Fees bezahlt:</b> €{total_fees:.2f}\n"
                f"📊 <b>Winrate:</b> {self.brain.win_rate:.1f}% ({self.brain.total_trades} Trades)\n\n"
                f"{best_txt}\n"
                f"{worst_txt}"
            )
            await send_telegram(msg)

    async def brain_backup_loop(self):
        """Sendet alle 2 Stunden die Brain-Datei via Telegram"""
        while self.running:
            await asyncio.sleep(2 * 3600)
            if not self.running:
                break
            await send_brain_backup(self.brain, self.portfolio)

    async def telegram_commands(self):
        """Lauscht auf Telegram-Befehle: /brain zum Brain-Import"""
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            return
        last_update_id = None
        log.info("📱 Telegram-Befehlsempfänger aktiv")
        while self.running:
            try:
                url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
                params = {"timeout": 30, "allowed_updates": ["message"]}
                if last_update_id:
                    params["offset"] = last_update_id + 1
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=35)) as resp:
                        data = await resp.json()
                for update in data.get("result", []):
                    last_update_id = update["update_id"]
                    msg = update.get("message", {})
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    if chat_id != TELEGRAM_CHAT_ID:
                        continue
                    text = msg.get("text", "")
                    document = msg.get("document", {})

                    # /help Befehl
                    if text == "/help":
                        await send_telegram(
                            "🤖 <b>CryptoMind Bot – Befehle</b>\n\n"
                            "📊 <b>/status</b>\n"
                            "  → Aktuellen Stand sofort abrufen\n\n"
                            "🧠 <b>/brain</b>\n"
                            "  → Brain-Datei jetzt senden\n\n"
                            "📁 <b>Datei schicken (.json)</b>\n"
                            "  → Brain importieren\n\n"
                            "🔄 <b>/reset</b>\n"
                            "  → Brain-Gewichtungen zurücksetzen\n\n"
                            "📉 <b>/resetstats</b>\n"
                            "  → Winrate & Trades auf 0 setzen\n"
                            "  (Gewichtungen bleiben erhalten)\n\n"
                            "ℹ️ Status-Report: stündlich automatisch\n"
                            "ℹ️ Brain-Backup: alle 2 Stunden automatisch"
                        )

                    # /status Befehl
                    elif text == "/status":
                        total = self.portfolio.total_value(self.prices)
                        pnl = total - INITIAL_CAPITAL
                        fees = getattr(self.portfolio, "total_fees", 0.0)
                        await send_telegram(
                            f"📊 <b>Status auf Anfrage</b>\n"
                            f"💲 Kapital: €{total:.2f}\n"
                            f"{'📈' if pnl>=0 else '📉'} PnL: €{pnl:+.2f}\n"
                            f"💸 Fees: €{fees:.2f}\n"
                            f"🧠 Winrate: {self.brain.win_rate:.1f}% ({self.brain.total_trades} Trades)\n"
                            f"📌 Positionen: {len(self.portfolio.positions)}"
                        )

                    # /brain Befehl – sendet aktuelle Brain-Datei
                    elif text == "/brain":
                        await send_brain_backup(self.brain, self.portfolio)

                    # /reset Befehl – Brain-Gewichtungen zurücksetzen
                    elif text == "/reset":
                        self.brain.weights = self.brain.DEFAULT_WEIGHTS.copy()
                        self.brain.save()
                        await send_telegram("🔄 Brain-Gewichtungen zurückgesetzt!")
                        log.info("🔄 Brain reset via Telegram")

                    # /resetstats Befehl – Winrate & Trades auf 0
                    elif text == "/resetstats":
                        self.brain.total_trades = 0
                        self.brain.wins = 0
                        self.brain.losses = 0
                        self.brain.total_pnl = 0.0
                        self.brain.history = []
                        self.brain.save()
                        await send_telegram(
                            "🔄 <b>Trade-Statistiken zurückgesetzt!</b>\n"
                            "📊 Winrate: 0%\n"
                            "🏆 Wins: 0 | Losses: 0\n"
                            "💡 Brain-Gewichtungen bleiben erhalten"
                        )
                        log.info("🔄 Trade stats reset via Telegram")

                    # Brain JSON Datei empfangen → importieren
                    elif document.get("file_name", "").endswith(".json"):
                        file_id = document["file_id"]
                        try:
                            async with aiohttp.ClientSession() as session:
                                # Datei-URL abrufen
                                r = await session.get(
                                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile",
                                    params={"file_id": file_id}
                                )
                                file_data = await r.json()
                                file_path = file_data["result"]["file_path"]
                                # Datei herunterladen
                                r2 = await session.get(
                                    f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
                                )
                                raw = await r2.read()
                            brain_json = json.loads(raw)
                            if "weights" in brain_json and "total_trades" in brain_json:
                                # Neue Gewichtungen übernehmen
                                self.brain.weights = {**self.brain.DEFAULT_WEIGHTS, **brain_json["weights"]}
                                self.brain.total_trades = brain_json.get("total_trades", 0)
                                self.brain.wins = brain_json.get("wins", 0)
                                self.brain.losses = brain_json.get("losses", 0)
                                self.brain.total_pnl = brain_json.get("total_pnl", 0.0)
                                self.brain.history = brain_json.get("history", [])
                                self.brain.save()
                                await send_telegram(
                                    f"✅ <b>Brain erfolgreich importiert!</b>\n"
                                    f"🧠 {self.brain.total_trades} Trades geladen\n"
                                    f"📊 Winrate: {self.brain.win_rate:.1f}%"
                                )
                                log.info(f"🧠 Brain importiert via Telegram: {self.brain.total_trades} Trades")
                            else:
                                await send_telegram("❌ Ungültige Brain-Datei!")
                        except Exception as e:
                            await send_telegram(f"❌ Import fehlgeschlagen: {e}")
                            log.error(f"Brain import error: {e}")

            except Exception as e:
                log.warning(f"Telegram commands error: {e}")
                await asyncio.sleep(5)

    async def run(self):
        self.running = True
        log.info("═" * 50)
        log.info("   CRYPTOMIND TRADING BOT  gestartet")
        log.info(f"   Kapital: €{INITIAL_CAPITAL} | Paare: {len(PAIRS)}")
        log.info("═" * 50)
        await asyncio.gather(
            self.connect_binance(),
            self.trading_loop(),
            self.status_report(),
            self.brain_backup_loop(),
            self.telegram_commands()
        )

# ── Start ──────────────────────────────────────────────
if __name__ == "__main__":
    bot = CryptoMindBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        log.info("Bot gestoppt")
