# -*- coding: utf-8 -*-
"""
Deriv V100 Trading Bot — Adaptive & Confirmed Entry (Easier Entry)
เข้าออเดอร์บ่อยขึ้น: ลดเกณฑ์ strict หลายตัว
- SCORE_THRESHOLD 1
- ยืดหยุ่น strong_trend_ok แค่ 2 ใน 3 ข้อ ผ่านได้
- ATR sideway filter relax
- CONFIRM_BARS = 1
- MIN_TICKS_PER_WIN = 2
- ยังมี risk control ครบ
"""
import os
import json
import time
import csv
import logging
import threading
from collections import deque, defaultdict
import numpy as np
from websocket import WebSocketApp
from flask import Flask, jsonify

# === CONFIG (ปรับใหม่) ===
SYMBOL = "R_100"
DURATION_MIN = 1
AMOUNT = 100
APP_ID = 1089

EMA_FAST_P = 5
EMA_MID_P = 20
EMA_SLOW_P = 50
SCORE_THRESHOLD = 1

CANDLE_SECONDS = 60
CONFIRM_BARS = 1
HIST_CONSEC_BARS = 1

K_EMA_GAP = 0.3
K_EMA_SLOPE = 0.18
K_ATR_RATIO = 0.22

SLOPE_WINDOW = 6

TICK_FREQ_WIN_SEC = 30
MIN_TICKS_PER_WIN = 2

REVERSAL_HIST_MIN = 0.0
ATR_PERIOD = 14
WATCH_WINDOW_SEC = 20
MIN_PAYOUT = 1.68

MAX_TRADES_PER_HOUR = 12
MAX_DAILY_LOSS = -100
DAILY_PROFIT_TARGET = 150
LOSS_COOLDOWN_SEC = 90
POST_BUY_COOLDOWN = 15
MAX_CONSEC_LOSSES = 3
PAUSE_AFTER_STREAK_SEC = 240

OUTLIER_PCT = 0.05
PRICE_HISTORY_MAX = 600
CANDLES_MAX = 600
CONTRACT_TIMEOUT_SEC = DURATION_MIN * 60 + 30
MIN_READY_BARS = 30
API_TOKEN = os.getenv("DERIV_TOKEN", "C82t0gtcRoQv99X")

# === LOGGING SETUP ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger("deriv-bot")

# === GLOBAL STATE ===
lock = threading.Lock()
price_history = deque(maxlen=PRICE_HISTORY_MAX)
candles = deque(maxlen=CANDLES_MAX)
hist_buffer = deque(maxlen=10)
mid_series = deque(maxlen=20)
active_contract_id = None
contract_sub_id = None
last_trade_time = 0.0
total_trades = 0
wins = 0
losses = 0
equity = 0.0
consecutive_losses = 0
trades_per_hour = defaultdict(int)
pause_until = 0.0
post_buy_until = 0.0
recent_dirs = deque(maxlen=CONFIRM_BARS)
pending_dir = None
watch_until = 0.0
watch_hist_sign = 0
_current_candle = {"open": None, "high": None, "low": None, "close": None, "t0": None}
tick_times = deque(maxlen=600)

# === UTILS ===
def hour_key(ts=None):
    t = time.localtime(ts or time.time())
    return f"{t.tm_year}-{t.tm_yday}-{t.tm_hour}"

def is_valid_tick(new_price, last_price):
    if last_price is None:
        return True
    change = abs(new_price - last_price) / last_price
    return change <= OUTLIER_PCT

# === INDICATORS ===
class EMACalculator:
    def __init__(self, period):
        self.period = period
        self.mult = 2.0 / (period + 1.0)
        self.ema = None
        self._warm = []
    def update(self, price):
        if self.ema is None:
            self._warm.append(price)
            if len(self._warm) >= self.period:
                self.ema = sum(self._warm) / len(self._warm)
                self._warm = None
                logger.info(f"EMA({self.period}) initialized: {self.ema:.6f}")
            return self.ema
        self.ema = price * self.mult + self.ema * (1.0 - self.mult)
        return self.ema
    def value(self):
        return self.ema
    def ready(self):
        return self.ema is not None

class MACDCalculator:
    def __init__(self):
        self.ema12 = EMACalculator(12)
        self.ema26 = EMACalculator(26)
        self.sig9  = EMACalculator(9)
    def update(self, price):
        e12 = self.ema12.update(price)
        e26 = self.ema26.update(price)
        if e12 is None or e26 is None:
            return None, None, None
        macd = e12 - e26
        sig = self.sig9.update(macd)
        if sig is None:
            return macd, None, None
        hist = macd - sig
        return macd, sig, hist
    def ready(self):
        return self.ema12.ready() and self.ema26.ready() and self.sig9.ready()

