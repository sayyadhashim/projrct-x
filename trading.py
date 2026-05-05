"""
alpaca_markov_bot.py — US Stock Markov Scanner + Alpaca Paper Trading
=======================================================================
Completely separate from your NSE bot.
Scans US stocks using same Markov strategy.
Runs during US market hours: 9:30 AM – 4:00 PM EST (7 PM – 1:30 AM IST)
Places bracket orders directly on Alpaca Paper Trading.
Sends Telegram alerts for every signal and closed trade.

Deploy this as a NEW Render Web Service.

Requirements:
    yfinance pandas numpy requests pytz pyotp logzero
    alpaca-trade-api websocket-client urllib3
"""

import yfinance as yf
import pandas as pd
import numpy as np
import time
import requests
import json
import threading
import os
import pytz
import warnings

from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import alpaca_trade_api as tradeapi
from alpaca_trade_api.rest import APIError

warnings.filterwarnings('ignore')

# =============================================================================
# CONFIGURATION
# =============================================================================

# ── US Watchlist ──────────────────────────────────────────────────────────────
TICKERS = [
    "AAPL",   # Apple
    "MSFT",   # Microsoft
    "GOOGL",  # Google
    "AMZN",   # Amazon
    "NVDA",   # Nvidia
    "META",   # Meta
    "TSLA",   # Tesla
    "JPM",    # JP Morgan
    "INFY",   # Infosys (US listed)
    "WIT",    # Wipro (US listed)
]

# ── Strategy Settings ─────────────────────────────────────────────────────────
ENTRY_THRESH    = 0.68    # Markov probability threshold
ATR_MULT        = 1.5     # ATR multiplier for SL
RR_RATIO        = 3.0     # Risk:Reward ratio
MAX_POSITIONS   = 3       # Max simultaneous open trades
MAX_CONSEC_LOSS = 3       # Auto-pause after this many losses in a row
SL_COOLDOWN_MIN = 30      # Minutes to wait after stop loss

# ── US Market Hours (EST) ─────────────────────────────────────────────────────
MARKET_START    = '09:45'  # Skip first 15 min of US open
MARKET_END      = '15:30'  # Stop 30 min before US close
EOD_CLOSE       = '15:45'  # Force close all at this time EST

# ── Alpaca Credentials ────────────────────────────────────────────────────────
ALPACA_API_KEY    = "PKLHKP2BJMCAOYWONVSHANDZV5"
ALPACA_SECRET_KEY = "GtxsSeUzRHALEMiL13JQSxM9W6xWRAeQVYYSzpY9tovT"
ALPACA_BASE_URL   = "https://paper-api.alpaca.markets"
QTY               = int(os.environ.get("TRADE_QTY", "1"))

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",   "8701070280:AAHPIDZpQZLHGar0HEh6f84SEJcJGHbWQys")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8125685903")

# ── Timezones ─────────────────────────────────────────────────────────────────
EST = pytz.timezone("US/Eastern")
IST = pytz.timezone("Asia/Kolkata")

# =============================================================================
# SHARED STATE
# =============================================================================
state_lock       = threading.Lock()
open_positions   = {}       # { symbol: position_dict }
last_signal_time = {}       # { symbol: candle_id }
sl_cooldown      = {}       # { symbol: datetime }
session_signals  = set()    # symbols traded today
latest_signal    = {"text": "", "timestamp": ""}
trade_history    = []
alpaca_api       = None

session_stats = {
    "wins": 0, "losses": 0,
    "net_pnl": 0.0,
    "consec_loss": 0,
    "paused": False
}

# =============================================================================
# TELEGRAM
# =============================================================================
def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       message,
            "parse_mode": "Markdown"
        }, timeout=5)
    except Exception as e:
        print(f"Telegram error: {e}")


