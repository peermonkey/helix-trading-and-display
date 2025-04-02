# Helix Trading Bot

A trading bot that uses Helix indicator to trade BTC and ETH pairs on MetaTrader 5.

## Features

- Real-time Helix indicator monitoring
- Automated trading based on Helix signals
- Web interface for monitoring and control
- Real-time position and PnL tracking
- Configurable trading parameters

## Installation

1. Install Python 3.7 or higher
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Make sure MetaTrader 5 is installed and configured with your broker

## Configuration

Edit the `config.json` file to set your broker credentials and trading parameters:

```json
{
  "broker": {
    "account": "YOUR_ACCOUNT_NUMBER",
    "password": "YOUR_PASSWORD",
    "server": "YOUR_BROKER_SERVER"
  },
  "trade_parameters": {
    "min_btc_qty": 0.01,
    "min_eth_qty": 0.27,
    "scaling_factor_btc": 1.0,
    "scaling_factor_eth": 1.0,
    "min_helix_threshold": 0.03,
    "helix_step_size": 0.02,
    "helix_reset_range": 0.1,
    "pnl_target_per_event": 2.5,
    "total_pnl_target": 500,
    "max_active_events": 10,
    "helix_websocket_url": "ws://localhost:3000",
    "binance_websocket_url": "wss://stream.binance.com:9443/ws",
    "web_port": 5000
  }
}
```

## Running the Bot

1. Make sure the Helix WebSocket server is running (typically on localhost:3000)
2. Start the bot:

```bash
python bot.py
```

3. Access the web interface by opening a browser and navigating to:

```
http://localhost:5000
```

## Using the Web Interface

The web interface provides:

- Real-time price and Helix indicator data
- Position and P&L monitoring
- Trading controls (pause/resume)
- Parameter adjustment
- Bot status monitoring

## API Endpoints

- `GET /api/status` - Get bot status and parameters
- `GET /api/positions` - Get open positions
- `POST /api/command` - Send command to bot

## WebSocket Events

- `status_update` - Bot status updates
- `price_update` - Price and Helix updates
- `position_update` - Position updates
- `event_update` - Trading event updates 