import json
import time
import numpy as np
import threading
import logging
from collections import deque
from websocket import WebSocketApp
from flask import Flask, jsonify

# ============ CONFIG ============
API_TOKEN = "C82t0gtcRoQv99X"
amount = 100
symbol = "R_100"
duration = 1  # นาที
max_price = 150  # ลดจาก 200 เหลือ 150
max_consecutive_losses = 3
pause_duration_sec = 300  # หยุด 5 นาทีหลังแพ้ติด
min_time_between_trades = 5  # วินาที ขั้นต่ำระหว่างเปิดเทรด
contract_timeout = 120  # 2 นาที
score_threshold = 3  # ลดจาก 4 เป็น 3
# ================================

# --- Logger setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s',
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger()

# --- สถานะและล็อก ---
price_history = deque(maxlen=max_price)
lock = threading.Lock()  # สำหรับ thread-safe

last_signal = None
signal_confidence = 0
active_contract_id = None
total_trades = 0
wins = 0
losses = 0
consecutive_losses = 0
pause_until = 0
last_trade_time = 0

# === NEW EMA Calculator Class ===
class EMACalculator:
    def __init__(self, period):
        self.period = period
        self.multiplier = 2 / (period + 1)
        self.ema = None
        self.warmup_data = []
        
    def update(self, price):
        if self.ema is None:
            self.warmup_data.append(price)
            if len(self.warmup_data) >= self.period:
                # ใช้ SMA สำหรับ EMA แรก
                self.ema = sum(self.warmup_data) / len(self.warmup_data)
                self.warmup_data = None
                logger.info(f"EMA {self.period} initialized with SMA: {self.ema:.5f}")
            return self.ema
        
        self.ema = (price * self.multiplier) + (self.ema * (1 - self.multiplier))
        return self.ema
    
    def get_value(self):
        return self.ema
    
    def is_ready(self):
        return self.ema is not None

# สร้าง EMA calculators
ema_fast_calc = EMACalculator(5)
ema_mid_calc = EMACalculator(20)
ema_slow_calc = EMACalculator(50)

# Flask API
app = Flask(__name__)

@app.route("/")
def status():
    with lock:
        status_str = "paused" if time.time() < pause_until else "running"
        return jsonify({
            "status": status_str,
            "symbol": symbol,
            "trades": total_trades,
            "wins": wins,
            "losses": losses,
            "consecutive_losses": consecutive_losses,
            "last_trade_time": last_trade_time,
            "ema_status": {
                "fast_ready": ema_fast_calc.is_ready(),
                "mid_ready": ema_mid_calc.is_ready(),
                "slow_ready": ema_slow_calc.is_ready()
            }
        })

@app.route('/favicon.ico')
def favicon():
    return '', 204

# --- MACD & Signal Line (improved) ---
class MACDCalculator:
    def __init__(self):
        self.ema12_calc = EMACalculator(12)
        self.ema26_calc = EMACalculator(26)
        self.signal_line_calc = EMACalculator(9)

    def update(self, price):
        ema12 = self.ema12_calc.update(price)
        ema26 = self.ema26_calc.update(price)
        
        if ema12 is None or ema26 is None:
            return None, None, None
            
        macd_line = ema12 - ema26
        signal_line = self.signal_line_calc.update(macd_line)
        
        if signal_line is None:
            return macd_line, None, None
            
        histogram = macd_line - signal_line
        return macd_line, signal_line, histogram
    
    def is_ready(self):
        return (self.ema12_calc.is_ready() and 
                self.ema26_calc.is_ready() and 
                self.signal_line_calc.is_ready())

macd_calculator = MACDCalculator()

# --- Improved Sideway Filter ---
def is_sideway():
    with lock:
        if len(price_history) < 30:
            return True
        window = list(price_history)[-30:]  # เพิ่มจาก 20 เป็น 30
    
    recent_range = max(window) - min(window)
    avg_price = np.mean(window)
    volatility = recent_range / avg_price if avg_price != 0 else 0
    
    # ปรับ threshold จาก 0.001 เป็น 0.0015
    return volatility < 0.0015

