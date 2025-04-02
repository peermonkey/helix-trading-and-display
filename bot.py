import json
import time
import MetaTrader5 as mt5
from datetime import datetime, timezone, timedelta
import threading
import websocket  # pip install websocket-client
try:
    import thread
except ImportError:
    import _thread as thread
import os
# Add imports for WebSocket server and API
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_socketio import SocketIO
import logging

# Load configuration from config.json
with open("config.json", "r") as config_file:
    config = json.load(config_file)

broker_config = config.get("broker", {})
trade_params = config.get("trade_parameters", {})

# -------------------------------------------------------------------------
# CONFIGURABLE PARAMETERS - Edit these values in config.json or here
# -------------------------------------------------------------------------
# Broker credentials from config
account = int(broker_config.get("account"))
password = broker_config.get("password")
server = broker_config.get("server")

# Trade parameters from config
MIN_BTC_QTY = trade_params.get("min_btc_qty", 0.01)
MIN_ETH_QTY = trade_params.get("min_eth_qty", 0.27)
SCALING_FACTOR_BTC = trade_params.get("scaling_factor_btc", 1.0)
SCALING_FACTOR_ETH = trade_params.get("scaling_factor_eth", 1.0)

# Trading thresholds and steps
MIN_HELIX_THRESHOLD = trade_params.get("min_helix_threshold", 0.06)  # Minimum Helix value to start trading
HELIX_STEP_SIZE = trade_params.get("helix_step_size", 0.02)  # Increment for additional trades
HELIX_RESET_RANGE = trade_params.get("helix_reset_range", 0.05)  # Range for Helix to reset (-X to +X)
PNL_TARGET_PER_EVENT = trade_params.get("pnl_target_per_event", 2.5)  # Profit target per trading event
TOTAL_PNL_TARGET = trade_params.get("total_pnl_target", 500)  # Total profit target to close all positions
MAX_ACTIVE_EVENTS = trade_params.get("max_active_events", 10)  # Maximum number of simultaneous events

# WebSocket URLs
HELIX_WEBSOCKET_URL = trade_params.get("helix_websocket_url", "ws://localhost:3000")
BINANCE_WEBSOCKET_URL = trade_params.get("binance_websocket_url", "wss://stream.binance.com:9443/ws")

# Web interface port
WEB_PORT = trade_params.get("web_port", 5000)
# -------------------------------------------------------------------------

# Dictionary to track active trading events
opened_positions = {}
event_id_counter = 0  # Unique event ID for each trade
waiting_for_reset = False  # Flag to ensure we wait for Helix reset before new trades

# Global variable for storing the latest BTC-ETH ratio (rounded)
latest_btc_eth_ratio = None

# Global variables for live prices from Binance
live_btc_price = 0.0
live_eth_price = 0.0

# Global flags for controlling trading
allow_new_trades = True  # Automatically managed by max events monitor
manual_trading_pause = False  # Manually controlled by user

# Global variables for storing latest Helix values
latest_helix_15m = 0.0
latest_helix_1h = 0.0
latest_btc_price = 0.0
latest_eth_price = 0.0

# Global event tracking
total_opened_events = 0
total_closed_events = 0

# Last traded levels per direction - initialized with min threshold
initial_last_trade_levels = {"bullish": -MIN_HELIX_THRESHOLD, "bearish": MIN_HELIX_THRESHOLD}
last_trade_levels = initial_last_trade_levels.copy()

# Global PnL tracking
total_pnl = 0
total_booked_pnl = 0

# Initialize Flask and SocketIO for web interface
app = Flask(__name__, static_folder='static')
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------
# API Endpoints
# ---------------------------
@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/api/status', methods=['GET'])
def get_status():
    active_events = sum(1 for event in opened_positions.values() if event.get("active", False))
    
    status = {
        "trading_status": "PAUSED" if manual_trading_pause else "ACTIVE",
        "active_events": active_events,
        "max_active_events": MAX_ACTIVE_EVENTS,
        "total_pnl": round(total_pnl, 2),
        "total_pnl_target": TOTAL_PNL_TARGET,
        "booked_pnl": round(total_booked_pnl, 2),
        "events_opened": total_opened_events,
        "events_closed": total_closed_events,
        "trading_params": {
            "btc_qty": MIN_BTC_QTY,
            "eth_qty": MIN_ETH_QTY,
            "btc_scale": SCALING_FACTOR_BTC,
            "eth_scale": SCALING_FACTOR_ETH,
            "helix_threshold": MIN_HELIX_THRESHOLD,
            "helix_step_size": HELIX_STEP_SIZE,
            "reset_range": HELIX_RESET_RANGE,
            "pnl_target_per_event": PNL_TARGET_PER_EVENT
        },
        "latest_values": {
            "helix_15m": round(latest_helix_15m, 4),
            "helix_1h": round(latest_helix_1h, 4),
            "btc_price": round(live_btc_price, 2),
            "eth_price": round(live_eth_price, 2),
            "btc_eth_ratio": latest_btc_eth_ratio
        }
    }
    
    return jsonify(status)