ema_fast_calc = EMACalculator(EMA_FAST_P)
ema_mid_calc  = EMACalculator(EMA_MID_P)
ema_slow_calc = EMACalculator(EMA_SLOW_P)
macd_calc     = MACDCalculator()

def update_candle(price, ts_epoch):
    global _current_candle
    bucket = ts_epoch - (ts_epoch % CANDLE_SECONDS)
    if _current_candle["t0"] is None:
        _current_candle = {"open": price, "high": price, "low": price, "close": price, "t0": bucket}
        return None
    if bucket > _current_candle["t0"]:
        closed = _current_candle.copy()
        candles.append(closed)
        _current_candle = {"open": price, "high": price, "low": price, "close": price, "t0": bucket}
        return closed
    else:
        _current_candle["high"] = max(_current_candle["high"], price)
        _current_candle["low"]  = min(_current_candle["low"], price)
        _current_candle["close"] = price
        return None

def compute_atr(c_list, period=ATR_PERIOD):
    if len(c_list) < period + 1:
        return None
    trs = []
    for i in range(1, len(c_list)):
        p = c_list[i]; p1 = c_list[i-1]
        tr = max(
            p["high"] - p["low"],
            abs(p["high"] - p1["close"]),
            abs(p["low"]  - p1["close"])
        )
        trs.append(tr)
    return np.mean(trs[-period:]) if len(trs) >= period else None

def compute_std_ratio():
    if len(price_history) < 30:
        return None
    closes = np.array(list(price_history)[-30:], dtype=float)
    mean = closes.mean()
    if mean == 0:
        return None
    return closes.std(ddof=1) / mean

def get_dynamic_thresholds():
    cl = list(candles)
    if len(cl) < max(ATR_PERIOD + 1, 20) or len(price_history) < 30:
        return None, None, None
    atr = compute_atr(cl, ATR_PERIOD)
    last20 = [c["close"] for c in cl[-20:]]
    avg_close = np.mean(last20) if last20 else None
    std_ratio = compute_std_ratio()
    if atr is None or avg_close in (None, 0) or std_ratio is None:
        return None, None, None
    min_ema_gap   = K_EMA_GAP   * (atr / avg_close)
    min_ema_slope = K_EMA_SLOPE * (atr / avg_close)
    atr_ratio_th  = K_ATR_RATIO * std_ratio
    return min_ema_gap, min_ema_slope, atr_ratio_th

def is_sideway_atr():
    cl = list(candles)
    atr = compute_atr(cl, ATR_PERIOD)
    if atr is None or len(cl) < 20:
        return False
    last20 = [c["close"] for c in cl[-20:]]
    avg_close = np.mean(last20) if last20 else 0
    min_gap, min_slope, atr_ratio_th = get_dynamic_thresholds()
    if atr_ratio_th is None or avg_close == 0:
        return False
    # sideway guard ผ่อนคลายขึ้น
    return (atr / avg_close) < (atr_ratio_th * 0.6)

def is_low_liquidity():
    now = time.time()
    while tick_times and now - tick_times[0] > TICK_FREQ_WIN_SEC:
        tick_times.popleft()
    cnt = len(tick_times)
    if cnt < MIN_TICKS_PER_WIN:
        logger.info(f"liquidity_dbg: ticks_in_{TICK_FREQ_WIN_SEC}s={cnt} < {MIN_TICKS_PER_WIN}")
        return True
    return False

def regression_slope(series, window=SLOPE_WINDOW):
    if len(series) < window:
        return None
    y = np.array(list(series)[-window:], dtype=float)
    x = np.arange(window, dtype=float)
    a, b = np.polyfit(x, y, 1)
    return a

def get_signal_score_and_direction(close_price):
    ema_fast = ema_fast_calc.update(close_price)
    ema_mid  = ema_mid_calc.update(close_price)
    ema_slow = ema_slow_calc.update(close_price)
    macd_line, sig_line, hist = macd_calc.update(close_price)
    if not (ema_fast_calc.ready() and ema_mid_calc.ready() and ema_slow_calc.ready() and macd_calc.ready()):
        return 0, None, ema_fast, ema_mid, ema_slow, hist
    score = 0
    if ema_fast > ema_mid: score += 1
    if ema_mid > ema_slow: score += 1
    if macd_line > sig_line: score += 1
    if hist > 0: score += 1
    if not is_sideway_atr(): score += 1
    up_votes = 0
    up_votes += 1 if ema_fast > ema_mid else 0
    up_votes += 1 if ema_mid  > ema_slow else 0
    up_votes += 1 if macd_line > sig_line else 0
    up_votes += 1 if hist > 0 else 0
    direction = None
    if score >= SCORE_THRESHOLD:
        if up_votes >= 3:
            direction = "CALL"
        elif (4 - up_votes) >= 3:
            direction = "PUT"
    return score, direction, ema_fast, ema_mid, ema_slow, hist