# --- สัญญาณรวมคะแนน (ปรับปรุง) ---
def get_signal_score_and_direction():
    with lock:
        if len(price_history) < 60:
            return 0, None  # ข้อมูลไม่พอ
        
        current_price = price_history[-1]

    # Update EMA calculators
    ema_fast = ema_fast_calc.update(current_price)
    ema_mid = ema_mid_calc.update(current_price)
    ema_slow = ema_slow_calc.update(current_price)

    macd_line, signal_line, histogram = macd_calculator.update(current_price)

    # ตรวจสอบว่าข้อมูลพร้อมหรือไม่
    if not all([ema_fast_calc.is_ready(), ema_mid_calc.is_ready(), 
                ema_slow_calc.is_ready(), macd_calculator.is_ready()]):
        return 0, None

    score = 0
    signals = []

    # 1. EMA trend alignment
    if ema_fast > ema_mid:
        score += 1
        signals.append("EMA fast > mid (UP)")
    else:
        signals.append("EMA fast <= mid (DOWN)")

    # 2. EMA medium trend
    if ema_mid > ema_slow:
        score += 1
        signals.append("EMA mid > slow (UP)")
    else:
        signals.append("EMA mid <= slow (DOWN)")

    # 3. MACD line vs Signal line
    if macd_line > signal_line:
        score += 1
        signals.append("MACD line > Signal (UP)")
    else:
        signals.append("MACD line <= Signal (DOWN)")

    # 4. Histogram momentum
    if histogram > 0:
        score += 1
        signals.append("Histogram > 0 (UP)")
    else:
        signals.append("Histogram <= 0 (DOWN)")

    # 5. Market condition (not sideway)
    if not is_sideway():
        score += 1
        signals.append("Not sideway")
    else:
        signals.append("Sideway detected")

    # นับสัญญาณ bullish/bearish
    bullish_count = sum(1 for s in signals[:4] if "(UP)" in s)
    bearish_count = 4 - bullish_count

    # กำหนด direction
    if bullish_count >= 3 and score >= score_threshold:
        direction = "CALL"
    elif bearish_count >= 3 and score >= score_threshold:
        direction = "PUT"
    else:
        direction = None

    # Log สำหรับ debug
    logger.debug(f"EMA: Fast={ema_fast:.5f}, Mid={ema_mid:.5f}, Slow={ema_slow:.5f}")
    logger.debug(f"MACD: Line={macd_line:.5f}, Signal={signal_line:.5f}, Hist={histogram:.5f}")
    logger.debug(f"Signals: {signals}")
    logger.debug(f"Score: {score}/{len(signals)}, Direction: {direction}")
    
    return score, direction

# --- Filter tick outlier (ปรับปรุง) ---
def is_valid_tick(new_price):
    with lock:
        if not price_history:
            return True
        last_price = price_history[-1]
    
    change = abs(new_price - last_price) / last_price
    # ปรับจาก 10% เป็น 5% เพื่อกรองได้ดีขึ้น
    if change > 0.05:
        logger.warning(f"Outlier detected: price jump {change*100:.2f}% from {last_price} to {new_price}")
        return False
    return True

# --- ส่งคำสั่งเทรด ---
def send_trade(ws, contract_type):
    global last_trade_time
    trade = {
        "buy": 1,
        "price": amount,
        "parameters": {
            "amount": amount,
            "basis": "stake",
            "contract_type": contract_type,
            "currency": "USD",
            "duration": duration,
            "duration_unit": "m",
            "symbol": symbol
        }
    }
    ws.send(json.dumps(trade))
    last_trade_time = time.time()
    logger.info(f"🚀 Trade sent: {contract_type} at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_trade_time))}")

# --- อัปเดตผล ---
def update_result(result, profit):
    global total_trades, wins, losses, consecutive_losses, pause_until
    with lock:
        total_trades += 1

        if result == "WIN":
            wins += 1
            consecutive_losses = 0
        else:
            losses += 1
            consecutive_losses += 1
            if consecutive_losses >= max_consecutive_losses:
                pause_until = time.time() + pause_duration_sec
                logger.warning(f"🛑 Too many losses — Pausing for {pause_duration_sec//60} mins.")

        win_rate = (wins / total_trades) * 100 if total_trades > 0 else 0

    logger.info("\n===== 📊 SUMMARY AFTER TRADE =====")
    logger.info(f"📌 Result         : {result}")
    logger.info(f"💰 Profit/Loss   : {profit:.2f} USD")
    logger.info(f"🧮 Total Trades  : {total_trades}")
    logger.info(f"✅ Wins          : {wins}")
    logger.info(f"❌ Losses        : {losses}")
    logger.info(f"📈 Win Rate      : {win_rate:.2f}%")
    logger.info(f"⚠️ Consecutive L : {consecutive_losses}")
    logger.info("=================================\n")