def send_daily_summary():
    with state_lock:
        w   = session_stats["wins"]
        l   = session_stats["losses"]
        pnl = session_stats["net_pnl"]
        tot = w + l
        wr  = (w / tot * 100) if tot else 0

    send_telegram(
        f"📊 *US Market Daily Summary*\n\n"
        f"*Total Trades:* {tot}\n"
        f"*Wins:* {w} | *Losses:* {l}\n"
        f"*Win Rate:* {wr:.1f}%\n"
        f"*Net P&L:* {'+'if pnl>=0 else ''}${pnl:.2f}\n"
        f"*Time:* {datetime.now(EST).strftime('%H:%M EST')}"
    )

# =============================================================================
# ALPACA CONNECTION
# =============================================================================
def connect_alpaca():
    global alpaca_api
    if not ALPACA_API_KEY:
        print("⚠️  ALPACA_API_KEY not set. Orders will be skipped.")
        return None
    try:
        api = tradeapi.REST(
            ALPACA_API_KEY,
            ALPACA_SECRET_KEY,
            ALPACA_BASE_URL,
            api_version='v2'
        )
        account = api.get_account()
        print(f"✅ Alpaca Paper connected")
        print(f"   Cash      : ${float(account.cash):,.2f}")
        print(f"   Portfolio : ${float(account.portfolio_value):,.2f}")
        alpaca_api = api
        return api
    except Exception as e:
        print(f"❌ Alpaca connection error: {e}")
        return None


def is_market_open() -> bool:
    """Check if US market is open via Alpaca clock."""
    global alpaca_api
    if not alpaca_api:
        return False
    try:
        clock = alpaca_api.get_clock()
        return clock.is_open
    except Exception:
        # Fallback: check EST time manually
        now = datetime.now(EST).strftime('%H:%M')
        today = datetime.now(EST).weekday()  # 0=Mon, 6=Sun
        return today < 5 and '09:30' <= now <= '16:00'


def place_bracket_order_alpaca(symbol, direction, entry_price, sl_price, tp_price):
    """Place bracket order on Alpaca."""
    global alpaca_api
    if not alpaca_api:
        print("⚠️  No Alpaca session. Skipping order.")
        return None

    side = "buy" if direction == "LONG" else "sell"

    # Calculate SL/TP as % from entry then apply to current US price
    sl_pct = abs(entry_price - sl_price) / entry_price
    tp_pct = abs(tp_price - entry_price) / entry_price

    # Get current market price
    try:
        bar = alpaca_api.get_latest_bar(symbol)
        current = float(bar.c)
    except Exception as e:
        print(f"❌ Price fetch error for {symbol}: {e}")
        return None

    if direction == "LONG":
        us_sl = round(current * (1 - sl_pct), 2)
        us_tp = round(current * (1 + tp_pct), 2)
    else:
        us_sl = round(current * (1 + sl_pct), 2)
        us_tp = round(current * (1 - tp_pct), 2)

    print(f"📤 {side.upper()} {symbol} x{QTY} @ ${current:.2f} | SL: ${us_sl:.2f} | TP: ${us_tp:.2f}")

    try:
        order = alpaca_api.submit_order(
            symbol        = symbol,
            qty           = QTY,
            side          = side,
            type          = "market",
            time_in_force = "day",
            order_class   = "bracket",
            stop_loss     = {"stop_price": str(us_sl)},
            take_profit   = {"limit_price": str(us_tp)},
        )
        print(f"✅ Order placed: {order.id}")
        return {"order_id": order.id, "price": current, "sl": us_sl, "tp": us_tp}
    except APIError as e:
        print(f"❌ Alpaca order error: {e}")
        return None


