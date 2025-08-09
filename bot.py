import json
import time
import numpy as np
import threading
import os
from websocket import WebSocketApp
from flask import Flask, jsonify
from datetime import datetime
import logging

# ============ CONFIG ============
API_TOKEN = "C82t0gtcRoQv99X"
amount = 100
symbol = "R_100"
duration = 1  # 1 นาที
required_confidence = 2  # ต้องการสัญญาณยืนยัน 2 ครั้ง
score_threshold = 3.0    # เพิ่มเป็น 3.0 เพื่อให้เข้มงวดกว่า
max_price = 500          # เพิ่มข้อมูลราคาเก็บไว้มากขึ้น
max_consecutive_losses = 2  # ลดลงเป็น 2 เพื่อจัดการความเสี่ยง
pause_duration_sec = 600    # หยุด 10 นาทีหลังแพ้ติด
min_time_between_trades = 30  # เพิ่มเป็น 30 วินาที เพื่อลด noise
contract_timeout = 180   # เพิ่มเป็น 3 นาที
min_history_length = 100 # จำนวนข้อมูลราคาขั้นต่ำก่อนเทรด
volatility_threshold = 0.002  # ปรับค่า volatility
# ================================

# สถานะ
price_history = []
signal_history = []  # เก็บประวัติสัญญาณ
last_signal = None
signal_confidence = 0
active_contract_id = None
total_trades = 0
wins = 0
losses = 0
consecutive_losses = 0
pause_until = 0
last_trade_time = 0
last_signal_time = 0

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# === Flask API ===
app = Flask(__name__)

@app.route("/")
def status():
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    return jsonify({
        "status": "running",
        "symbol": symbol,
        "trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": f"{win_rate:.2f}%",
        "consecutive_losses": consecutive_losses,
        "last_trade_time": datetime.fromtimestamp(last_trade_time).strftime("%Y-%m-%d %H:%M:%S") if last_trade_time else None,
        "is_paused": time.time() < pause_until,
        "price_history_length": len(price_history),
        "active_contract": active_contract_id is not None
    })

@app.route('/favicon.ico')
def favicon():
    return '', 204

# --- Enhanced EMA ---
def ema(values, period):
    if len(values) < period:
        return None
    alpha = 2 / (period + 1)
    ema_val = values[0]
    for price in values[1:]:
        ema_val = alpha * price + (1 - alpha) * ema_val
    return float(ema_val)

# --- Enhanced RSI ---
def rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    
    deltas = np.diff(prices[-(period+1):])
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    
    avg_gain = np.mean(gains) if len(gains) > 0 else 0
    avg_loss = np.mean(losses) if len(losses) > 0 else 0
    
    if avg_loss == 0:
        return 100.0
    
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

# --- Enhanced Bollinger Bands ---
def bollinger_bands(prices, period=20, std_dev=2):
    if len(prices) < period:
        return None, None, None
    
    recent_prices = prices[-period:]
    sma = np.mean(recent_prices)
    std = np.std(recent_prices, ddof=1)
    
    upper = sma + std_dev * std
    lower = sma - std_dev * std
    
    return float(upper), float(sma), float(lower)

# --- Enhanced MACD ---
def calculate_macd(prices, fast=12, slow=26, signal=9):
    if len(prices) < slow + signal:
        return None, None, None
    
    ema_fast = ema(prices[-fast:], fast) if len(prices) >= fast else None
    ema_slow = ema(prices[-slow:], slow) if len(prices) >= slow else None
    
    if ema_fast is None or ema_slow is None:
        return None, None, None
    
    macd_line = ema_fast - ema_slow
    
    # Calculate signal line
    macd_history = []
    for i in range(max(0, len(prices) - slow - signal + 1), len(prices) - slow + 1):
        if i + slow <= len(prices):
            sub_prices = prices[i:i+slow]
            if len(sub_prices) >= slow:
                ema_f = ema(sub_prices[-fast:], fast) if len(sub_prices) >= fast else None
                ema_s = ema(sub_prices, slow)
                if ema_f is not None and ema_s is not None:
                    macd_history.append(ema_f - ema_s)
    
    macd_history.append(macd_line)
    
    if len(macd_history) < signal:
        return macd_line, None, None
    
    signal_line = ema(macd_history[-signal:], signal)
    histogram = macd_line - signal_line if signal_line is not None else None
    
    return float(macd_line), float(signal_line), float(histogram) if histogram else None