def strong_trend_ok(avg_price, ema_mid, ema_slow, hist_series, bullish):
    min_gap, min_slope, _atr_ratio_th = get_dynamic_thresholds()
    if avg_price is None or ema_mid is None or ema_slow is None or min_gap is None or min_slope is None:
        logger.info("quality_fail: thresholds/data not ready")
        return False
    gap = abs(ema_mid - ema_slow) / avg_price if avg_price else 0
    slope_raw = regression_slope(mid_series, window=SLOPE_WINDOW)
    slope_norm = abs(slope_raw) / avg_price if (slope_raw is not None and avg_price) else 0
    if len(hist_series) < HIST_CONSEC_BARS:
        hist_ok = False
    else:
        last_slice = hist_series[-HIST_CONSEC_BARS:]
        hist_ok = all(h > 0 for h in last_slice) if bullish else all(h < 0 for h in last_slice)
    ok_cnt = 0
    if gap >= min_gap: ok_cnt += 1
    if slope_norm >= min_slope: ok_cnt += 1
    if hist_ok: ok_cnt += 1
    logger.info(f"trend_dbg: gap={gap:.4f} min={min_gap:.4f} slope={slope_norm:.5f} min={min_slope:.5f} hist_ok={hist_ok}")
    return ok_cnt >= 2

def market_ready():
    return (
        ema_slow_calc.ready() and macd_calc.ready()
        and len(candles) >= 30
        and len(price_history) >= MIN_READY_BARS
    )

def request_ticks(ws):
    ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))

def request_proposal(ws, contract_type):
    req = {
        "proposal": 1,
        "amount": AMOUNT,
        "basis": "stake",
        "contract_type": contract_type,
        "currency": "USD",
        "duration": DURATION_MIN,
        "duration_unit": "m",
        "symbol": SYMBOL
    }
    ws.send(json.dumps(req))
    logger.info(f"🧾 Proposal requested: {contract_type}")

def buy_from_proposal(ws, proposal_id):
    ws.send(json.dumps({"buy": proposal_id, "price": AMOUNT}))
    logger.info(f"🚀 Buy requested from proposal_id={proposal_id}")

def base_can_trade_now():
    now = time.time()
    if now < pause_until:
        return False, "paused"
    if now < post_buy_until:
        return False, "post_buy_cooldown"
    if equity <= MAX_DAILY_LOSS:
        return False, "daily_loss_limit"
    if equity >= DAILY_PROFIT_TARGET:
        return False, "daily_profit_target"
    if trades_per_hour[hour_key()] >= MAX_TRADES_PER_HOUR:
        return False, "hourly_cap"
    if active_contract_id is not None:
        return False, "active_contract"
    if is_low_liquidity():
        return False, "low_liquidity"
    return True, "ok"

def update_result(result, profit):
    global total_trades, wins, losses, equity, consecutive_losses, pause_until, post_buy_until
    with lock:
        total_trades += 1
        equity += float(profit)
        trades_per_hour[hour_key()] += 1
        if result == "WIN":
            wins += 1
            consecutive_losses = 0
        else:
            losses += 1
            consecutive_losses += 1
            pause_until = max(pause_until, time.time() + LOSS_COOLDOWN_SEC)
            if consecutive_losses >= MAX_CONSEC_LOSSES:
                pause_until = max(pause_until, time.time() + PAUSE_AFTER_STREAK_SEC)
                logger.warning(f"🛑 Too many losses — pause {PAUSE_AFTER_STREAK_SEC//60} min.")
        post_buy_until = time.time() + POST_BUY_COOLDOWN
    win_rate = (wins / total_trades) * 100 if total_trades else 0.0
    logger.info("\n===== 📊 SUMMARY =====")
    logger.info(f"Result        : {result}")
    logger.info(f"PnL           : {profit:.2f} USD")
    logger.info(f"Equity        : {equity:.2f} USD")
    logger.info(f"Trades        : {total_trades}  (W/L={wins}/{losses}, WinRate={win_rate:.2f}%)")
    logger.info(f"Cons. Losses  : {consecutive_losses}")
    logger.info("==============\n")