@app.route('/api/positions', methods=['GET'])
def get_positions():
    position_data = []
    positions_from_mt5 = mt5.positions_get()
    
    if positions_from_mt5:
        for position in positions_from_mt5:
            position_data.append({
                "ticket": position.ticket,
                "symbol": position.symbol,
                "type": "BUY" if position.type == mt5.ORDER_TYPE_BUY else "SELL",
                "volume": position.volume,
                "open_price": position.price_open,
                "current_price": position.price_current,
                "profit": position.profit,
                "swap": position.swap,
                "time_open": position.time
            })
    
    return jsonify(position_data)

@app.route('/api/command', methods=['POST'])
def execute_command():
    if not request.json or 'command' not in request.json:
        return jsonify({"status": "error", "message": "No command provided"}), 400
    
    command = request.json['command']
    result = process_user_command(command)
    
    # Emit updated status after command
    emit_status_update()
    
    return jsonify({"status": "success", "command": command, "continue": result})

# ---------------------------
# WebSocket Emit Functions
# ---------------------------
def emit_status_update():
    """Emit current status to all connected clients"""
    active_events = sum(1 for event in opened_positions.values() if event.get("active", False))
    
    status = {
        "trading_status": "PAUSED" if manual_trading_pause else "ACTIVE",
        "active_events": active_events,
        "max_active_events": MAX_ACTIVE_EVENTS,
        "total_pnl": round(total_pnl, 2),
        "booked_pnl": round(total_booked_pnl, 2),
        "events_opened": total_opened_events,
        "events_closed": total_closed_events
    }
    
    socketio.emit('status_update', status)

def emit_price_update():
    """Emit current prices to all connected clients"""
    price_data = {
        "btc_price": round(live_btc_price, 2),
        "eth_price": round(live_eth_price, 2),
        "btc_eth_ratio": latest_btc_eth_ratio,
        "helix_15m": round(latest_helix_15m, 4),
        "helix_1h": round(latest_helix_1h, 4)
    }
    
    socketio.emit('price_update', price_data)

def emit_position_update():
    """Emit current positions to all connected clients"""
    position_data = []
    positions_from_mt5 = mt5.positions_get()
    
    if positions_from_mt5:
        for position in positions_from_mt5:
            position_data.append({
                "ticket": position.ticket,
                "symbol": position.symbol,
                "type": "BUY" if position.type == mt5.ORDER_TYPE_BUY else "SELL",
                "volume": position.volume,
                "open_price": position.price_open,
                "current_price": position.price_current,
                "profit": position.profit,
                "swap": position.swap,
                "time_open": position.time
            })
    
    socketio.emit('position_update', position_data)

def emit_event_update():
    """Emit trading events data to all connected clients"""
    events_data = []
    
    for event_id, event in opened_positions.items():
        if event.get("active", False):
            events_data.append({
                "event_id": event_id,
                "helix_entry": round(event.get("helix", 0), 4),
                "pnl": round(event.get("pnl", 0), 2),
                "positions": [pos.get("ticket") for pos in event.get("positions", [])]
            })
    
    socketio.emit('event_update', events_data)

# Add SocketIO connection handlers
@socketio.on('connect')
def handle_connect():
    logger.info('Client connected')
    # Send initial data
    emit_status_update()
    emit_price_update()
    emit_position_update()
    emit_event_update()

@socketio.on('disconnect')
def handle_disconnect():
    logger.info('Client disconnected')

# ---------------------------
# Update the price data thread to emit WebSocket updates
# ---------------------------
def periodic_data_emitter():
    """Periodically emit data updates to connected clients"""
    while True:
        emit_price_update()
        emit_position_update()
        emit_status_update()
        emit_event_update()
        time.sleep(1)  # Update every second