# --- Advanced Sideway Detection ---
def is_sideway():
    if len(price_history) < 50:
        return True
    
    # ใช้หลายกรอบเวลา
    short_window = price_history[-20:]
    medium_window = price_history[-30:]
    long_window = price_history[-50:]
    
    short_range = max(short_window) - min(short_window)
    medium_range = max(medium_window) - min(medium_window)
    long_range = max(long_window) - min(long_window)
    
    avg_price = np.mean(short_window)
    short_volatility = short_range / avg_price if avg_price != 0 else 0
    medium_volatility = medium_range / avg_price if avg_price != 0 else 0
    long_volatility = long_range / avg_price if avg_price != 0 else 0
    
    logger.info(f"🚛 Sideway Check: Short={short_volatility:.6f}, Medium={medium_volatility:.6f}, Long={long_volatility:.6f}")
    
    # ถ้าทุกกรอบเวลามี volatility ต่ำ = sideway
    return (short_volatility < volatility_threshold and 
            medium_volatility < volatility_threshold * 1.2 and 
            long_volatility < volatility_threshold * 1.5)

# --- Multi-timeframe Trend Analysis ---
def get_comprehensive_trend():
    if len(price_history) < 100:
        return None, None, None
    
    # Short-term trend (20 periods)
    ema_10 = ema(price_history[-50:], 10)
    ema_20 = ema(price_history[-50:], 20)
    
    # Medium-term trend (50 periods)
    ema_30 = ema(price_history[-80:], 30)
    ema_50 = ema(price_history[-100:], 50)
    
    # Long-term trend (100 periods)
    ema_100 = ema(price_history[-100:], 100)
    
    if None in (ema_10, ema_20, ema_30, ema_50, ema_100):
        return None, None, None
    
    short_trend = "UP" if ema_10 > ema_20 else "DOWN"
    medium_trend = "UP" if ema_30 > ema_50 else "DOWN"
    long_trend = "UP" if ema_50 > ema_100 else "DOWN"
    
    return short_trend, medium_trend, long_trend