def check_alpaca_positions():
    """
    Check Alpaca for closed positions and update our tracking.
    Alpaca bracket orders auto-close on SL/TP hit.
    """
    global alpaca_api
    if not alpaca_api:
        return
    try:
        # Get all closed orders from today
        # Note: removed 'nested' arg — not supported in all SDK versions
        orders = alpaca_api.list_orders(
            status='closed',
            limit=50
        )
        for order in orders:
            sym = order.symbol
            with state_lock:
                if sym not in open_positions:
                    continue
                pos = open_positions[sym]

            # Check if the position was closed by SL or TP
            if order.status in ['filled', 'canceled']:
                # Try to get fill price
                fill_price = float(order.filled_avg_price) if order.filled_avg_price else 0
                entry      = pos.get("us_entry", pos["entry"])
                direction  = pos["direction"]

                if fill_price > 0:
                    pnl = (fill_price - entry) if direction == "LONG" else (entry - fill_price)
                    pnl = round(pnl, 2)
                    result = "WIN" if pnl > 0 else "LOSS"
                    emoji  = "✅" if result == "WIN" else "🛑"

                    send_telegram(
                        f"{emoji} *US TRADE CLOSED — {result}*\n\n"
                        f"*Symbol:* {sym}\n"
                        f"*Direction:* {direction}\n"
                        f"*Entry:* ${entry:.2f} → *Exit:* ${fill_price:.2f}\n"
                        f"*P&L:* ${pnl:+.2f}"
                    )
                    print(f">>> CLOSED {sym} [{result}] @ ${fill_price:.2f} | P&L: ${pnl:+.2f}")

                    with state_lock:
                        session_stats["net_pnl"] += pnl
                        if result == "WIN":
                            session_stats["wins"] += 1
                            session_stats["consec_loss"] = 0
                            session_stats["paused"] = False
                        else:
                            session_stats["losses"] += 1
                            session_stats["consec_loss"] += 1
                            sl_cooldown[sym] = datetime.now(EST)
                            if session_stats["consec_loss"] >= MAX_CONSEC_LOSS:
                                session_stats["paused"] = True
                                send_telegram(
                                    f"⚠️ *Bot Paused*\n"
                                    f"{MAX_CONSEC_LOSS} consecutive losses.\n"
                                    f"Auto-resumes in 30 min."
                                )

                        trade_history.append({
                            **pos,
                            "exit": fill_price,
                            "pnl":  pnl,
                            "result": result,
                            "closed_at": datetime.now(EST).strftime('%H:%M EST')
                        })
                        if sym in open_positions:
                            del open_positions[sym]

    except Exception as e:
        print(f"Position check error: {e}")


def eod_close_all():
    """Close all Alpaca positions at EOD."""
    global alpaca_api
    if not alpaca_api:
        return
    try:
        positions = alpaca_api.list_positions()
        if not positions:
            return
        print("⏰ EOD — closing all positions...")
        alpaca_api.close_all_positions(cancel_orders=True)
        send_telegram(
            f"⏰ *EOD Square-Off*\n"
            f"Closed {len(positions)} position(s)\n"
            f"Time: {datetime.now(EST).strftime('%H:%M EST')}"
        )
        with state_lock:
            open_positions.clear()
        send_daily_summary()
    except Exception as e:
        print(f"EOD close error: {e}")

# =============================================================================
# MARKET DATA (same Markov logic, US stocks)
# =============================================================================
def fetch_data(symbol):
    df = yf.download(symbol, period="7d", interval="5m", progress=False)
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.strip().lower() for c in df.columns]
    df.rename(columns={
        'open':'Open','high':'High',
        'low':'Low','close':'Close','volume':'Volume'
    }, inplace=True)
    return df.dropna()


def fetch_daily(symbol):
    df = yf.download(symbol, period="60d", interval="1d", progress=False)
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.strip().lower() for c in df.columns]
    df.rename(columns={
        'open':'Open','high':'High',
        'low':'Low','close':'Close'
    }, inplace=True)
    return df.dropna()


def add_regime_labels(df, atr_period=14, threshold=0.6):
    df = df.copy()
    hl  = df['High'] - df['Low']
    hc  = np.abs(df['High'] - df['Close'].shift(1))
    lc  = np.abs(df['Low']  - df['Close'].shift(1))
    tr  = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df['atr']      = tr.rolling(atr_period).mean()
    df['prev_atr'] = df['atr'].shift(1)
    df['up_diff']  = (df['High'] - df['Open']) / df['prev_atr']
    df['dn_diff']  = (df['Open'] - df['Low'])  / df['prev_atr']
    df['regime']   = 'r'
    mask = (df['up_diff'] >= threshold) | (df['dn_diff'] >= threshold)
    df.loc[mask, 'regime'] = 't'
    return df.dropna(subset=['prev_atr'])


