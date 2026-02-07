# Lighter Latency Tester

Tests geo-blocking and taker order latency against Lighter exchange. Designed for fresh AWS instances.

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

## Configuration

Edit the top of `test_lighter_connectivity.py`:

```python
ACCOUNT_INDEX = 699528
PRIVATE_KEY = "your_hex_key_here"
API_KEY_INDEX = 4
API_URL = "https://mainnet.zklighter.elliot.ai"
MARKET_INDEX = 0  # ETH/USDT.p
```
