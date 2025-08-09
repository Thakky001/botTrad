import json
import time
import numpy as np
import threading
import os
from websocket import WebSocketApp
from flask import Flask, jsonify

# ============ CONFIG ============
API_TOKEN = "C82t0gtcRoQv99X"
amount = 100
symbol = "R_100"
duration = 1  # 1 นาที
required_confidence = 1  # ยังคง 1 (ใช้ score threshold แทน)
score_threshold = 2.0    # ถ้าคะแนนรวม >= ค่านี้ จะเข้าเทรด
max_price = 200
max_consecutive_losses = 3
pause_duration_sec = 300  # หยุด 5 นาทีหลังแพ้ติด
min_time_between_trades = 5  # วินาที ขั้นต่ำระหว่างการเปิดเทรด  (ช่วยลด noise)
# ================================

# สถานะ
price_history = []
last_signal = None
signal_confidence = 0
active_contract_id = None
total_trades = 0
wins = 0
losses = 0
consecutive_losses = 0
pause_until = 0
last_trade_time = 0

# === Flask API ===
app = Flask(__name__)

@app.route("/")
def status():
    return jsonify({
        "status": "running",
        "symbol": symbol,
        "trades": total_trades,
        "wins": wins,
        "losses": losses,
        "consecutive_losses": consecutive_losses,
        "last_trade_time": last_trade_time
    })

@app.route('/favicon.ico')
def favicon():
    return '', 204

# --- EMA ---
def ema(values, period):
    if len(values) < period:
        return None
    weights = np.exp(np.linspace(-1., 0., period))
    weights /= weights.sum()
    conv = np.convolve(values, weights, mode='valid')
    return float(conv[-1])

# --- RSI ---
def rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    deltas = np.diff(prices[-(period+1):])
    ups = deltas[deltas > 0].sum() / period
    downs = -deltas[deltas < 0].sum() / period
    if downs == 0:
        return 100.0
    rs = ups / downs
    return 100.0 - (100.0 / (1.0 + rs))

# --- Bollinger Bands ---
def bollinger_bands(prices, period=20):
    if len(prices) < period:
        return None, None, None
    sma = np.mean(prices[-period:])
    std = np.std(prices[-period:])
    upper = sma + 2 * std
    lower = sma - 2 * std
    return float(upper), float(sma), float(lower)

# --- MACD ---
def calculate_macd(prices):
    # ต้องการอย่างน้อย 35 candle ตามเดิม
    if len(prices) < 35:
        return None, None
    # คำนวณ EMA12 และ EMA26 จากช่วงล่าสุด
    ema12 = ema(prices[-(26+12):], 12) if len(prices) >= 26+12 else ema(prices[-26:], 12)
    ema26 = ema(prices[-26:], 26)
    if ema12 is None or ema26 is None:
        return None, None
    macd_line = ema12 - ema26

    # สร้าง macd history (9 ค่า) เพื่อหา signal line (EMA9 ของ macd_history)
    macd_history = []
    # สร้างย้อนหลังให้ได้ 9 ค่า (ถ้าเป็นไปได้)
    for offset in range(9, 0, -1):
        start = -offset - 26
        end = -offset
        if abs(start) <= len(prices):
            sub = prices[start:end]
            e12 = ema(sub, 12)
            e26 = ema(sub, 26)
            if e12 is not None and e26 is not None:
                macd_history.append(e12 - e26)
    macd_history.append(macd_line)
    if len(macd_history) < 9:
        return None, None
    signal_line = ema(macd_history, 9)
    return float(macd_line), float(signal_line)

# --- Sideway Filter ---
def is_sideway():
    if len(price_history) < 20:
        return True
    window = price_history[-20:]
    recent_range = max(window) - min(window)
    avg_price = np.mean(window)
    volatility = recent_range / avg_price if avg_price != 0 else 0
    print(f"🚛 Sideway Check: Range={recent_range:.6f}, Volatility={volatility:.6f}")
    # ปรับ threshold แบบผ่อนกว่าเดิม (ถ้าต้องการให้เข้าบ่อยขึ้น ให้เพิ่ม threshold)
    return volatility < 0.0025