# --- Advanced Signal Scoring System ---
def get_trade_signal_with_score():
    if len(price_history) < min_history_length:
        return None, 0.0, {}

    # Technical indicators
    ema_5 = ema(price_history[-30:], 5)
    ema_10 = ema(price_history[-30:], 10)
    ema_20 = ema(price_history[-50:], 20)
    
    macd_line, signal_line, histogram = calculate_macd(price_history)
    rsi_value = rsi(price_history)
    upper, sma, lower = bollinger_bands(price_history)
    
    short_trend, medium_trend, long_trend = get_comprehensive_trend()
    current_price = price_history[-1]

    details = {
        "ema_5": ema_5,
        "ema_10": ema_10,
        "ema_20": ema_20,
        "macd_line": macd_line,
        "signal_line": signal_line,
        "histogram": histogram,
        "rsi": rsi_value,
        "upper": upper,
        "sma": sma,
        "lower": lower,
        "short_trend": short_trend,
        "medium_trend": medium_trend,
        "long_trend": long_trend,
        "price": current_price
    }

    if None in (ema_5, ema_10, ema_20, macd_line, signal_line, rsi_value, upper, lower):
        return None, 0.0, details

    score = 0.0

    # 1. MACD Analysis (Weight: 2.0)
    if macd_line > signal_line:
        score += 2.0
        macd_dir = "UP"
        if histogram and histogram > 0:
            score += 0.3  # Momentum strengthening
    else:
        score -= 2.0
        macd_dir = "DOWN"
        if histogram and histogram < 0:
            score += 0.3  # Momentum strengthening

    details["macd_dir"] = macd_dir

    # 2. EMA Trend Analysis (Weight: 1.5)
    if ema_5 > ema_10 > ema_20:
        score += 1.5
        ema_trend = "STRONG_UP"
    elif ema_5 > ema_10:
        score += 0.8
        ema_trend = "UP"
    elif ema_5 < ema_10 < ema_20:
        score -= 1.5
        ema_trend = "STRONG_DOWN"
    elif ema_5 < ema_10:
        score -= 0.8
        ema_trend = "DOWN"
    else:
        ema_trend = "SIDEWAYS"

    details["ema_trend"] = ema_trend

    # 3. RSI Analysis (Weight: 0.8)
    if 40 <= rsi_value <= 60:
        score += 0.8  # Neutral zone
    elif 30 <= rsi_value < 40:
        score += 0.5  # Slightly oversold
    elif 60 < rsi_value <= 70:
        score += 0.5  # Slightly overbought
    elif rsi_value < 30:
        score += 0.2  # Very oversold
    elif rsi_value > 70:
        score += 0.2  # Very overbought

    # 4. Bollinger Bands Analysis (Weight: 0.7)
    band_width = upper - lower
    price_position = (current_price - lower) / band_width if band_width > 0 else 0.5
    
    if 0.3 <= price_position <= 0.7:
        score += 0.7  # Price in middle zone
    elif 0.2 <= price_position < 0.3 or 0.7 < price_position <= 0.8:
        score += 0.4  # Price near bands but not extreme
    else:
        score += 0.1  # Price at extreme levels

    details["price_position"] = price_position

    # 5. Multi-timeframe Trend Confirmation (Weight: 1.0)
    trend_score = 0
    if short_trend == medium_trend == long_trend == "UP":
        trend_score = 1.0
    elif short_trend == medium_trend == long_trend == "DOWN":
        trend_score = -1.0
    elif short_trend == medium_trend and short_trend == "UP":
        trend_score = 0.6
    elif short_trend == medium_trend and short_trend == "DOWN":
        trend_score = -0.6
    elif short_trend == "UP":
        trend_score = 0.3
    elif short_trend == "DOWN":
        trend_score = -0.3

    score += trend_score
    details["trend_score"] = trend_score

    # 6. Signal Consistency Check
    recent_signals = signal_history[-5:] if len(signal_history) >= 5 else signal_history
    if recent_signals:
        consistency = len([s for s in recent_signals if s == macd_dir]) / len(recent_signals)
        score += consistency * 0.5

    details["final_score"] = score

    # Determine signal
    if macd_dir == "UP" and ema_trend in ["UP", "STRONG_UP"] and score > 0:
        signal = "CALL"
    elif macd_dir == "DOWN" and ema_trend in ["DOWN", "STRONG_DOWN"] and score < 0:
        signal = "PUT"
    else:
        signal = None

    return signal, abs(score), details  # Return absolute score