def compute_probabilities(states):
    s = [1 if x == 't' else 0 for x in states]
    n = len(s)
    second = {}
    for i in range(2, n):
        key = (s[i-2], s[i-1])
        second.setdefault(key, [0, 0])[1] += 1
        if s[i] == 1:
            second[key][0] += 1
    second_prob = {k: v[0]/v[1] for k, v in second.items() if v[1] > 0}
    return second_prob, sum(s)/n


def compute_rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def get_daily_trend(symbol):
    try:
        df = fetch_daily(symbol)
        if len(df) < 50:
            return 'NEUTRAL'
        df['sma20'] = df['Close'].rolling(20).mean()
        df['sma50'] = df['Close'].rolling(50).mean()
        last = df.iloc[-1]
        if last['Close'] > last['sma20'] > last['sma50']:
            return 'UP'
        elif last['Close'] < last['sma20'] < last['sma50']:
            return 'DOWN'
        return 'NEUTRAL'
    except Exception:
        return 'NEUTRAL'

# =============================================================================
# MAIN SCANNER
# =============================================================================
def scan_market(symbol):
    now_est      = datetime.now(EST)
    current_time = now_est.strftime('%H:%M')

    # ── EOD close ─────────────────────────────────────────────────────────────
    if current_time >= EOD_CLOSE:
        eod_close_all()
        return

    # ── Check Alpaca for closed positions ─────────────────────────────────────
    check_alpaca_positions()

    # ── Bot paused? ───────────────────────────────────────────────────────────
    with state_lock:
        if session_stats["paused"]:
            last_loss = max(sl_cooldown.values()) if sl_cooldown else None
            if last_loss:
                mins = (now_est - last_loss).seconds / 60
                if mins >= 30:
                    session_stats["paused"] = False
                    session_stats["consec_loss"] = 0
                    send_telegram("▶️ *Bot Resumed* — 30 min pause complete.")
            return

    print(f"[{now_est.strftime('%H:%M:%S')} EST] Scanning {symbol}...")

    # ── Already in position? ──────────────────────────────────────────────────
    with state_lock:
        if symbol in open_positions:
            print(f"  -> {symbol} already open. Skipping.")
            return
        if len(open_positions) >= MAX_POSITIONS:
            print(f"  -> Max {MAX_POSITIONS} positions reached. Skipping.")
            return
        if symbol in session_signals:
            print(f"  -> {symbol} already traded today. Skipping.")
            return

    # ── SL cooldown ───────────────────────────────────────────────────────────
    with state_lock:
        cd = sl_cooldown.get(symbol)
    if cd:
        mins = (now_est - cd).seconds / 60
        if mins < SL_COOLDOWN_MIN:
            print(f"  -> {symbol} in SL cooldown ({int(mins)}m). Skipping.")
            return

    # ── Fetch 5-min data ──────────────────────────────────────────────────────
    df = fetch_data(symbol)
    if len(df) < 200:
        print(f"  Not enough data for {symbol}.")
        return

    df = add_regime_labels(df)
    df['sma200']       = df['Close'].rolling(200).mean()
    df['atr_median20'] = df['atr'].rolling(20).median()
    df['rsi']          = compute_rsi(df['Close'])

    if 'Volume' in df.columns:
        df['vol_sma20'] = df['Volume'].rolling(20).mean()
        df['vol_ok']    = (df['atr'] > 1.1 * df['atr_median20']) & \
                          (df['Volume'] > 0.8 * df['vol_sma20'])
    else:
        df['vol_ok']    = df['atr'] > 1.1 * df['atr_median20']

    df.dropna(inplace=True)
    if len(df) < 5:
        return

    second_prob, overall_t = compute_probabilities(df['regime'])
    last_bar = df.iloc[-1]
    prev_bar = df.iloc[-2]

    prev2 = 1 if df['regime'].iloc[-3] == 't' else 0
    prev1 = 1 if df['regime'].iloc[-2] == 't' else 0
    p_t   = second_prob.get((prev2, prev1), overall_t)

    bar_time = last_bar.name
    bar_time = EST.localize(bar_time) if bar_time.tzinfo is None else bar_time.astimezone(EST)
    candle_time      = bar_time.strftime('%H:%M')
    unique_candle_id = bar_time.strftime('%Y-%m-%d %H:%M')
    valid_time       = MARKET_START <= candle_time <= MARKET_END

    price = float(last_bar['Close'])
    atr   = float(last_bar['atr'])
    rsi   = float(last_bar['rsi'])

    print(f"  -> {symbol} ${price:.2f} | Prob: {p_t:.2%} | RSI: {rsi:.1f} | Vol: {last_bar['vol_ok']}")

    # ── Entry checks ──────────────────────────────────────────────────────────
    if not (p_t >= ENTRY_THRESH and last_bar['vol_ok'] and valid_time):
        return
    if last_signal_time.get(symbol) == unique_candle_id:
        return

    # ── Daily trend filter ────────────────────────────────────────────────────
    daily_trend = get_daily_trend(symbol)

    if prev_bar['Close'] > prev_bar['sma200']:
        direction = "LONG"
    elif prev_bar['Close'] < prev_bar['sma200']:
        direction = "SHORT"
    else:
        return

    if daily_trend == "DOWN" and direction == "LONG":
        print(f"  -> Skip LONG — daily DOWN")
        return
    if daily_trend == "UP" and direction == "SHORT":
        print(f"  -> Skip SHORT — daily UP")
        return

    # ── RSI filter ────────────────────────────────────────────────────────────
    if direction == "LONG"  and rsi > 72:
        print(f"  -> Skip LONG — RSI overbought {rsi:.1f}")
        return
    if direction == "SHORT" and rsi < 28:
        print(f"  -> Skip SHORT — RSI oversold {rsi:.1f}")
        return

    # ── SL / TP ───────────────────────────────────────────────────────────────
    if direction == "LONG":
        sl = price - (ATR_MULT * atr)
        tp = price + (ATR_MULT * RR_RATIO * atr)
    else:
        sl = price + (ATR_MULT * atr)
        tp = price - (ATR_MULT * RR_RATIO * atr)

    emoji = "🟢" if direction == "LONG" else "🔴"

    # ── Signal text ───────────────────────────────────────────────────────────
    signal_text = (
        f"{emoji} {direction} ALERT: {symbol}\n"
        f"Time: {candle_time} EST\n"
        f"Entry: ${price:.2f}\n"
        f"Stop Loss: ${sl:.2f}\n"
        f"Target (1:3): ${tp:.2f}\n"
        f"Markov Prob: {p_t:.2%}\n"
        f"RSI: {rsi:.1f} | Trend: {daily_trend}"
    )

    # ── Telegram alert ────────────────────────────────────────────────────────
    send_telegram(
        f"{emoji} *{direction} ALERT: {symbol}*\n\n"
        f"*Time:* {candle_time} EST  "
        f"({datetime.now(IST).strftime('%H:%M')} IST)\n"
        f"*Entry:* ${price:.2f}\n"
        f"*Stop Loss:* ${sl:.2f}\n"
        f"*Target (1:3):* ${tp:.2f}\n"
        f"*Markov Prob:* {p_t:.2%}\n"
        f"*RSI:* {rsi:.1f} | *Trend:* {daily_trend}\n"
        f"*R:R:* 1:{RR_RATIO}"
    )
    print(f">>> SIGNAL: {direction} {symbol} @ ${price:.2f} | Prob: {p_t:.2%}")

    # ── Place Alpaca order ────────────────────────────────────────────────────
    result = place_bracket_order_alpaca(symbol, direction, price, sl, tp)
    us_entry = result["price"] if result else price

    # ── Update state ──────────────────────────────────────────────────────────
    with state_lock:
        latest_signal["text"]      = signal_text
        latest_signal["timestamp"] = now_est.isoformat()
        last_signal_time[symbol]   = unique_candle_id
        session_signals.add(symbol)
        open_positions[symbol] = {
            "symbol":    symbol,
            "direction": direction,
            "entry":     price,
            "us_entry":  us_entry,
            "sl":        round(sl, 2),
            "tp":        round(tp, 2),
            "prob":      round(p_t * 100, 2),
            "rsi":       round(rsi, 1),
            "trend":     daily_trend,
            "opened_at": candle_time,
        }