# ---------------------------
# Decide Trade Quantity Function (with rounding)
# ---------------------------
def decide_trade_qty(min_btc_qty, min_eth_qty, scaling_factor_btc, scaling_factor_eth, btc_eth_ratio):
    """
    Determine the trade quantity for BTC and ETH.
    For ETH, the quantity is computed as btc_qty * btc_eth_ratio (rounded) and then scaled.
    
    Parameters:
        min_btc_qty (float): Minimum BTC quantity.
        min_eth_qty (float): Minimum ETH quantity.
        scaling_factor_btc (float): Scaling factor for BTC.
        scaling_factor_eth (float): Scaling factor for ETH.
        btc_eth_ratio (float): Rounded BTC/ETH ratio.
        
    Returns:
        btc_qty (float): Calculated BTC quantity (rounded to 2 decimals).
        eth_qty (float): Calculated ETH quantity (rounded to 2 decimals).
    """
    btc_qty = min_btc_qty * scaling_factor_btc
    eth_qty = btc_qty * btc_eth_ratio * scaling_factor_eth

    # Ensure minimum quantities are met.
    if btc_qty < min_btc_qty:
        btc_qty = min_btc_qty
    if eth_qty < min_eth_qty:
        eth_qty = min_eth_qty

    # Round to two decimals to match broker precision requirements
    btc_qty = round(btc_qty, 2)
    eth_qty = round(eth_qty, 2)
    
    return btc_qty, eth_qty

# Initialize MT5
if not mt5.initialize():
    print("MT5 initialization failed, error:", mt5.last_error())
    exit()

if not mt5.login(account, password, server):
    print("Login failed, error:", mt5.last_error())
    mt5.shutdown()
    exit()

# Function to convert UTC timestamp to IST
def convert_to_ist(timestamp):
    utc_time = datetime.fromtimestamp(timestamp / 1000.0, tz=timezone.utc)
    ist_time = utc_time.astimezone(timezone(timedelta(hours=5, minutes=30)))
    return ist_time.strftime("%Y-%m-%d %H:%M:%S")

# Function to place an order in MT5
def place_order(symbol, volume, signal_type, event_id):
    order_type = mt5.ORDER_TYPE_BUY if signal_type == "long" else mt5.ORDER_TYPE_SELL
    
    # Check if symbol exists in MT5
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        print(f"❌ Error: Symbol {symbol} not found in MT5")
        # List available symbols that contain either BTC or ETH
        symbols = mt5.symbols_get()
        print("Available symbols:")
        for s in symbols:
            if "BTC" in s.name or "ETH" in s.name:
                print(f"  - {s.name}")
        return None
    
    if not symbol_info.visible:
        print(f"⚠️ Symbol {symbol} is not visible, trying to make it visible")
        if not mt5.symbol_select(symbol, True):
            print(f"❌ Failed to select symbol {symbol}")
            return None
    
    price = mt5.symbol_info_tick(symbol).ask if order_type == mt5.ORDER_TYPE_BUY else mt5.symbol_info_tick(symbol).bid

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": order_type,
        "price": price,
        "deviation": 10,
        "magic": 234000,
        "comment": f"Helix Signal {signal_type}",
        "type_filling": mt5.ORDER_FILLING_IOC,
        "type_time": mt5.ORDER_TIME_GTC,
    }

    print(f"📝 Sending order request for {symbol}: {volume} lots, {signal_type}")
    order_result = mt5.order_send(request)

    # Accept either the original success code or a retcode of 1
    if order_result is None:
        print(f"❌ Order failed for {symbol}. Error: {mt5.last_error()}")
        return None
    elif order_result.retcode != mt5.TRADE_RETCODE_DONE and order_result.retcode != 1:
        print(f"❌ Order failed for {symbol}. Error code: {order_result.retcode}, Error: {order_result.comment}")
        return None

    print(f"✅ Order executed: {symbol}, {signal_type}, Ticket: {order_result.order}")

    return order_result.order