# --- Enhanced Trade Execution ---
def send_trade(ws, contract_type):
    global last_trade_time
    
    # Double-check conditions before sending
    if active_contract_id is not None:
        logger.warning("🚫 Cannot send trade: Active contract exists")
        return
    
    if time.time() < pause_until:
        logger.warning("🚫 Cannot send trade: In pause period")
        return
    
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
    
    logger.info(f"🚀 Trade sent: {contract_type} at {datetime.fromtimestamp(last_trade_time).strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"💰 Amount: {amount} USD, Duration: {duration} minute(s)")

# --- Enhanced Results Tracking ---
def update_result(result, profit):
    global total_trades, wins, losses, consecutive_losses, pause_until
    
    total_trades += 1

    if result == "WIN":
        wins += 1
        consecutive_losses = 0
        logger.info("✅ Trade Result: WIN")
    else:
        losses += 1
        consecutive_losses += 1
        logger.info("❌ Trade Result: LOSS")
        
        if consecutive_losses >= max_consecutive_losses:
            pause_until = time.time() + pause_duration_sec
            logger.warning(f"🛑 Consecutive losses limit reached! Pausing for {pause_duration_sec//60} minutes.")

    win_rate = (wins / total_trades) * 100 if total_trades > 0 else 0
    net_profit = wins * amount - losses * amount  # Approximate calculation

    logger.info("\n" + "="*50)
    logger.info("📊 TRADING SUMMARY")
    logger.info("="*50)
    logger.info(f"📌 Last Result    : {result}")
    logger.info(f"💰 Profit/Loss    : {profit:.2f} USD")
    logger.info(f"🧮 Total Trades   : {total_trades}")
    logger.info(f"✅ Wins          : {wins}")
    logger.info(f"❌ Losses        : {losses}")
    logger.info(f"📈 Win Rate       : {win_rate:.2f}%")
    logger.info(f"💵 Net P/L        : {net_profit:.2f} USD (approx)")
    logger.info(f"⚠️ Consecutive L  : {consecutive_losses}/{max_consecutive_losses}")
    logger.info(f"⏰ Time          : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("="*50 + "\n")

# --- WebSocket Event Handlers ---
def on_open(ws):
    logger.info("✅ WebSocket Connected Successfully!")
    ws.send(json.dumps({"authorize": API_TOKEN}))

def on_message(ws, message):
    global last_signal, signal_confidence, active_contract_id, last_trade_time, last_signal_time
    
    try:
        data = json.loads(message)
    except json.JSONDecodeError:
        logger.error("❌ Failed to parse JSON message")
        return

    current_time = time.time()

    # Contract timeout check
    if active_contract_id is not None:
        if current_time - last_trade_time > contract_timeout:
            logger.warning(f"⚠️ Contract timeout ({contract_timeout}s) - Resetting contract state")
            active_contract_id = None
            signal_confidence = 0
            last_signal = None

    if data.get("msg_type") == "authorize":
        if data.get("authorize", {}).get("loginid"):
            logger.info("✅ Authorization Successful!")
            ws.send(json.dumps({"ticks": symbol}))
        else:
            logger.error("❌ Authorization Failed!")

    elif data.get("msg_type") == "tick":
        # Check if in pause period
        if time.time() < pause_until:
            remaining = int(pause_until - time.time())
            if remaining % 30 == 0:  # Log every 30 seconds
                logger.info(f"⏸️ Trading paused - Resume in {remaining} seconds")
            return

        price = float(data["tick"]["quote"])
        price_history.append(price)
        
        # Maintain history size
        if len(price_history) > max_price:
            price_history.pop(0)

        # Log price periodically
        if len(price_history) % 10 == 0:
            logger.info(f"📊 Price: {price:.5f} | History: {len(price_history)} ticks")

        # Check minimum time between trades
        if now - last_trade_time < min_time_between_trades:
            return

        # Skip if insufficient data
        if len(price_history) < min_history_length:
            if len(price_history) % 20 == 0:
                logger.info(f"📝 Collecting data... {len(price_history)}/{min_history_length}")
            return

        # Get trading signal
        signal, score, details = get_trade_signal_with_score()
        
        if current_time - last_signal_time > 60:  # Log detailed analysis every minute
            logger.info(f"🔍 Analysis - Signal: {signal}, Score: {score:.2f}")
            logger.info(f"📈 MACD: {details.get('macd_dir')}, EMA: {details.get('ema_trend')}, RSI: {details.get('rsi', 0):.1f}")
            last_signal_time = current_time

        if signal:
            # Check for sideway market
            if is_sideway():
                logger.info("⛔ Signal ignored: Market in sideway trend")
                return

            # Check score threshold
            if score >= score_threshold and active_contract_id is None:
                # Signal confirmation logic
                if signal == last_signal:
                    signal_confidence += 1
                    logger.info(f"🎯 Signal confirmation {signal_confidence}/{required_confidence}: {signal}")
                else:
                    signal_confidence = 1
                    last_signal = signal
                    logger.info(f"🔄 New signal detected: {signal} (Score: {score:.2f})")

                # Add to signal history
                signal_history.append(signal)
                if len(signal_history) > 20:
                    signal_history.pop(0)

                # Execute trade if confidence met
                if signal_confidence >= required_confidence:
                    logger.info(f"✅ All conditions met! Executing {signal} trade")
                    send_trade(ws, signal)
                    signal_confidence = 0
                    last_signal = None
            else:
                if score < score_threshold:
                    logger.info(f"📊 Score below threshold: {score:.2f} < {score_threshold}")
                if active_contract_id is not None:
                    logger.info("⏳ Waiting for active contract to complete")
        else:
            # Reset confidence if no clear signal
            if signal_confidence > 0:
                logger.info("🔄 Signal confidence reset - No clear direction")
            signal_confidence = 0
            last_signal = None

    elif data.get("msg_type") == "buy":
        contract_id = data["buy"]["contract_id"]
        active_contract_id = contract_id
        logger.info(f"📋 Contract opened successfully: {contract_id}")
        
        # Subscribe to contract updates
        ws.send(json.dumps({
            "proposal_open_contract": 1,
            "contract_id": contract_id
        }))

    elif data.get("msg_type") == "proposal_open_contract":
        contract = data["proposal_open_contract"]
        
        if contract.get("is_sold"):
            profit = contract.get("profit", 0)
            result = "WIN" if profit > 0 else "LOSS"
            
            logger.info(f"🏁 Contract completed: {result}")
            update_result(result, profit)
            active_contract_id = None
        else:
            # Log contract status updates
            current_spot = contract.get("current_spot")
            entry_spot = contract.get("entry_tick")
            if current_spot and entry_spot:
                logger.info(f"📊 Contract update - Entry: {entry_spot}, Current: {current_spot}")

    elif data.get("msg_type") == "error":
        error_msg = data.get("error", {}).get("message", "Unknown error")
        logger.error(f"❌ WebSocket Error: {error_msg}")
        
        # Reset state on certain errors
        if "InvalidContractType" in error_msg or "InvalidSymbol" in error_msg:
            active_contract_id = None
            signal_confidence = 0

def on_error(ws, error):
    logger.error(f"❌ WebSocket Error: {error}")

def on_close(ws, code, reason):
    logger.warning(f"🔌 WebSocket Disconnected: Code={code}, Reason={reason}")
    logger.info("🔄 Attempting to reconnect in 15 seconds...")
    time.sleep(15)
    run_bot()

# --- Bot Runner ---
def run_bot():
    logger.info("🤖 Starting Enhanced Trading Bot...")
    logger.info(f"📊 Configuration:")
    logger.info(f"   Symbol: {symbol}")
    logger.info(f"   Amount: {amount} USD")
    logger.info(f"   Duration: {duration} minute(s)")
    logger.info(f"   Score Threshold: {score_threshold}")
    logger.info(f"   Required Confidence: {required_confidence}")
    logger.info(f"   Max Consecutive Losses: {max_consecutive_losses}")
    
    ws = WebSocketApp(
        "wss://ws.derivws.com/websockets/v3?app_id=1089",
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    ws.run_forever()

# --- Main Execution ---
if __name__ == '__main__':
    # Start trading bot in separate thread
    threading.Thread(target=run_bot, daemon=True).start()
    
    # Start Flask API
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"🌐 Starting Flask API on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)