# =============================================================================
# RESET SESSION
# =============================================================================
def reset_session():
    with state_lock:
        session_signals.clear()
        sl_cooldown.clear()
        session_stats.update({
            "wins": 0, "losses": 0,
            "net_pnl": 0.0,
            "consec_loss": 0,
            "paused": False
        })
    print("🔄 Session reset for new trading day.")

# =============================================================================
# WEB SERVER (Keep Render alive + API endpoints)
# =============================================================================
class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path == "/signal":
            self._json(latest_signal)
        elif self.path == "/positions":
            with state_lock:
                self._json(open_positions)
        elif self.path == "/history":
            with state_lock:
                self._json(trade_history)
        elif self.path == "/stats":
            with state_lock:
                tot = session_stats["wins"] + session_stats["losses"]
                wr  = round(session_stats["wins"]/tot*100, 1) if tot else 0
                self._json({
                    **session_stats,
                    "total_trades":   tot,
                    "win_rate_pct":   wr,
                    "open_positions": len(open_positions),
                })
        elif self.path == "/account":
            if alpaca_api:
                try:
                    acc = alpaca_api.get_account()
                    self._json({
                        "cash":      float(acc.cash),
                        "portfolio": float(acc.portfolio_value),
                        "pnl_today": float(acc.equity) - float(acc.last_equity),
                        "status":    acc.status,
                    })
                except Exception as e:
                    self._json({"error": str(e)})
            else:
                self._json({"error": "Alpaca not connected"})
        elif self.path == "/health":
            self._text("Alpaca Markov Bot is running ✅")
        else:
            self._text("Alpaca Markov Bot ✅")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def _json(self, data):
        body = json.dumps(data, indent=2, default=str).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _text(self, text):
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(text.encode())

    def log_message(self, *args):
        pass