# Function to close a position using ticket ID
def close_position(ticket):
    """Closes an open trade in MT5 using the correct ticket ID."""
    position = mt5.positions_get(ticket=ticket)
    if not position:
        print(f"🔍 Trade {ticket} not found or already closed.")
        return False

    position = position[0]
    order_type = mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    price = mt5.symbol_info_tick(position.symbol).bid if order_type == mt5.ORDER_TYPE_SELL else mt5.symbol_info_tick(position.symbol).ask

    close_request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": position.symbol,
        "volume": position.volume,
        "type": order_type,
        "position": position.ticket,
        "price": price,
        "deviation": 10,
        "magic": 234000,
        "comment": "Auto-close: PnL Target Hit",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(close_request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        print(f"✅ Trade {ticket} closed successfully.")
        return True
    else:
        print(f"❌ Failed to close trade {ticket}. MT5 Error: {mt5.last_error()}")
        return False

# ---------------------------
# Binance WebSocket Client for Live Prices
# ---------------------------
def on_binance_message(ws, message):
    global live_btc_price, live_eth_price, latest_btc_eth_ratio
    
    try:
        data = json.loads(message)
        
        # Extract price data from the stream
        if "s" in data and "c" in data:  # Check for symbol and current price
            symbol = data["s"]
            price = float(data["c"])  # Current close price
            
            if symbol == "BTCUSDT":
                live_btc_price = price
                print(f"📊 Latest BTC Price: ${live_btc_price:.2f}")
            elif symbol == "ETHUSDT":
                live_eth_price = price
                print(f"📊 Latest ETH Price: ${live_eth_price:.2f}")
            
            # Calculate BTC-ETH ratio if we have both prices
            if live_btc_price > 0 and live_eth_price > 0:
                btc_eth_ratio_og = live_btc_price / live_eth_price
                latest_btc_eth_ratio = round(btc_eth_ratio_og)
                print(f"📊 Updated BTC-ETH Ratio: {latest_btc_eth_ratio} (from live prices)")
            
            # Emit price update via WebSocket
            emit_price_update()
    
    except Exception as e:
        print(f"Error processing Binance message: {e}")
        print(f"Raw message: {message}")

def on_binance_error(ws, error):
    print(f"Binance WebSocket Error: {error}")

def on_binance_close(ws, close_status_code, close_msg):
    print(f"Binance WebSocket connection closed: {close_status_code} - {close_msg}")
    # Try to reconnect after a delay
    print("Attempting to reconnect to Binance in 5 seconds...")
    time.sleep(5)
    start_binance_websocket()

def on_binance_open(ws):
    print("Binance WebSocket connection established")
    # Subscribe to BTC and ETH ticker streams
    subscribe_msg = json.dumps({
        "method": "SUBSCRIBE",
        "params": [
            "btcusdt@ticker",
            "ethusdt@ticker"
        ],
        "id": 1
    })
    ws.send(subscribe_msg)

def start_binance_websocket():
    # Create a WebSocket connection to Binance
    ws = websocket.WebSocketApp(BINANCE_WEBSOCKET_URL,
                                on_open=on_binance_open,
                                on_message=on_binance_message,
                                on_error=on_binance_error,
                                on_close=on_binance_close)
    
    # Start the WebSocket connection in a separate thread
    wst = threading.Thread(target=ws.run_forever)
    wst.daemon = True
    wst.start()
    return ws

# ---------------------------
# Helix WebSocket Client
# ---------------------------
def on_message(ws, message):
    global latest_helix_15m, latest_helix_1h, latest_btc_price, latest_eth_price
    
    try:
        data = json.loads(message)
        if data["type"] == "welcome":
            print(f"Connected to Helix WebSocket server: {data['message']}")
        
        elif data["type"] == "helix_update":
            helix_data = data["data"]
            
            # Extract the 15m and 1h Helix values
            if "15m" in helix_data:
                helix_15m_data = helix_data["15m"]
                latest_helix_15m = float(helix_15m_data["helixValue"])
                latest_btc_price = float(helix_15m_data["btcDelta"])
                latest_eth_price = float(helix_15m_data["ethDelta"])
            
            if "1h" in helix_data:
                helix_1h_data = helix_data["1h"]
                latest_helix_1h = float(helix_1h_data["helixValue"])
            
            # Process the trading logic with the latest Helix values
            process_trading_logic(latest_helix_15m, latest_helix_1h)
            
            # Log the latest values
            print(f"📊 Latest Helix Values - 15m: {latest_helix_15m:.4f}, 1h: {latest_helix_1h:.4f}, BTC-ETH Ratio: {latest_btc_eth_ratio}")
            
            # Emit update via WebSocket
            emit_price_update()
    
    except Exception as e:
        print(f"Error processing Helix WebSocket message: {e}")
        print(f"Raw message: {message}")

def on_error(ws, error):
    print(f"Helix WebSocket Error: {error}")

def on_close(ws, close_status_code, close_msg):
    print(f"Helix WebSocket connection closed: {close_status_code} - {close_msg}")
    # Try to reconnect after a delay
    print("Attempting to reconnect to Helix in 5 seconds...")
    time.sleep(5)
    start_helix_websocket()

def on_open(ws):
    print("Helix WebSocket connection established")

def start_helix_websocket():
    # Create a WebSocket connection with all callback functions
    ws = websocket.WebSocketApp(HELIX_WEBSOCKET_URL,
                                on_open=on_open,
                                on_message=on_message,
                                on_error=on_error,
                                on_close=on_close)
    
    # Start the WebSocket connection in a separate thread
    wst = threading.Thread(target=ws.run_forever)
    wst.daemon = True
    wst.start()
    return ws

def process_trading_logic(helix_15m, helix_1h):
    global event_id_counter, total_opened_events, total_closed_events, waiting_for_reset, last_trade_levels, latest_btc_eth_ratio, allow_new_trades, manual_trading_pause
    
    # Check for manual trading pause
    if manual_trading_pause:
        # Only monitor existing positions, don't open new ones
        return
        
    # Check for Helix Reset
    if -HELIX_RESET_RANGE <= helix_15m <= HELIX_RESET_RANGE:
        # If we weren't previously in the reset range, this is a new reset
        if not waiting_for_reset:
            print("✅ Helix15m reset detected. Ready for new trades after exit from reset range.")
            # Reset trading levels to initial thresholds for pyramiding
            last_trade_levels = initial_last_trade_levels.copy()
            waiting_for_reset = True
        # While in reset range, don't process trading logic
        return
    else:
        # If we were waiting for reset and now we're outside the range, we can resume trading
        if waiting_for_reset:
            print(f"🔄 Helix15m exited reset range at {helix_15m:.4f}. Trading resumed with fresh levels.")
            waiting_for_reset = False

    # Check if new trades are allowed based on active events count
    if not allow_new_trades:
        print("🚨 New trades are paused due to too many active events.")
        return

    # Ensure we have a valid ratio; default to 16.0 if not set.
    trade_ratio = latest_btc_eth_ratio if latest_btc_eth_ratio is not None else 16.0
    
    # Debug information
    print(f"🧮 Current trading levels - Bearish: {last_trade_levels['bearish']}, Bullish: {last_trade_levels['bullish']}")
    print(f"🧮 Current Helix15m: {helix_15m}, Step size: {HELIX_STEP_SIZE}")
    print(f"🧮 BTC/ETH ratio: {trade_ratio}")

    ### 🚀 Bearish Trend (SHORT BTC, LONG ETH)
    if helix_15m > last_trade_levels["bearish"] + HELIX_STEP_SIZE:
        last_trade_levels["bearish"] = round(helix_15m, 2)
        event_id_counter += 1
        # Calculate trade quantities based on the BTC-ETH ratio.
        btc_qty, eth_qty = decide_trade_qty(MIN_BTC_QTY, MIN_ETH_QTY, SCALING_FACTOR_BTC, SCALING_FACTOR_ETH, trade_ratio)
        print(f"🔄 Placing Bearish trades - BTC Qty: {btc_qty}, ETH Qty: {eth_qty}")
        
        btc_ticket = place_order("BTCUSD", btc_qty, "short", event_id_counter)
        if btc_ticket:
            print(f"⏳ BTC order placed, now placing ETH order...")
            eth_ticket = place_order("ETHUSD", eth_qty, "long", event_id_counter)
            
            if eth_ticket:
                total_opened_events += 1
                opened_positions[event_id_counter] = {
                    "positions": [{"ticket": btc_ticket}, {"ticket": eth_ticket}],
                    "pnl": 0,
                    "helix": helix_15m,
                    "active": True
                }
                threading.Thread(target=monitor_pnl, args=(event_id_counter,), daemon=True).start()
            else:
                print("❌ ETH order failed. Cancelling BTC order.")
                close_position(btc_ticket)
                total_closed_events += 1
        else:
            print("❌ BTC order failed. Not placing ETH order.")
            total_closed_events += 1

    ### 🚀 Bullish Trend (LONG BTC, SHORT ETH)
    if helix_15m < last_trade_levels["bullish"] - HELIX_STEP_SIZE:
        last_trade_levels["bullish"] = round(helix_15m, 2)
        event_id_counter += 1
        # Calculate trade quantities based on the BTC-ETH ratio.
        btc_qty, eth_qty = decide_trade_qty(MIN_BTC_QTY, MIN_ETH_QTY, SCALING_FACTOR_BTC, SCALING_FACTOR_ETH, trade_ratio)
        print(f"🔄 Placing Bullish trades - BTC Qty: {btc_qty}, ETH Qty: {eth_qty}")
        
        btc_ticket = place_order("BTCUSD", btc_qty, "long", event_id_counter)
        if btc_ticket:
            print(f"⏳ BTC order placed, now placing ETH order...")
            eth_ticket = place_order("ETHUSD", eth_qty, "short", event_id_counter)
            
            if eth_ticket:
                total_opened_events += 1
                opened_positions[event_id_counter] = {
                    "positions": [{"ticket": btc_ticket}, {"ticket": eth_ticket}],
                    "pnl": 0,
                    "helix": helix_15m,
                    "active": True
                }
                threading.Thread(target=monitor_pnl, args=(event_id_counter,), daemon=True).start()
            else:
                print("❌ ETH order failed. Cancelling BTC order.")
                close_position(btc_ticket)
                total_closed_events += 1
        else:
            print("❌ BTC order failed. Not placing ETH order.")
            total_closed_events += 1

def monitor_pnl(event_id):
    """Monitors PnL for a given event and closes trades if targets are hit."""
    global total_pnl, total_booked_pnl, total_closed_events
    print(f"📊 Monitoring PnL for Event {event_id}...")
    while opened_positions.get(event_id, {}).get("active", False):
        event_pnl = 0
        event_positions = opened_positions[event_id]["positions"]
        event_helix = opened_positions[event_id]["helix"]
        positions_data = mt5.positions_get()
        if not positions_data:
            print(f"🛑 No open positions for Event {event_id}. Marking as closed.")
            opened_positions[event_id]["active"] = False
            emit_event_update()  # Emit update
            return
        open_tickets = set(position.ticket for position in positions_data)
        for trade in event_positions:
            if trade["ticket"] in open_tickets:
                for position in positions_data:
                    if position.ticket == trade["ticket"]:
                        event_pnl += position.profit
        opened_positions[event_id]["pnl"] = event_pnl
        total_pnl = sum(event["pnl"] for event in opened_positions.values() if event["active"])
        print(f"📈 Event {event_id} - Helix Entry: {event_helix:.4f} | Live PnL: {event_pnl:.2f}, "
              f"Total Open PnL: {total_pnl:.2f}, Booked PnL: {total_booked_pnl:.2f}, "
              f"Total Events: {total_opened_events} Opened / {total_closed_events} Closed")
        
        # Emit updates via WebSocket
        emit_status_update()
        emit_position_update()
        emit_event_update()
        
        if event_pnl >= PNL_TARGET_PER_EVENT:
            print(f"🚀 Event {event_id} (Helix: {event_helix:.4f}) reached PnL target! Closing trades...")
            for trade in event_positions:
                close_position(trade["ticket"])
            opened_positions[event_id]["active"] = False
            total_booked_pnl += event_pnl
            total_closed_events += 1
            emit_status_update()  # Emit update
            emit_event_update()  # Emit update
            return
        if total_pnl >= TOTAL_PNL_TARGET:
            print(f"🔥 Total PnL target (${TOTAL_PNL_TARGET}) reached! Closing all trades...")
            for active_event in list(opened_positions.keys()):
                for trade in opened_positions[active_event]["positions"]:
                    close_position(trade["ticket"])
                opened_positions[active_event]["active"] = False
                total_booked_pnl += opened_positions[active_event]["pnl"]
                total_closed_events += 1
            emit_status_update()  # Emit update
            emit_event_update()  # Emit update
            return
        time.sleep(0.2)

# ---------------------------
# Monitor Simultaneous Positions Function (based on active events)
# ---------------------------
def monitor_simultaneous_positions():
    """
    Monitors the total number of active events.
    If more than MAX_ACTIVE_EVENTS are active,
    it sets allow_new_trades to False to pause new trades.
    Once the count falls back to MAX_ACTIVE_EVENTS or below, it resumes trading.
    """
    global allow_new_trades
    while True:
        active_events = sum(1 for event in opened_positions.values() if event.get("active", False))
        if active_events > MAX_ACTIVE_EVENTS:
            if allow_new_trades:
                allow_new_trades = False
                print(f"🚨 Maximum simultaneous events reached ({active_events}). Pausing new trades.")
        else:
            if not allow_new_trades:
                allow_new_trades = True
                print(f"✅ Active events count back to safe zone ({active_events}). Resuming new trades.")
        time.sleep(0.2)

# ---------------------------
# Command Line Interface for Trade Control
# ---------------------------
def process_user_command(command):
    """Process user commands for controlling the trading bot."""
    global manual_trading_pause, MIN_BTC_QTY, MIN_ETH_QTY, SCALING_FACTOR_BTC, SCALING_FACTOR_ETH
    global MIN_HELIX_THRESHOLD, HELIX_STEP_SIZE, HELIX_RESET_RANGE, PNL_TARGET_PER_EVENT, TOTAL_PNL_TARGET
    
    command = command.strip().lower()
    
    if command == "status":
        # Show current trading status and parameters
        active_events = sum(1 for event in opened_positions.values() if event.get("active", False))
        
        print("\n===== BOT STATUS =====")
        print(f"Trading Status: {'PAUSED' if manual_trading_pause else 'ACTIVE'}")
        print(f"Active Events: {active_events}/{MAX_ACTIVE_EVENTS}")
        print(f"Total PnL: ${total_pnl:.2f} (Target: ${TOTAL_PNL_TARGET})")
        print(f"Booked PnL: ${total_booked_pnl:.2f}")
        print(f"Events: {total_opened_events} Opened / {total_closed_events} Closed")
        
        print("\n===== TRADING PARAMETERS =====")
        print(f"BTC Qty: {MIN_BTC_QTY} (Scaling: {SCALING_FACTOR_BTC})")
        print(f"ETH Qty: {MIN_ETH_QTY} (Scaling: {SCALING_FACTOR_ETH})")
        print(f"Helix Threshold: {MIN_HELIX_THRESHOLD}")
        print(f"Helix Step Size: {HELIX_STEP_SIZE}")
        print(f"Reset Range: ±{HELIX_RESET_RANGE}")
        print(f"PnL Target Per Event: ${PNL_TARGET_PER_EVENT}")
        
        print("\n===== LATEST VALUES =====")
        print(f"Helix 15m: {latest_helix_15m:.4f}")
        print(f"Helix 1h: {latest_helix_1h:.4f}")
        print(f"BTC Price: ${live_btc_price:.2f}")
        print(f"ETH Price: ${live_eth_price:.2f}")
        print(f"BTC/ETH Ratio: {latest_btc_eth_ratio}")
        
    elif command == "pause":
        manual_trading_pause = True
        print("⏸️ Trading PAUSED. Monitoring existing positions only.")
        
    elif command == "resume":
        manual_trading_pause = False
        print("▶️ Trading RESUMED. New positions will be opened based on Helix signals.")
        
    elif command.startswith("set btc_qty "):
        try:
            value = float(command.split()[2])
            if value > 0:
                MIN_BTC_QTY = value
                print(f"✅ BTC quantity set to {MIN_BTC_QTY}")
            else:
                print("❌ Quantity must be greater than 0")
        except (ValueError, IndexError):
            print("❌ Invalid command format. Use: set btc_qty 0.01")
            
    elif command.startswith("set eth_qty "):
        try:
            value = float(command.split()[2])
            if value > 0:
                MIN_ETH_QTY = value
                print(f"✅ ETH quantity set to {MIN_ETH_QTY}")
            else:
                print("❌ Quantity must be greater than 0")
        except (ValueError, IndexError):
            print("❌ Invalid command format. Use: set eth_qty 0.27")
            
    elif command.startswith("set btc_scale "):
        try:
            value = float(command.split()[2])
            if value > 0:
                SCALING_FACTOR_BTC = value
                print(f"✅ BTC scaling factor set to {SCALING_FACTOR_BTC}")
            else:
                print("❌ Scaling factor must be greater than 0")
        except (ValueError, IndexError):
            print("❌ Invalid command format. Use: set btc_scale 1.0")
            
    elif command.startswith("set eth_scale "):
        try:
            value = float(command.split()[2])
            if value > 0:
                SCALING_FACTOR_ETH = value
                print(f"✅ ETH scaling factor set to {SCALING_FACTOR_ETH}")
            else:
                print("❌ Scaling factor must be greater than 0")
        except (ValueError, IndexError):
            print("❌ Invalid command format. Use: set eth_scale 1.0")
            
    elif command.startswith("set threshold "):
        try:
            value = float(command.split()[2])
            if value > 0:
                MIN_HELIX_THRESHOLD = value
                print(f"✅ Helix threshold set to {MIN_HELIX_THRESHOLD}")
            else:
                print("❌ Threshold must be greater than 0")
        except (ValueError, IndexError):
            print("❌ Invalid command format. Use: set threshold 0.3")
            
    elif command.startswith("set step "):
        try:
            value = float(command.split()[2])
            if value > 0:
                HELIX_STEP_SIZE = value
                print(f"✅ Helix step size set to {HELIX_STEP_SIZE}")
            else:
                print("❌ Step size must be greater than 0")
        except (ValueError, IndexError):
            print("❌ Invalid command format. Use: set step 0.3")
            
    elif command.startswith("set pnl_target "):
        try:
            value = float(command.split()[2])
            if value > 0:
                PNL_TARGET_PER_EVENT = value
                print(f"✅ PnL target per event set to ${PNL_TARGET_PER_EVENT}")
            else:
                print("❌ PnL target must be greater than 0")
        except (ValueError, IndexError):
            print("❌ Invalid command format. Use: set pnl_target 2.5")
            
    elif command == "help":
        print("\n===== COMMAND HELP =====")
        print("status - Show bot status and settings")
        print("pause - Pause opening new positions")
        print("resume - Resume opening new positions")
        print("set btc_qty X - Set BTC quantity to X")
        print("set eth_qty X - Set ETH quantity to X")
        print("set btc_scale X - Set BTC scaling factor to X")
        print("set eth_scale X - Set ETH scaling factor to X")
        print("set threshold X - Set Helix threshold to X")
        print("set step X - Set Helix step size to X")
        print("set pnl_target X - Set PnL target per event to X")
        print("help - Show this help message")
        print("exit - Exit the bot")
    
    elif command == "exit":
        print("Exiting bot...")
        return False
        
    else:
        print("❓ Unknown command. Type 'help' for available commands.")
    
    return True

# ---------------------------
# Command input thread
# ---------------------------
def command_input_thread():
    """Thread for handling user commands while the bot runs."""
    running = True
    print("\n📣 Command interface ready. Type 'help' for available commands.")
    
    while running:
        try:
            command = input("Command > ")
            running = process_user_command(command)
            if not running:
                # Signal main thread to exit
                print("Shutting down bot, please wait...")
                break
        except Exception as e:
            print(f"Error processing command: {e}")
    
    # Force exit
    os._exit(0)

if __name__ == "__main__":
    print("Starting Helix Trading Bot with WebSocket connections...")
    print(f"Configuration: Min Threshold: {MIN_HELIX_THRESHOLD}, Step Size: {HELIX_STEP_SIZE}, PnL Target: {PNL_TARGET_PER_EVENT}")
    
    # Start the Binance WebSocket connection
    print(f"Connecting to Binance WebSocket for live prices...")
    binance_ws = start_binance_websocket()
    
    # Give Binance connection time to establish and get initial prices
    time.sleep(2)
    
    # Start the Helix WebSocket connection
    print(f"Connecting to Helix WebSocket at {HELIX_WEBSOCKET_URL}")
    helix_ws = start_helix_websocket()
    
    # Start the thread to monitor simultaneous events
    monitor_thread = threading.Thread(target=monitor_simultaneous_positions, daemon=True)
    monitor_thread.start()
    
    # Start the command input thread
    cmd_thread = threading.Thread(target=command_input_thread, daemon=True)
    cmd_thread.start()
    
    # Start the periodic data emitter thread
    emitter_thread = threading.Thread(target=periodic_data_emitter, daemon=True)
    emitter_thread.start()
    
    # Start the web server
    print(f"Starting web interface on port {WEB_PORT}")
    socketio.run(app, host='0.0.0.0', port=WEB_PORT, debug=False)