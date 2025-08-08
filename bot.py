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
required_confidence = 2  # ลดจาก 3 เป็น 2 เพื่อเข้าเทรดบ่อยขึ้น
max_price = 100  # ความยาวของประวัติราคา
max_consecutive_losses = 3
pause_duration_sec = 300  # หยุด 5 นาทีหลังแพ้ติด
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
        "consecutive_losses": consecutive_losses
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
    return np.convolve(values, weights, mode='valid')[-1]

# --- RSI ---
def rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    deltas = np.diff(prices[-(period+1):])
    ups = deltas[deltas > 0].sum() / period
    downs = -deltas[deltas < 0].sum() / period
    if downs == 0:
        return 100
    rs = ups / downs
    return 100 - (100 / (1 + rs))

# --- Bollinger Bands ---
def bollinger_bands(prices, period=20):
    if len(prices) < period:
        return None, None, None
    sma = np.mean(prices[-period:])
    std = np.std(prices[-period:])
    upper = sma + 2 * std
    lower = sma - 2 * std
    return upper, sma, lower

# --- MACD ---
def calculate_macd(prices):
    if len(prices) < 35:
        return None, None
    ema12 = ema(prices[-26:], 12)
    ema26 = ema(prices[-26:], 26)
    if ema12 is None or ema26 is None:
        return None, None
    macd_line = ema12 - ema26
    macd_history = []
    for i in range(9, 0, -1):
        sub_prices = prices[-i-26:-i]
        e12 = ema(sub_prices, 12)
        e26 = ema(sub_prices, 26)
        # แก้ไขตรงนี้: เช็คค่า None อย่างถูกต้อง
        if e12 is not None and e26 is not None:
            macd_history.append(e12 - e26)
    macd_history.append(macd_line)
    if len(macd_history) < 9:
        return None, None
    signal_line = ema(macd_history, 9)
    return macd_line, signal_line

# --- Sideway Filter ---
def is_sideway():
    if len(price_history) < 20:
        return True
    recent_range = max(price_history[-20:]) - min(price_history[-20:])
    avg_price = np.mean(price_history[-20:])
    volatility = recent_range / avg_price
    # เพิ่ม log volatility
    print(f"🚛 Sideway Check: Range={recent_range:.5f}, Volatility={volatility:.5f}")
    # ปรับ threshold เป็น 0.004 เพื่อให้ฟิลเตอร์ไม่เข้มเกินไป
    return volatility < 0.004

# --- Trend Filter ---
def get_trend_bias():
    ema_50 = ema(price_history[-60:], 50)
    ema_20 = ema(price_history[-60:], 20)
    if ema_20 and ema_50:
        print(f"\U0001f4c8 Trend Check: EMA20={ema_20}, EMA50={ema_50}")
        if ema_20 > ema_50:
            return "UP"
        elif ema_20 < ema_50:
            return "DOWN"
    return None

# --- Trade Signal ---
def get_trade_signal():
    if len(price_history) < 35:
        return None
    ema_fast = ema(price_history[-20:], 5)
    ema_slow = ema(price_history[-20:], 20)
    macd_line, signal_line = calculate_macd(price_history)
    rsi_value = rsi(price_history)
    upper, sma, lower = bollinger_bands(price_history)

    print(f"\U0001f4ca EMA5={ema_fast}, EMA20={ema_slow}, MACD={macd_line}, Signal={signal_line}, RSI={rsi_value}")

    if None in (ema_fast, ema_slow, macd_line, signal_line, rsi_value, upper, lower):
        return None

    if macd_line > signal_line and ema_fast > ema_slow and rsi_value < 70 and price_history[-1] < upper:
        return "CALL"
    elif macd_line < signal_line and ema_fast < ema_slow and rsi_value > 30 and price_history[-1] > lower:
        return "PUT"
    return None

# --- ส่งคำสั่งเทรด ---
def send_trade(ws, contract_type):
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
    print("\U0001f680 Trade sent:", contract_type)

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
    global last_signal, signal_confidence, active_contract_id
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

        print(f"📉 Tick: {price}")

        signal = get_trade_signal()

        if signal:
            if is_sideway():
                print("⚠️ Market is Sideway — Skipping.")
                return

            trend = get_trend_bias()
            # --- ถ้าอยากลองปิดฟิลเตอร์เทรนด์ให้ comment บรรทัดนี้ ---
            if (trend == "UP" and signal == "PUT") or (trend == "DOWN" and signal == "CALL"):
                print(f"⚠️ Trend Conflict ({trend}) — Skipping.")
                return

            if signal == last_signal:
                signal_confidence += 1
            else:
                signal_confidence = 1
                last_signal = signal

            if signal_confidence >= required_confidence and active_contract_id is None:
                send_trade(ws, signal)
                signal_confidence = 0
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
        print("❌ Error:", data["error"]["message"])

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