TRADE_LOG_FILE = "trades.csv"
if not os.path.exists(TRADE_LOG_FILE):
    with open(TRADE_LOG_FILE, "w", newline="") as f:
        csv.writer(f).writerow(["time","signal","return_ratio","result","profit","equity"])

def log_trade(signal, return_ratio, result, profit, equity_now):
    with open(TRADE_LOG_FILE, "a", newline="") as f:
        csv.writer(f).writerow([
            time.strftime('%Y-%m-%d %H:%M:%S'),
            signal, f"{return_ratio:.4f}", result, f"{profit:.2f}", f"{equity_now:.2f}"
        ])

def process_closed_candle(ws, closed_candle):
    global pending_dir, watch_until, watch_hist_sign
    close_price = closed_candle["close"]
    with lock:
        price_history.append(close_price)
    score, signal, ema_fast, ema_mid, ema_slow, hist = get_signal_score_and_direction(close_price)
    if ema_mid is not None:
        mid_series.append(ema_mid)
    if hist is not None:
        hist_buffer.append(hist)
    if signal and score >= SCORE_THRESHOLD:
        recent_dirs.append(signal)
        if len(recent_dirs) == CONFIRM_BARS and len(set(recent_dirs)) == 1:
            pending_dir = signal
            watch_until = closed_candle["t0"] + CANDLE_SECONDS + WATCH_WINDOW_SEC
            watch_hist_sign = 1 if (hist is not None and hist > 0) else (-1 if (hist is not None and hist < 0) else 0)
            mg, ms, atr_th = get_dynamic_thresholds()
            logger.info(
                f"👀 WATCH: {pending_dir} (confirmed {CONFIRM_BARS} bars), "
                f"until ~{int(max(0, watch_until - time.time()))}s | "
                f"thr[min_gap={mg}, min_slope={ms}, atr_th={atr_th}]"
            )
    else:
        recent_dirs.clear()

def maybe_precheck_and_request(ws):
    global pending_dir, watch_hist_sign
    if not pending_dir:
        return
    now = time.time()
    if now > watch_until:
        pending_dir = None
        return
    if not market_ready():
        return
    ok, reason = base_can_trade_now()
    if not ok:
        if reason != "active_contract":
            logger.info(f"⏸️ Skip (precheck): {reason}")
        return
    if hist_buffer:
        latest_hist = hist_buffer[-1]
        latest_sign = 1 if latest_hist > 0 else (-1 if latest_hist < 0 else 0)
        if (
            watch_hist_sign != 0 and latest_sign != 0 and latest_sign != watch_hist_sign
            and abs(latest_hist) >= REVERSAL_HIST_MIN
        ):
            logger.info("🚫 Reversal trap: histogram flipped during WATCH — cancel signal")
            pending_dir = None
            return
    with lock:
        avg_price = np.mean(list(price_history)[-20:]) if len(price_history) >= 20 else None
        ema_mid = ema_mid_calc.value()
        ema_slow = ema_slow_calc.value()
        atr_ok = not is_sideway_atr()
    bullish = (pending_dir == "CALL")
    if strong_trend_ok(avg_price, ema_mid, ema_slow, list(hist_buffer), bullish) and atr_ok:
        request_proposal(ws, pending_dir)
        pending_dir = None
    else:
        logger.info("⏸️ Precheck quality not met; waiting...")

def on_open(ws):
    ws._backoff = 2
    logger.info("✅ Connected")
    ws.send(json.dumps({"authorize": API_TOKEN}))