# --- Trend Bias ---
def get_trend_bias():
    if len(price_history) < 60:
        return None
    ema_20 = ema(price_history[-60:], 20)
    ema_50 = ema(price_history[-60:], 50)
    if ema_20 is None or ema_50 is None:
        return None
    if ema_20 > ema_50:
        return "UP"
    elif ema_20 < ema_50:
        return "DOWN"
    return None

# --- Scoring Trade Signal ---
def get_trade_signal_with_score():
    """
    คืนค่า (signal, score, details)
    signal: "CALL" / "PUT" / None
    score: คะแนนรวม (float)
    details: dict ของคะแนนแยกส่วน (เพื่อ debug)
    """
    if len(price_history) < 35:
        return None, 0.0, {}

    ema_fast = ema(price_history[-20:], 5)
    ema_slow = ema(price_history[-20:], 20)
    macd_line, signal_line = calculate_macd(price_history)
    rsi_value = rsi(price_history)
    upper, sma, lower = bollinger_bands(price_history)
    trend = get_trend_bias()
    current_price = price_history[-1]

    details = {
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "macd_line": macd_line,
        "signal_line": signal_line,
        "rsi": rsi_value,
        "upper": upper,
        "lower": lower,
        "trend": trend,
        "price": current_price
    }

    # ถ้ามาส่วนสำคัญขาด ให้ return None
    if None in (ema_fast, ema_slow, macd_line, signal_line, rsi_value, upper, lower):
        return None, 0.0, details

    # กำหนด score ทีละส่วน (น้ำหนักสามารถปรับได้)
    score = 0.0

    # MACD/Signal: แกนหลัก (weight 1.5)
    if macd_line > signal_line:
        score += 1.5
        macd_dir = "UP"
    else:
        score -= 1.5
        macd_dir = "DOWN"
    details["macd_dir"] = macd_dir

    # EMA alignment: weight 1.0
    if ema_fast > ema_slow:
        score += 1.0
        ema_dir = "UP"
    else:
        score -= 1.0
        ema_dir = "DOWN"
    details["ema_dir"] = ema_dir

    # RSI: ถ้าอยู่ในช่วงกลาง (30-70) ให้คะแนนบวกเล็กน้อย, ถ้า oversold/overbought ให้ลดคะแนน
    if 30 < rsi_value < 70:
        score += 0.5
    elif rsi_value <= 30:
        score += 0.2  # oversold แต่ยังเป็นโอกาส CALL
    elif rsi_value >= 70:
        score += 0.0  # overbought (ไม่เพิ่ม)
    details["rsi_score"] = score

    # Bollinger: ถ้าราคาระหว่าง band แต่ไม่ชิด band มาก ให้คะแนนบวก
    band_width = upper - lower if (upper is not None and lower is not None) else 0
    if band_width > 0:
        dist_to_upper = upper - current_price
        dist_to_lower = current_price - lower
        # ถ้าอยู่กลาง band (ห่างจาก band มากพอ) ให้คะแนน
        if dist_to_upper > 0.25 * band_width and dist_to_lower > 0.25 * band_width:
            score += 0.4
        else:
            # ถ้ชิด upper มาก => ลดโอกาส CALL, ถชิด lower => ลดโอกาส PUT
            score += 0.1
    details["band_width"] = band_width

    # Trend bias: ถ้าสอดคล้องกับ macd/ema ให้เพิ่มคะแนนเล็กน้อย
    if trend is not None:
        if (trend == "UP" and macd_dir == "UP" and ema_dir == "UP"):
            score += 0.4
        elif (trend == "DOWN" and macd_dir == "DOWN" and ema_dir == "DOWN"):
            score += 0.4
        else:
            score -= 0.2

    details["score"] = score

    # ตัดสิน CALL / PUT ตามทิศทาง MACD+EMA เป็นหลัก
    if macd_dir == "UP" and ema_dir == "UP":
        signal = "CALL"
    elif macd_dir == "DOWN" and ema_dir == "DOWN":
        signal = "PUT"
    else:
        signal = None

    return signal, float(score), details

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
    print("🚀 Trade sent:", contract_type, "at", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_trade_time)))