def start_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(('0.0.0.0', port), Handler).serve_forever()

# =============================================================================
# MAIN LOOP
# =============================================================================
if __name__ == "__main__":
    threading.Thread(target=start_server, daemon=True).start()

    print("🚀 Alpaca Markov US Bot starting...")
    print(f"   Tickers  : {TICKERS}")
    print(f"   Threshold: {ENTRY_THRESH}")
    print(f"   Hours    : {MARKET_START}–{MARKET_END} EST")
    print(f"   Max pos  : {MAX_POSITIONS}")

    # Connect Alpaca
    connect_alpaca()

    last_reset_day = None

    while True:
        now_est = datetime.now(EST)
        t       = now_est.strftime('%H:%M')
        today   = now_est.date()

        # Reset session each new trading day
        if today != last_reset_day and t >= '09:00':
            reset_session()
            last_reset_day = today

        # Only scan during US market hours
        if '09:30' <= t <= '16:00':
            for stock in TICKERS:
                try:
                    scan_market(stock)
                except Exception as e:
                    print(f"Error scanning {stock}: {e}")
                time.sleep(5)

            print(f"✅ Scan complete [{t} EST]. Sleeping 5 mins...")
            time.sleep(300)
        else:
            ist_now = datetime.now(IST).strftime('%H:%M')
            print(f"[{t} EST / {ist_now} IST] US Market closed. Sleeping 10 mins...")
            time.sleep(600)
