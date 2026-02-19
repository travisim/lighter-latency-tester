# Lighter Latency Tester

Tests latency to Binance perps (BTCUSDT bookTicker) and Lighter exchange (geo-blocking + taker signal-to-fill latency). Designed for fresh AWS instances.

## Mac

```bash
git clone https://github.com/travisim/lighter-latency-tester.git
cd lighter-latency-tester
pip install websockets -e .
python test_lighter_connectivity.py
```

## Ubuntu (AWS)

```bash
sudo apt update && sudo apt install -y git python3 python3-pip python3-venv
git clone https://github.com/travisim/lighter-latency-tester.git
cd lighter-latency-tester
python3 -m venv venv
source venv/bin/activate
pip install websockets -e .
python test_lighter_connectivity.py
```

## One-liner (Ubuntu)

```bash
sudo apt update && sudo apt install -y git python3 python3-pip python3-venv && git clone https://github.com/travisim/lighter-latency-tester.git && cd lighter-latency-tester && python3 -m venv venv && source venv/bin/activate && pip install websockets -e . && python test_lighter_connectivity.py
```

## What It Tests

1. **Binance BTCUSDT Perps** — WS connect time, ping RTT, one-way ticker latency (server timestamp delta over 20 samples)
2. **Lighter Geo-Block** — WS connect + orderbook subscription
3. **Lighter Signal-to-Fill** — true taker latency from order decision to matching engine fill confirmation

Uses a dual-WebSocket architecture: one connection listens for fill notifications (`account_all_trades`), another sends orders. Reports a full breakdown:

- **Signing** — local order signing time
- **Send → Ack** — network round-trip until server acknowledges the order
- **Ack → Fill** — time from server ack to matching engine fill notification
- **Total S2F** — end-to-end signal-to-fill latency

Binance runs first as a baseline. Binance failure does not block Lighter tests.

## Configuration

Edit the top of `test_lighter_connectivity.py`:

```python
ACCOUNT_INDEX = 699528
PRIVATE_KEY = "your_hex_key_here"
API_KEY_INDEX = 4
API_URL = "https://mainnet.zklighter.elliot.ai"
MARKET_INDEX = 0  # ETH/USDT.p
```

Binance test requires no configuration (public WebSocket stream).
