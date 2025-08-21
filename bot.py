# -*- coding: utf-8 -*-
"""
Deriv V100 Trading Bot — Adaptive & Confirmed Entry (Tuned)
- Candle(1m), multi-bar confirm → WATCH window
- Adaptive thresholds (ATR/avg) + EMA(mid) regression slope
- MACD histogram continuity check
- Tick-frequency liquidity guard
- Reversal trap during WATCH
- Proposal → double-check → Buy
- Subscribed contract; clear on settle
- Risk controls (caps, cooldowns)
- Exponential backoff reconnect
- Flask status endpoint

Tuning in this build to actually take trades:
- Start after ~30 bars instead of 60
- WATCH window 15s
- Liquidity threshold slightly relaxed
- Adaptive thresholds (gap/slope) slightly relaxed
- Early "sideway" guard no longer blocks when data is insufficient; quality filter handles it
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

# ===================== CONFIG =====================
SYMBOL = "R_100"
DURATION_MIN = 1            # ระยะสัญญา (นาที)
AMOUNT = 100                # เงินต่อไม้ (stake)
APP_ID = 1089               # Deriv demo app id

# อินดิเคเตอร์หลัก
EMA_FAST_P = 5
EMA_MID_P  = 20
EMA_SLOW_P = 50
SCORE_THRESHOLD = 3

# การรวมแท่ง
CANDLE_SECONDS = 60

# การยืนยันสัญญาณ
CONFIRM_BARS = 2            # ต้องได้ทิศเดียวกันติดกันกี่แท่ง (จากแท่งที่ปิดแล้ว)

# ==== Adaptive thresholds & robustness ====
SLOPE_WINDOW = 7            # ใช้ค่า 5–7 แท่งได้ (เลือก 7 ให้เนียน)
K_EMA_GAP    = 0.7          # เดิม 1.0 → ผ่อนเล็กน้อย
K_EMA_SLOPE  = 0.6          # เดิม 0.8 → ผ่อนเล็กน้อย
K_ATR_RATIO  = 0.8          # ใช้กำหนดเกณฑ์ sideway แบบ adaptive

# ==== Volume proxy / liquidity ====
TICK_FREQ_WIN_SEC = 20      # วินโดว์วัดความถี่ tick
MIN_TICKS_PER_WIN = 9       # เดิม 12 → 9 (≈ ≥0.45 tick/sec)

# Reversal trap
REVERSAL_HIST_MIN = 0.0     # ขั้นต่ำขนาด histogram เพื่อถือว่าพลิก (0 = ทุกการข้ามศูนย์)

# เกณฑ์ sideway ดั้งเดิม (ถูกแทนด้วย adaptive ภายในฟังก์ชัน)
ATR_PERIOD = 14

# หน้าต่างรอเช็คซ้ำตอนเปิดแท่งใหม่ (WATCH / PRECHECK)
WATCH_WINDOW_SEC = 15       # เดิม 8 → 15

# ข้อเสนอ-อัตราจ่ายขั้นต่ำ
MIN_PAYOUT = 1.75           # payout/ask_price อย่างน้อยเท่านี้

# Risk management
MAX_TRADES_PER_HOUR = 12
MAX_DAILY_LOSS      = -100
DAILY_PROFIT_TARGET = 150
LOSS_COOLDOWN_SEC   = 120   # แพ้ 1 ไม้ พักสั้น ๆ
POST_BUY_COOLDOWN   = 20    # หลังซื้อ พักเพื่อกันเข้าไม้ซ้อน
MAX_CONSEC_LOSSES   = 3     # แพ้ติดกี่ไม้ให้พักยาว
PAUSE_AFTER_STREAK_SEC = 300

# อื่น ๆ
OUTLIER_PCT = 0.05          # ตัด tick กระโดด >5%
PRICE_HISTORY_MAX = 600     # เก็บ close ล่าสุดกี่แท่ง
CANDLES_MAX = 600

# Timeout: ผูกกับ duration
CONTRACT_TIMEOUT_SEC = DURATION_MIN * 60 + 30

# เริ่มทำงานหลังมีข้อมูลพอ
MIN_READY_BARS = 30         # เดิม 60 → 30 (อินดี้ชุดนี้พอแล้ว)

# Token (ใช้ env ก่อน ถ้าไม่มีก็ fallback)
API_TOKEN = os.getenv("DERIV_TOKEN", "C82t0gtcRoQv99X")

# ================== LOGGING SETUP ==================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger("deriv-bot")

# ================== GLOBAL STATE ===================
lock = threading.Lock()

# ราคา/แท่ง/อินดิเคเตอร์
price_history = deque(maxlen=PRICE_HISTORY_MAX)  # เก็บ close ของแท่ง
candles = deque(maxlen=CANDLES_MAX)              # เก็บแท่ง (dict)
hist_buffer = deque(maxlen=10)                   # เก็บ MACD histogram ล่าสุด (ปิดแท่งเท่านั้น)
mid_series = deque(maxlen=20)                    # เก็บ EMA(mid) ล่าสุด ใช้ regression slope

# สถานะการซื้อขาย
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

# กระบวนการสัญญาณ
recent_dirs = deque(maxlen=CONFIRM_BARS)
pending_dir = None
watch_until = 0.0
watch_hist_sign = 0  # บันทึกสัญญาณ histogram ตอนเข้าช่วง WATCH (+1/-1/0)

# ตัวรวมแท่งจาก tick
_current_candle = {"open": None, "high": None, "low": None, "close": None, "t0": None}

# Volume proxy: เก็บเวลา tick
tick_times = deque(maxlen=600)  # ~10 นาที

# ================== UTILITIES ======================
def hour_key(ts=None):
    t = time.localtime(ts or time.time())
    return f"{t.tm_year}-{t.tm_yday}-{t.tm_hour}"

def is_valid_tick(new_price, last_price):
    if last_price is None:
        return True
    change = abs(new_price - last_price) / last_price
    return change <= OUTLIER_PCT

# ================= INDICATORS ======================
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
                self.ema = sum(self._warm) / len(self._warm)  # SMA for seed
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

# ============= CANDLE AGGREGATION =================
def update_candle(price, ts_epoch):
    """
    รวม tick เป็นแท่ง 1 นาที
    return: แท่งที่ 'เพิ่งปิด' (dict) หรือ None
    """
    global _current_candle
    bucket = ts_epoch - (ts_epoch % CANDLE_SECONDS)
    if _current_candle["t0"] is None:
        _current_candle = {"open": price, "high": price, "low": price, "close": price, "t0": bucket}
        return None

    if bucket > _current_candle["t0"]:
        # ปิดแท่งเดิม
        closed = _current_candle.copy()
        candles.append(closed)
        # เริ่มแท่งใหม่
        _current_candle = {"open": price, "high": price, "low": price, "close": price, "t0": bucket}
        return closed
    else:
        # อัปเดตแท่งปัจจุบัน
        _current_candle["high"] = max(_current_candle["high"], price)
        _current_candle["low"]  = min(_current_candle["low"], price)
        _current_candle["close"] = price
        return None

# ================ ATR / VOL / ADAPTIVE ============
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
    """
    ส่วนเบี่ยงเบนมาตรฐานของ 'close' ล่าสุดเทียบกับราคาเฉลี่ย (30 แท่งหลัง)
    """
    if len(price_history) < 30:
        return None
    closes = np.array(list(price_history)[-30:], dtype=float)
    mean = closes.mean()
    if mean == 0:
        return None
    return closes.std(ddof=1) / mean

def get_dynamic_thresholds():
    """
    สร้างเกณฑ์แบบ adaptive:
    - MIN_EMA_GAP    = K_EMA_GAP   * (ATR / avg_close)
    - MIN_EMA_SLOPE  = K_EMA_SLOPE * (ATR / avg_close)
    - ATR_RATIO_TH   = K_ATR_RATIO * (std_ratio)
    """
    cl = list(candles)
    if len(cl) < max(ATR_PERIOD + 1, 20) or len(price_history) < 30:
        return None, None, None

    atr = compute_atr(cl, ATR_PERIOD)
    last20 = [c["close"] for c in cl[-20:]]
    avg_close = np.mean(last20) if last20 else None
    std_ratio = compute_std_ratio()  # อิง price_history

    if atr is None or avg_close in (None, 0) or std_ratio is None:
        return None, None, None

    min_ema_gap   = K_EMA_GAP   * (atr / avg_close)
    min_ema_slope = K_EMA_SLOPE * (atr / avg_close)
    atr_ratio_th  = K_ATR_RATIO * std_ratio
    return min_ema_gap, min_ema_slope, atr_ratio_th

def is_sideway_atr():
    """
    เดิม: ถ้าข้อมูลไม่ครบ → ถือว่า sideway (True) แล้วตัดสิทธิ์
    ใหม่: ถ้าข้อมูลยังไม่ครบ → ไม่ตัดสิทธิ์ (False) ให้ strong_trend_ok เป็นด่านหลัก
    """
    cl = list(candles)
    atr = compute_atr(cl, ATR_PERIOD)
    if atr is None or len(cl) < 20:
        return False  # ไม่ตัดสิทธิ์ช่วงต้น

    last20 = [c["close"] for c in cl[-20:]]
    avg_close = np.mean(last20) if last20 else 0
    min_gap, min_slope, atr_ratio_th = get_dynamic_thresholds()
    if atr_ratio_th is None or avg_close == 0:
        return False  # ไม่ตัดสิทธิ์ถ้ายังคำนวณไม่ครบ
    return (atr / avg_close) < atr_ratio_th

def is_low_liquidity():
    """
    Volume proxy: ถ้าความถี่ tick ใน TICK_FREQ_WIN_SEC ต่ำกว่าเกณฑ์ -> งดเทรด
    """
    now = time.time()
    while tick_times and now - tick_times[0] > TICK_FREQ_WIN_SEC:
        tick_times.popleft()
    return len(tick_times) < MIN_TICKS_PER_WIN

# =============== REGRESSION SLOPE ==================
def regression_slope(series, window=SLOPE_WINDOW):
    """
    คำนวณสโลป (Linear Regression) บนช่วงท้ายของ series
    คืนค่า slope ต่อ 1 step (index) — ภายนอกจะ normalize ด้วย avg_price
    """
    if len(series) < window:
        return None
    y = np.array(list(series)[-window:], dtype=float)
    x = np.arange(window, dtype=float)
    a, b = np.polyfit(x, y, 1)  # y = a*x + b
    return a

# ========== SIGNAL & QUALITY CHECKS ===============
def get_signal_score_and_direction(close_price):
    """
    อัปเดต EMA/MACD ด้วยราคาปิดแท่ง แล้วคืน score, direction และค่าประกอบการตัดสินใจ
    """
    # update EMAs
    ema_fast = ema_fast_calc.update(close_price)
    ema_mid  = ema_mid_calc.update(close_price)
    ema_slow = ema_slow_calc.update(close_price)

    macd_line, sig_line, hist = macd_calc.update(close_price)

    if not (ema_fast_calc.ready() and ema_mid_calc.ready() and ema_slow_calc.ready() and macd_calc.ready()):
        return 0, None, ema_fast, ema_mid, ema_slow, hist

    score = 0
    # EMA fast > mid
    if ema_fast > ema_mid:
        score += 1
    # EMA mid > slow
    if ema_mid > ema_slow:
        score += 1
    # MACD line > signal
    if macd_line > sig_line:
        score += 1
    # histogram > 0
    if hist > 0:
        score += 1
    # not sideway
    if not is_sideway_atr():
        score += 1

    # direction vote
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
    """
    เวอร์ชันใหม่ (มีเหตุผล debug):
    - ใช้ threshold แบบ adaptive (ATR/avg_close)
    - สโลป EMA(mid) จาก linear regression บน mid_series ช่วงท้าย
    - MACD histogram ต้องต่อเนื่องตามทิศ
    """
    reasons = []
    if avg_price is None or ema_mid is None or ema_slow is None:
        reasons.append("avg/ema None")
        logger.info("quality_fail: " + ", ".join(reasons))
        return False

    min_gap, min_slope, _atr_ratio_th = get_dynamic_thresholds()
    if min_gap is None or min_slope is None:
        reasons.append("adaptive thresholds not ready")
        logger.info("quality_fail: " + ", ".join(reasons))
        return False

    gap = abs(ema_mid - ema_slow) / avg_price
    if gap < min_gap:
        reasons.append(f"gap {gap:.5f} < {min_gap:.5f}")

    slope_raw = regression_slope(mid_series, window=SLOPE_WINDOW)
    if slope_raw is None:
        reasons.append("slope_raw None")
    else:
        slope_norm = abs(slope_raw) / avg_price
        if slope_norm < min_slope:
            reasons.append(f"slope {slope_norm:.5f} < {min_slope:.5f}")

    if len(hist_series) < 2:
        reasons.append("hist_len < 2")
    else:
        if bullish:
            if not all(h > 0 for h in hist_series[-2:]):
                reasons.append("hist not consecutively positive")
        else:
            if not all(h < 0 for h in hist_series[-2:]):
                reasons.append("hist not consecutively negative")

    if reasons:
        logger.info("quality_fail: " + ", ".join(reasons))
        return False
    return True

# =================== MARKET READY ==================
def market_ready():
    """
    เงื่อนไขขั้นต่ำให้ระบบเริ่มพิจารณาเทรด
    """
    return (
        ema_slow_calc.ready() and macd_calc.ready()
        and len(candles) >= 30
        and len(price_history) >= MIN_READY_BARS
    )

# ============== PROPOSAL & BUY FLOW ===============
def request_ticks(ws):
    ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))

def request_proposal(ws, contract_type):
    req = {
        "proposal": 1,
        "amount": AMOUNT,
        "basis": "stake",
        "contract_type": contract_type,  # "CALL" or "PUT"
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

# ================== RISK CONTROL ===================
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
            # cooldown สั้น ๆ ทุกครั้งที่แพ้
            pause_until = max(pause_until, time.time() + LOSS_COOLDOWN_SEC)
            # แพ้ติดหลายไม้ -> พักยาว
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

# ================== WATCH/PRECHECK =================
def process_closed_candle(ws, closed_candle):
    """
    เรียกเมื่อแท่งเพิ่งปิด: อัปเดตอินดี้, ทำ multi-bar confirm,
    ถ้าผ่าน -> เข้าสถานะ WATCH (แท่งถัดไป)
    """
    global pending_dir, watch_until, watch_hist_sign

    close_price = closed_candle["close"]
    with lock:
        price_history.append(close_price)

    score, signal, ema_fast, ema_mid, ema_slow, hist = get_signal_score_and_direction(close_price)

    # เก็บ series เพื่อเช็คสโลป/hist
    if ema_mid is not None:
        mid_series.append(ema_mid)
    if hist is not None:
        hist_buffer.append(hist)

    if signal and score >= SCORE_THRESHOLD:
        recent_dirs.append(signal)
        if len(recent_dirs) == CONFIRM_BARS and len(set(recent_dirs)) == 1:
            pending_dir = signal
            watch_until = closed_candle["t0"] + CANDLE_SECONDS + WATCH_WINDOW_SEC
            # บันทึกเครื่องหมาย histogram ตอนเริ่ม WATCH
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
    """
    เรียกทุก tick ระหว่างอยู่ในช่วง WATCH:
    - เช็ค market_ready + risk caps/active/liquidity
    - เช็คคุณภาพเทรนด์ (gap/slope/hist streak) + ATR ผ่าน
    - Reversal trap: histogram พลิกระหว่าง WATCH -> ยกเลิก
    - ผ่านทั้งหมด -> ขอ proposal ทันที
    """
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

    # Reversal trap
    if hist_buffer:
        latest_hist = hist_buffer[-1]
        latest_sign = 1 if latest_hist > 0 else (-1 if latest_hist < 0 else 0)
        if watch_hist_sign != 0 and latest_sign != 0 and latest_sign != watch_hist_sign and abs(latest_hist) >= REVERSAL_HIST_MIN:
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

# ================== TRADE LOGGING ==================
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

# =================== WS HANDLERS ===================
def on_open(ws):
    ws._backoff = 2  # reset backoff
    logger.info("✅ Connected")
    ws.send(json.dumps({"authorize": API_TOKEN}))

def on_message(ws, message):
    global active_contract_id, contract_sub_id, last_trade_time, pause_until, post_buy_until

    data = json.loads(message)

    # ---------- AUTH ----------
    if data.get("msg_type") == "authorize":
        if "error" in data:
            logger.error(f"❌ Auth error: {data['error']}")
            return
        logger.info("✅ Authorized")
        request_ticks(ws)
        return

    # ---------- ERROR ----------
    if data.get("msg_type") == "error":
        err = data.get("error", {}).get("message")
        logger.error(f"❌ API Error: {err}")
        return

    # ---------- TICKS ----------
    if data.get("msg_type") == "tick":
        tick = data["tick"]
        price = float(tick["quote"])
        ts = int(tick["epoch"])

        # track tick frequency
        tick_times.append(time.time())

        # กรอง tick กระโดด
        last_tick_price = _current_candle["close"] if _current_candle["close"] is not None else None
        if not is_valid_tick(price, last_tick_price):
            logger.warning(f"⚠️ Outlier tick filtered: {last_tick_price} -> {price}")
            return

        closed_candle = update_candle(price, ts)
        if closed_candle:
            process_closed_candle(ws, closed_candle)

        # ถ้าอยู่ใน WATCH: ลอง precheck + ขอ proposal
        maybe_precheck_and_request(ws)

        # ถ้ามี contract ค้าง แต่ไม่มีสถานะวิ่ง ให้ใช้ timeout ป้องกันแขวน
        if active_contract_id is not None and (time.time() - last_trade_time > CONTRACT_TIMEOUT_SEC):
            logger.warning(f"⚠️ Contract timeout {CONTRACT_TIMEOUT_SEC}s. Force reset.")
            with lock:
                active_contract_id = None
            if contract_sub_id:
                ws.send(json.dumps({"forget": contract_sub_id}))
                contract_sub_id = None
        return

    # ---------- PROPOSAL ----------
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

        # Double-check ตลาด ณ เวลานี้ (ใช้ค่าล่าสุดของอินดี้จากแท่งปิด)
        with lock:
            avg_price = np.mean(list(price_history)[-20:]) if len(price_history) >= 20 else None
            ema_mid = ema_mid_calc.value()
            ema_slow = ema_slow_calc.value()
            atr_ok = not is_sideway_atr()

        bullish = (ctype == "CALL")
        strong_ok = strong_trend_ok(avg_price, ema_mid, ema_slow, list(hist_buffer), bullish) and atr_ok

        if rr >= MIN_PAYOUT and strong_ok:
            buy_from_proposal(ws, quote["id"])
        else:
            logger.info(f"❎ Skip proposal: RR={rr:.2f} (need {MIN_PAYOUT}) strong_ok={strong_ok}")
        return

    # ---------- BUY CONFIRMED ----------
    if data.get("msg_type") == "buy":
        contract_id = data["buy"]["contract_id"]
        with lock:
            active_contract_id = contract_id
            last_trade_time = time.time()
        logger.info(f"📈 Buy confirmed. Contract ID: {contract_id}")

        # subscribe สถานะสัญญา
        ws.send(json.dumps({
            "proposal_open_contract": 1,
            "contract_id": contract_id,
            "subscribe": 1
        }))
        return

    # ---------- CONTRACT STATUS ----------
    if data.get("msg_type") == "proposal_open_contract":
        poc = data["proposal_open_contract"]

        # เก็บ sub id ไว้เพื่อ forget ตอนเสร็จ
        if "subscription" in poc and poc["subscription"] and "id" in poc["subscription"]:
            subid = poc["subscription"]["id"]
            if subid:
                contract_sub_id = subid

        # จบสัญญา
        if poc.get("is_sold"):
            profit = float(poc.get("profit", 0) or 0)
            result = "WIN" if profit > 0 else "LOSS"
            update_result(result, profit)

            # ล้างสถานะ
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

# ================== FLASK STATUS ===================
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

# ===================== MAIN ========================
if __name__ == "__main__":
    logger.info("🤖 Starting Deriv Trading Bot (adaptive confirmed-entry, tuned)…")
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=10000)