# --- อัปเดตผล ---
def update_result(result, profit):
    global total_trades, wins, losses, consecutive_losses, pause_until
    total_trades += 1

    if result == "WIN":
        wins += 1
        consecutive_losses = 0
    else:
        losses += 1
        consecutive_losses += 1
        if consecutive_losses >= max_consecutive_losses:
            pause_until = time.time() + pause_duration_sec
            print(f"🛑 Too many losses — Pausing for {pause_duration_sec//60} mins.")

    win_rate = (wins / total_trades) * 100 if total_trades > 0 else 0

    print("\n===== 📊 SUMMARY AFTER TRADE =====")
    print(f"📌 Result         : {result}")
    print(f"💰 Profit/Loss   : {profit:.2f} USD")
    print(f"🧮 Total Trades  : {total_trades}")
    print(f"✅ Wins          : {wins}")
    print(f"❌ Losses        : {losses}")
    print(f"📈 Win Rate      : {win_rate:.2f}%")
    print(f"⚠️ Consecutive L : {consecutive_losses}")
    print("=================================\n")

# --- WebSocket Events ---
def on_open(ws):
    print("✅ Connected!")
    ws.send(json.dumps({"authorize": API_TOKEN}))

def on_message(ws, message):
    global last_signal, signal_confidence, active_contract_id, last_trade_time
    data = json.loads(message)

    if data.get("msg_type") == "authorize":
        print("✅ Authorized!")
        ws.send(json.dumps({"ticks": symbol}))

    elif data.get("msg_type") == "tick":
        if time.time() < pause_until:
            remaining = int(pause_until - time.time())
            print(f"⏸️ Pausing... Resume in {remaining} seconds")
            return

        price = float(data["tick"]["quote"])
        price_history.append(price)
        if len(price_history) > max_price:
            price_history.pop(0)

        now = time.time()
        print(f"📉 Tick: {price}  (history={len(price_history)})")

        # หลีกเลี่ยงเทรดถี่เกินไป
        if now - last_trade_time < min_time_between_trades:
            # print(f"⏳ Cooldown: {now - last_trade_time:.2f}s since last trade")
            return

        signal, score, details = get_trade_signal_with_score()
        print(f"🔎 Signal={signal}, Score={score:.2f}, Details={details}")

        if signal:
            # เพิ่มเงื่อนไข sideway check แบบผ่อน
            if is_sideway():
                print("⛔ Skip: Sideway Market")
                return

            # ถ้าคะแนนถึง threshold ให้เข้าเทรด
            if score >= score_threshold and active_contract_id is None:
                # ใช้ required_confidence เพื่อให้สามารถรอ confirmation ซ้ำ (แต่ default =1)
                if signal == last_signal:
                    signal_confidence += 1
                else:
                    signal_confidence = 1
                    last_signal = signal

                if signal_confidence >= required_confidence:
                    send_trade(ws, signal)
                    signal_confidence = 0
            else:
                print(f"ℹ️ Score below threshold ({score:.2f} < {score_threshold}) — Not trading")
        else:
            signal_confidence = 0
            last_signal = None

    elif data.get("msg_type") == "buy":
        contract_id = data["buy"]["contract_id"]
        active_contract_id = contract_id
        print("📈 Buy Confirmed:", json.dumps(data, indent=2))
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
            active_contract_id = None  # เคลียร์หลังสัญญาปิด

    elif data.get("msg_type") == "error":
        print("❌ Error:", data.get("error", {}).get("message"))

def on_error(ws, error):
    print("❌ Error:", error)

def on_close(ws, code, reason):
    print(f"🔌 Disconnected: {code} | {reason}")
    print("🔁 Reconnecting in 10 sec...")
    time.sleep(10)
    run_bot()

# --- Start ---
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
    threading.Thread(target=run_bot).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
