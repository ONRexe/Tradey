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
MIN_CONFIDENCE     = 51           # Mindest-Konfidenz für Trade (%)
INVEST_FRACTION    = 0.25         # Max 25% des Kapitals pro Trade
CYCLE_SECONDS      = 5            # Analyse-Zyklus in Sekunden
HISTORY_SIZE       = 100          # Preishistorie pro Paar
BRAIN_FILE         = "brain.json" # Lern-Datei
PORTFOLIO_FILE     = "portfolio.json"
LOG_FILE           = "trading.log"

TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

PAIRS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "MATIC/USDT", "DOT/USDT",
    "LINK/USDT", "LTC/USDT", "UNI/USDT", "ATOM/USDT", "TRX/USDT"
]
SYMBOLS = {p: p.replace("/", "").lower() for p in PAIRS}

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

# ── Brain (Lern-System) ────────────────────────────────
class Brain:
    DEFAULT_WEIGHTS = {
        "rsi_oversold": 1.0,
        "rsi_low": 1.0,
        "rsi_overbought": 1.0,
        "macd_positive": 1.0,
        "momentum_up": 1.0,
        "momentum_down": 1.0,
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

    def learn(self, indicators: dict, pnl: float, pair: str, entry: float, exit_price: float):
        won = pnl > 0
        delta = 0.05 if won else -0.04
        for key in indicators:
            if key in self.weights:
                self.weights[key] = max(0.1, min(3.0, self.weights[key] + delta))
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
        try:
            if os.path.exists(PORTFOLIO_FILE):
                with open(PORTFOLIO_FILE, "r") as f:
                    data = json.load(f)
                self.cash = data.get("cash", INITIAL_CAPITAL)
                self.positions = data.get("positions", {})
                self.trades = data.get("trades", [])
                log.info(f"💼 Portfolio geladen: €{self.cash:.2f} Cash, {len(self.positions)} Positionen")
        except Exception as e:
            log.warning(f"Portfolio laden fehlgeschlagen: {e}")

    def save(self):
        try:
            with open(PORTFOLIO_FILE, "w") as f:
                json.dump({
                    "cash": self.cash,
                    "positions": self.positions,
                    "trades": self.trades[-500:]
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
        units = invest / price
        self.cash -= invest
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

    def sell(self, pair: str, price: float, confidence: int, reason: str):
        if pair not in self.positions:
            return False, 0, 0, {}
        pos = self.positions[pair]
        sale_value = pos["amount"] * price
        pnl = sale_value - pos["amount"] * pos["avg_price"]
        indicators = pos.get("indicators", {})
        self.cash += sale_value
        self.trades.append({
            "pair": pair, "type": "SELL", "price": price,
            "amount": pos["amount"], "value": sale_value,
            "pnl": round(pnl, 4), "confidence": confidence,
            "reason": reason, "time": datetime.now().strftime("%H:%M:%S")
        })
        del self.positions[pair]
        self.save()
        return True, sale_value, pnl, indicators

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

    # RSI
    if rsi < 35:
        score += 30 * w["rsi_oversold"]
        reasons.append("RSI überverkauft")
        indicators["rsi_oversold"] = True
    elif rsi < 45:
        score += 15 * w["rsi_low"]
        reasons.append("RSI niedrig")
        indicators["rsi_low"] = True
    elif rsi > 65:
        score -= 30 * w["rsi_overbought"]
        reasons.append("RSI überkauft")
        indicators["rsi_overbought"] = True
    elif rsi > 55:
        score -= 15
        reasons.append("RSI hoch")

    # MACD
    if macd_hist > 0:
        score += 20 * w["macd_positive"]
        reasons.append("MACD positiv")
        indicators["macd_positive"] = True
    else:
        score -= 20
        reasons.append("MACD negativ")

    # Momentum
    if len(history) >= 5:
        momentum = (history[-1] - history[-5]) / history[-5] * 100
        if momentum > 0.3:
            score += 15 * w["momentum_up"]
            reasons.append("Aufwärtstrend")
            indicators["momentum_up"] = True
        elif momentum < -0.3:
            score -= 15 * w["momentum_down"]
            reasons.append("Abwärtstrend")
            indicators["momentum_down"] = True

    # 24h Change
    if change > 2:
        score += 10
    elif change < -2:
        score -= 10

    # Position management
    if has_pos and score > 10:
        score -= 15
    if not has_pos and score < -10:
        score = min(score, -5)

    # Signal
    if score >= 20 and not has_pos:
        signal = "BUY"
        confidence = min(51 + int(score / 2), 95)
    elif score <= -20 and has_pos:
        signal = "SELL"
        confidence = min(51 + int(abs(score) / 2), 95)
    elif score >= 15 and not has_pos:
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
        log.info(f"🤖 Trading-Loop gestartet | Zyklus: {CYCLE_SECONDS}s | Min. Konfidenz: {MIN_CONFIDENCE}%")
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

                if conf >= MIN_CONFIDENCE and sig == "BUY":
                    success, invested = self.portfolio.buy(pair, price, conf, reason, result["indicators"])
                    if success:
                        msg = f"🟢 GEKAUFT {pair}\n💰 €{invested:.2f} @ ${price:.4f}\n📊 RSI: {result['rsi']} | {reason}\n🎯 Konfidenz: {conf}%"
                        log.info(f"🟢 BUY  {pair:<12} | €{invested:.2f} @ ${price:.4f} | {conf}% | {reason}")
                        await send_telegram(f"🟢 <b>GEKAUFT {pair}</b>\n💰 €{invested:.2f} @ ${price:.4f}\n📊 {reason} | {conf}% Konfidenz")

                elif conf >= MIN_CONFIDENCE and sig == "SELL":
                    success, value, pnl, indicators = self.portfolio.sell(pair, price, conf, reason)
                    if success:
                        self.brain.learn(indicators, pnl, pair, self.prices[pair]["current"], price)
                        emoji = "✅" if pnl >= 0 else "❌"
                        log.info(f"🔴 SELL {pair:<12} | €{value:.2f} | PnL: {pnl:+.2f}€ | {conf}% | {reason}")
                        await send_telegram(f"🔴 <b>VERKAUFT {pair}</b>\n💰 €{value:.2f} @ ${price:.4f}\n{emoji} PnL: €{pnl:+.2f}\n📊 {reason}")

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
        """Sendet alle 6 Stunden einen Status-Report via Telegram"""
        while self.running:
            await asyncio.sleep(6 * 3600)
            if not self.running:
                break
            total = self.portfolio.total_value(self.prices)
            pnl = total - INITIAL_CAPITAL
            pnl_pct = (pnl / INITIAL_CAPITAL) * 100
            pos_text = "\n".join([f"  • {p}: {v['amount']:.4f}" for p, v in self.portfolio.positions.items()]) or "  Keine"
            msg = (
                f"📊 <b>CryptoMind Status-Report</b>\n\n"
                f"💼 Kapital: €{total:.2f}\n"
                f"{'📈' if pnl>=0 else '📉'} PnL: €{pnl:+.2f} ({pnl_pct:+.1f}%)\n"
                f"💵 Cash: €{self.portfolio.cash:.2f}\n"
                f"🧠 Winrate: {self.brain.win_rate:.1f}% ({self.brain.total_trades} Trades)\n"
                f"📌 Positionen:\n{pos_text}"
            )
            await send_telegram(msg)

    async def run(self):
        self.running = True
        log.info("═" * 50)
        log.info("   CRYPTOMIND TRADING BOT  gestartet")
        log.info(f"   Kapital: €{INITIAL_CAPITAL} | Paare: {len(PAIRS)}")
        log.info("═" * 50)
        await asyncio.gather(
            self.connect_binance(),
            self.trading_loop(),
            self.status_report()
        )

# ── Start ──────────────────────────────────────────────
if __name__ == "__main__":
    bot = CryptoMindBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        log.info("Bot gestoppt")