def on_message(ws, message):
    global active_contract_id, contract_sub_id, last_trade_time, pause_until, post_buy_until
    data = json.loads(message)
    if data.get("msg_type") == "authorize":
        if "error" in data:
            logger.error(f"❌ Auth error: {data['error']}")
            return
        logger.info("✅ Authorized")
        request_ticks(ws)
        return
    if data.get("msg_type") == "error":
        err = data.get("error", {}).get("message")
        logger.error(f"❌ API Error: {err}")
        return
    if data.get("msg_type") == "tick":
        tick = data["tick"]
        price = float(tick["quote"])
        ts = int(tick["epoch"])
        tick_times.append(time.time())
        last_tick_price = _current_candle["close"] if _current_candle["close"] is not None else None
        if not is_valid_tick(price, last_tick_price):
            logger.warning(f"⚠️ Outlier tick filtered: {last_tick_price} -> {price}")
            return
        closed_candle = update_candle(price, ts)
        if closed_candle:
            process_closed_candle(ws, closed_candle)
        maybe_precheck_and_request(ws)
        if active_contract_id is not None and (time.time() - last_trade_time > CONTRACT_TIMEOUT_SEC):
            logger.warning(f"⚠️ Contract timeout {CONTRACT_TIMEOUT_SEC}s. Force reset.")
            with lock:
                active_contract_id = None
            if contract_sub_id:
                ws.send(json.dumps({"forget": contract_sub_id}))
                contract_sub_id = None
        return
    if data.get("msg_type") == "proposal":
        quote = data["proposal"]
        payout = float(quote.get("payout", 0) or 0)
        ask_price = float(quote.get("ask_price", 0) or 0)
        rr = (payout / ask_price) if ask_price else 0.0
        ctype = quote.get("contract_type")
        if not market_ready():
            logger.info("❎ Skip proposal: market not ready")
            return
        ok, reason = base_can_trade_now()
        if not ok:
            logger.info(f"❎ Skip proposal (risk): {reason}")
            return
        with lock:
            avg_price = np.mean(list(price_history)[-20:]) if len(price_history) >= 20 else None
            ema_mid = ema_mid_calc.value()
            ema_slow = ema_slow_calc.value()
            atr_ok = not is_sideway_atr()
        bullish = (ctype == "CALL")
        strong_ok = strong_trend_ok(avg_price, ema_mid, ema_slow, list(hist_buffer), bullish) and atr_ok
        logger.info(f"proposal_dbg: payout={payout:.2f} ask={ask_price:.2f} rr={rr:.3f} need>={MIN_PAYOUT} strong_ok={strong_ok}")
        if rr >= MIN_PAYOUT and strong_ok:
            buy_from_proposal(ws, quote["id"])
        else:
            logger.info(f"❎ Skip proposal: RR={rr:.2f} (need {MIN_PAYOUT}) strong_ok={strong_ok}")
        return
    if data.get("msg_type") == "buy":
        contract_id = data["buy"]["contract_id"]
        with lock:
            active_contract_id = contract_id
            last_trade_time = time.time()
        logger.info(f"📈 Buy confirmed. Contract ID: {contract_id}")
        ws.send(json.dumps({
            "proposal_open_contract": 1,
            "contract_id": contract_id,
            "subscribe": 1
        }))
        return
    if data.get("msg_type") == "proposal_open_contract":
        poc = data["proposal_open_contract"]
        if "subscription" in poc and poc["subscription"] and "id" in poc["subscription"]:
            subid = poc["subscription"]["id"]
            if subid:
                contract_sub_id = subid
        if poc.get("is_sold"):
            profit = float(poc.get("profit", 0) or 0)
            result = "WIN" if profit > 0 else "LOSS"
            update_result(result, profit)
            with lock:
                cid = active_contract_id
                active_contract_id = None
            if contract_sub_id:
                ws.send(json.dumps({"forget": contract_sub_id}))
                contract_sub_id = None
            logger.info(f"🧾 Settled Contract {cid} -> {result} ({profit:.2f})")
        return

def on_error(ws, error):
    logger.error(f"❌ WebSocket Error: {error}")

def on_close(ws, code, reason):
    logger.warning(f"🔌 Disconnected: Code={code}, Reason={reason}")
    backoff = getattr(ws, "_backoff", 2)
    backoff = min(backoff * 2, 60)
    ws._backoff = backoff
    logger.info(f"🔁 Reconnecting in {backoff}s ...")
    threading.Timer(backoff, run_bot).start()

def run_bot():
    ws = WebSocketApp(
        f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}",
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    ws.run_forever()

app = Flask(__name__)

@app.route("/")
def status():
    with lock:
        status_str = "paused" if time.time() < pause_until else "running"
        return jsonify({
            "status": status_str,
            "symbol": SYMBOL,
            "duration_min": DURATION_MIN,
            "amount": AMOUNT,
            "trades": total_trades,
            "wins": wins,
            "losses": losses,
            "equity": equity,
            "consecutive_losses": consecutive_losses,
            "active_contract_id": active_contract_id,
            "watch_dir": pending_dir,
            "watch_remaining_sec": max(0, int(watch_until - time.time())) if pending_dir else 0,
            "low_liquidity": is_low_liquidity(),
        })

@app.route("/favicon.ico")
def favicon():
    return "", 204

if __name__ == "__main__":
    if not API_TOKEN or API_TOKEN == "C82t0gtcRoQv99X":
        logger.warning("⚠️ Please set DERIV_TOKEN env var or put your API token in API_TOKEN.")
    logger.info("🤖 Starting Deriv Trading Bot (adaptive confirmed-entry, easier entry)…")
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=10000)