# --- WebSocket Events ---
def on_open(ws):
    logger.info("✅ Connected!")
    ws.send(json.dumps({"authorize": API_TOKEN}))

def on_message(ws, message):
    global last_signal, signal_confidence, active_contract_id, last_trade_time

    data = json.loads(message)
    current_time = time.time()

    # ตรวจสอบ contract timeout
    if active_contract_id is not None:
        if current_time - last_trade_time > contract_timeout:
            logger.warning(f"⚠️ Contract timeout reached ({contract_timeout}s), resetting active_contract_id")
            active_contract_id = None
            signal_confidence = 0
            last_signal = None

    if data.get("msg_type") == "authorize":
        logger.info("✅ Authorized!")
        ws.send(json.dumps({"ticks": symbol}))

    elif data.get("msg_type") == "tick":
        # ตรวจสอบ pause status
        if time.time() < pause_until:
            remaining = int(pause_until - time.time())
            if remaining % 60 == 0:  # log ทุกนาที
                logger.info(f"⏸️ Pausing... Resume in {remaining} seconds")
            return

        price = float(data["tick"]["quote"])

        # กรอง outlier ticks
        if not is_valid_tick(price):
            return

        with lock:
            price_history.append(price)

        # ตรวจสอบเวลาระหว่างเทรด
        now = time.time()
        if now - last_trade_time < min_time_between_trades:
            return

        # คำนวณสัญญาณ
        score, signal = get_signal_score_and_direction()

        # ตัดสินใจเทรด
        if signal and score >= score_threshold and active_contract_id is None:
            if signal == last_signal:
                signal_confidence += 1
            else:
                signal_confidence = 1
                last_signal = signal

            # เทรดทันทีเมื่อได้สัญญาณ (confidence >= 1)
            if signal_confidence >= 1:
                logger.info(f"📊 Signal: {signal}, Score: {score}, Confidence: {signal_confidence}")
                send_trade(ws, signal)
                signal_confidence = 0
        else:
            # reset signal ถ้าไม่ได้เงื่อนไข
            if signal != last_signal:
                signal_confidence = 0
                last_signal = None

    elif data.get("msg_type") == "buy":
        contract_id = data["buy"]["contract_id"]
        active_contract_id = contract_id
        logger.info(f"📈 Buy Confirmed - Contract ID: {contract_id}")
        ws.send(json.dumps({
            "proposal_open_contract": 1,
            "contract_id": contract_id
        }))

    elif data.get("msg_type") == "proposal_open_contract":
        contract = data["proposal_open_contract"]
        if contract.get("is_sold"):
            profit = contract.get("profit", 0)
            result = "WIN" if profit > 0 else "LOSS"
            update_result(result, profit)
            active_contract_id = None

    elif data.get("msg_type") == "error":
        logger.error(f"❌ Error: {data.get('error', {}).get('message')}")
        # Reset active contract on error
        if active_contract_id:
            active_contract_id = None

def on_error(ws, error):
    logger.error(f"❌ WebSocket Error: {error}")

def on_close(ws, code, reason):
    logger.warning(f"🔌 Disconnected: Code={code}, Reason={reason}")
    logger.info("🔁 Reconnecting in 10 seconds...")

    def reconnect():
        run_bot()
    timer = threading.Timer(10, reconnect)
    timer.start()

# --- Start Bot ---
def run_bot():
    ws = WebSocketApp(
        "wss://ws.derivws.com/websockets/v3?app_id=1089",
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    ws.run_forever()

# --- Run bot + Flask ---
if __name__ == '__main__':
    logger.info("🤖 Starting Trading Bot with Improved EMA Calculation...")
    threading.Thread(target=run_bot, daemon=True).start()
    port = 10000
    app.run(host='0.0.0.0', port=port)