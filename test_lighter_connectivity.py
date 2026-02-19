#!/usr/bin/env python3
"""
Lighter Connectivity & Latency Tester

Tests geo-blocking and taker order latency against Lighter exchange.
Designed to be scp'd to fresh AWS instances for region-comparison testing.

Usage:
    pip install -e .
    python test_lighter_connectivity.py
"""

import asyncio
import json
import signal
import sys
import time
from datetime import datetime, timezone

import websockets

import lighter
from lighter import AccountApi

# ============================================================
# === EDIT THESE ===
# ============================================================
ACCOUNT_INDEX = 699528
PRIVATE_KEY = "85d8e89c9dd2b4418eb88921c97d0ab855b8c071209b9df337d9694db103620980b15c96c56d200d"
API_KEY_INDEX = 4
API_URL = "https://mainnet.zklighter.elliot.ai"
MARKET_INDEX = 0  # ETH/USDT.p
# ============================================================

SLIPPAGE = 0.005  # 0.5%
TEST_SIZE_WEI = 10  # 0.001 ETH (scaled by 1e4)
FALLBACK_SIZE_WEI = 100  # 0.01 ETH if 0.001 rejected
GEO_BLOCK_TIMEOUT = 10  # seconds
ORDER_TIMEOUT = 10  # seconds
LIMIT_PRICE_DISCOUNT = 0.95  # 5% below best bid
FILL_TIMEOUT = 5  # seconds to wait for fill notification after ack

# WS URL derived from API URL
WS_URL = API_URL.replace("https://", "wss://") + "/stream"

# Binance Futures
BINANCE_WS_URL = "wss://fstream.binance.com/ws/btcusdt@bookTicker"
BINANCE_SAMPLE_COUNT = 20
BINANCE_TIMEOUT = 10  # seconds


class Results:
    """Collects test results for summary output."""

    def __init__(self):
        self.geo_blocked = None  # True/False/None
        self.ws_connect_ms = None
        self.orderbook_sub_ms = None
        self.best_bid = None
        self.best_ask = None
        self.preflight_ok = False
        self.balance_usdc = None
        self.position_str = "UNKNOWN"
        self.taker_error = None
        self.cleanup_position = "UNKNOWN"
        self.cleanup_balance = None
        # Signal-to-fill breakdown
        self.fill_listener_setup_ms = None
        self.taker_buy_signing_ms = None
        self.taker_buy_send_to_ack_ms = None
        self.taker_buy_ack_to_fill_ms = None  # None = no fill / timeout
        self.taker_buy_s2f_ms = None
        self.taker_sell_signing_ms = None
        self.taker_sell_send_to_ack_ms = None
        self.taker_sell_ack_to_fill_ms = None
        self.taker_sell_s2f_ms = None
        # Binance
        self.binance_ws_connect_ms = None
        self.binance_ping_rtt_ms = None
        self.binance_latency_min_ms = None
        self.binance_latency_median_ms = None
        self.binance_latency_max_ms = None
        self.binance_best_bid = None
        self.binance_best_ask = None
        self.binance_error = None


results = Results()

# Global state for cleanup on Ctrl+C
_signer_client = None
_cleanup_done = False


def _print_header():
    print("=" * 60)
    print("CONNECTIVITY & LATENCY TEST")
    print("=" * 60)
    host = API_URL.replace("https://", "").replace("http://", "")
    print(f"Endpoint: {host}")
    print(f"Account:  {ACCOUNT_INDEX}")
    print(f"Time:     {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print()


def _print_summary():
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)

    # Binance section
    if results.binance_ws_connect_ms is not None or results.binance_error:
        print("  --- Binance BTCUSDT Perps ---")
        if results.binance_ws_connect_ms is not None:
            print(f"  WS Connect:         {results.binance_ws_connect_ms:.0f}ms")
        if results.binance_ping_rtt_ms is not None:
            print(f"  WS Ping RTT:        {results.binance_ping_rtt_ms:.0f}ms (one-way est: {results.binance_ping_rtt_ms/2:.0f}ms)")
        if results.binance_latency_median_ms is not None:
            print(f"  Ticker Latency:     {results.binance_latency_median_ms:.0f}ms (median, N={BINANCE_SAMPLE_COUNT})")
            print(f"    Min: {results.binance_latency_min_ms:.0f}ms  Max: {results.binance_latency_max_ms:.0f}ms")
        if results.binance_best_bid is not None:
            print(f"  Best Bid: ${results.binance_best_bid:.2f}  Best Ask: ${results.binance_best_ask:.2f}")
        if results.binance_error:
            print(f"  Error:              {results.binance_error}")
        print()

    # Lighter section
    print("  --- Lighter ETH/USDT.p ---")

    if results.geo_blocked:
        print("  Geo-Blocked:        YES")
        print("  (Test skipped)")
    else:
        print("  Geo-Blocked:        NO")

    if results.ws_connect_ms is not None:
        print(f"  WS Connect:         {results.ws_connect_ms:.0f}ms")
    if results.orderbook_sub_ms is not None:
        print(f"  Orderbook Sub:      {results.orderbook_sub_ms:.0f}ms")
    if results.fill_listener_setup_ms is not None:
        print(f"  Fill Listener:      {results.fill_listener_setup_ms:.0f}ms (setup)")

    if results.taker_error:
        print(f"  Taker Test:         FAILED ({results.taker_error})")

    if results.taker_buy_s2f_ms is not None or results.taker_buy_send_to_ack_ms is not None:
        print(f"  Taker BUY:")
        if results.taker_buy_signing_ms is not None:
            print(f"    Signing:          {results.taker_buy_signing_ms:.0f}ms")
        if results.taker_buy_send_to_ack_ms is not None:
            print(f"    Send -> Ack:      {results.taker_buy_send_to_ack_ms:.0f}ms")
        if results.taker_buy_ack_to_fill_ms is not None:
            print(f"    Ack -> Fill:      {results.taker_buy_ack_to_fill_ms:.0f}ms")
        else:
            print(f"    Ack -> Fill:      TIMEOUT")
        if results.taker_buy_s2f_ms is not None:
            print(f"    Total S2F:        {results.taker_buy_s2f_ms:.0f}ms")

    if results.taker_sell_s2f_ms is not None or results.taker_sell_send_to_ack_ms is not None:
        print(f"  Taker SELL:")
        if results.taker_sell_signing_ms is not None:
            print(f"    Signing:          {results.taker_sell_signing_ms:.0f}ms")
        if results.taker_sell_send_to_ack_ms is not None:
            print(f"    Send -> Ack:      {results.taker_sell_send_to_ack_ms:.0f}ms")
        if results.taker_sell_ack_to_fill_ms is not None:
            print(f"    Ack -> Fill:      {results.taker_sell_ack_to_fill_ms:.0f}ms")
        else:
            print(f"    Ack -> Fill:      TIMEOUT")
        if results.taker_sell_s2f_ms is not None:
            print(f"    Total S2F:        {results.taker_sell_s2f_ms:.0f}ms")

    if results.taker_buy_s2f_ms is not None and results.taker_sell_s2f_ms is not None:
        avg = (results.taker_buy_s2f_ms + results.taker_sell_s2f_ms) / 2
        print(f"  Average S2F:        {avg:.0f}ms")

    print("=" * 60)


# ------------------------------------------------------------------
# Test 0: Binance Perps Ticker Latency
# ------------------------------------------------------------------
async def test_binance_latency():
    """Measure latency to Binance USDM futures bookTicker stream."""
    print("[Binance] BTCUSDT Perps Ticker Latency")

    # --- WS Connect ---
    t_start = time.perf_counter()
    try:
        ws = await asyncio.wait_for(
            websockets.connect(BINANCE_WS_URL, ping_interval=None, close_timeout=5),
            timeout=BINANCE_TIMEOUT,
        )
    except Exception as e:
        print(f"  WS Connect:        FAIL ({e})")
        results.binance_error = str(e)
        print()
        return

    connect_ms = (time.perf_counter() - t_start) * 1000
    results.binance_ws_connect_ms = connect_ms
    print(f"  WS Connect:        {connect_ms:.0f}ms")

    # --- WS Ping RTT ---
    try:
        t_ping = time.perf_counter()
        pong = await ws.ping()
        await asyncio.wait_for(pong, timeout=5)
        ping_rtt_ms = (time.perf_counter() - t_ping) * 1000
        results.binance_ping_rtt_ms = ping_rtt_ms
        print(f"  WS Ping RTT:       {ping_rtt_ms:.0f}ms (one-way est: {ping_rtt_ms/2:.0f}ms)")
    except Exception as e:
        print(f"  WS Ping:           FAIL ({e})")

    # --- Collect bookTicker samples ---
    latencies = []
    best_bid = 0.0
    best_ask = 0.0
    try:
        for _ in range(BINANCE_SAMPLE_COUNT):
            raw = await asyncio.wait_for(ws.recv(), timeout=BINANCE_TIMEOUT)
            t_recv = time.time() * 1000  # epoch ms
            msg = json.loads(raw)

            if msg.get("e") == "bookTicker":
                server_time = msg["E"]
                latency = t_recv - server_time
                latencies.append(latency)
                best_bid = float(msg["b"])
                best_ask = float(msg["a"])
    except asyncio.TimeoutError:
        print(f"  Ticker stream:     TIMEOUT (got {len(latencies)}/{BINANCE_SAMPLE_COUNT} samples)")
    except Exception as e:
        print(f"  Ticker stream:     ERROR ({e})")

    await ws.close()

    if latencies:
        latencies.sort()
        results.binance_latency_min_ms = latencies[0]
        results.binance_latency_median_ms = latencies[len(latencies) // 2]
        results.binance_latency_max_ms = latencies[-1]
        results.binance_best_bid = best_bid
        results.binance_best_ask = best_ask

        print(f"  Ticker Latency:    {results.binance_latency_median_ms:.0f}ms (median, N={len(latencies)})")
        print(f"    Min: {results.binance_latency_min_ms:.0f}ms  Max: {results.binance_latency_max_ms:.0f}ms")
        print(f"  Best Bid: ${best_bid:.2f}  Best Ask: ${best_ask:.2f}")
    else:
        results.binance_error = "no samples received"
        print("  Ticker Latency:    NO DATA")

    print()


# ------------------------------------------------------------------
# Test 1: Geo-Block Detection
# ------------------------------------------------------------------
async def test_geo_block():
    """Connect to Lighter WS, subscribe to orderbook, detect geo-blocks."""
    print("[1/2] Geo-Block Test")

    t_start = time.perf_counter()

    try:
        ws = await asyncio.wait_for(
            websockets.connect(WS_URL, ping_interval=None, close_timeout=5),
            timeout=GEO_BLOCK_TIMEOUT,
        )
    except websockets.exceptions.InvalidStatusCode as e:
        results.geo_blocked = True
        if e.status_code in (403, 451):
            print(f"  WebSocket Connect:   FAIL (HTTP {e.status_code} - geo-restricted)")
        else:
            print(f"  WebSocket Connect:   FAIL (HTTP {e.status_code})")
        return None
    except asyncio.TimeoutError:
        results.geo_blocked = True
        print(f"  WebSocket Connect:   FAIL (timeout after {GEO_BLOCK_TIMEOUT}s - possible geo-block)")
        return None
    except OSError as e:
        results.geo_blocked = True
        err = str(e)
        if "Name or service not known" in err or "Network is unreachable" in err:
            print(f"  WebSocket Connect:   FAIL (DNS/network block: {err})")
        else:
            print(f"  WebSocket Connect:   FAIL ({err})")
        return None
    except Exception as e:
        results.geo_blocked = True
        print(f"  WebSocket Connect:   FAIL ({type(e).__name__}: {e})")
        return None

    connect_ms = (time.perf_counter() - t_start) * 1000
    results.ws_connect_ms = connect_ms
    results.geo_blocked = False
    print(f"  WebSocket Connect:   PASS ({connect_ms:.0f}ms)")

    # Wait for "connected" message
    try:
        msg = await asyncio.wait_for(ws.recv(), timeout=5)
        data = json.loads(msg)
        if data.get("type") != "connected":
            print(f"  Unexpected first message: {data.get('type')}")
    except (asyncio.TimeoutError, json.JSONDecodeError) as e:
        print(f"  Warning: No 'connected' message ({e})")

    # Subscribe to orderbook
    t_sub = time.perf_counter()
    await ws.send(json.dumps({"type": "subscribe", "channel": f"order_book/{MARKET_INDEX}"}))

    # Wait for orderbook snapshot
    best_bid = 0.0
    best_ask = 0.0
    try:
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=GEO_BLOCK_TIMEOUT)
            data = json.loads(msg)
            msg_type = data.get("type", "")

            if msg_type == "subscribed/order_book":
                sub_ms = (time.perf_counter() - t_sub) * 1000
                results.orderbook_sub_ms = sub_ms
                print(f"  Orderbook Subscribe: PASS ({sub_ms:.0f}ms)")

                ob = data.get("order_book", {})
                bids = ob.get("bids", [])
                asks = ob.get("asks", [])

                if bids:
                    bids.sort(key=lambda x: float(x["price"]), reverse=True)
                    best_bid = float(bids[0]["price"])
                if asks:
                    asks.sort(key=lambda x: float(x["price"]))
                    best_ask = float(asks[0]["price"])

                results.best_bid = best_bid
                results.best_ask = best_ask
                print(f"  Best Bid: ${best_bid:.2f}  Best Ask: ${best_ask:.2f}")
                break

            elif msg_type == "ping":
                await ws.send(json.dumps({"type": "pong"}))
    except asyncio.TimeoutError:
        print(f"  Orderbook Subscribe: FAIL (timeout)")
        results.geo_blocked = True
    except Exception as e:
        print(f"  Orderbook Subscribe: FAIL ({e})")

    await ws.close()
    print()
    return ws if not results.geo_blocked else None


# ------------------------------------------------------------------
# Pre-Flight Checks
# ------------------------------------------------------------------
async def pre_flight():
    """Initialize signer client, verify credentials, check account state."""
    global _signer_client
    print("[Pre-flight]")

    # Init signer client
    try:
        signer = lighter.SignerClient(
            url=API_URL,
            account_index=ACCOUNT_INDEX,
            api_private_keys={API_KEY_INDEX: PRIVATE_KEY},
        )
    except Exception as e:
        print(f"  Credentials:       FAIL ({e})")
        return None

    err = signer.check_client()
    if err is not None:
        print(f"  Credentials:       FAIL ({err})")
        return None

    print("  Credentials:       OK")
    _signer_client = signer

    # Query account
    api_client = lighter.ApiClient(configuration=lighter.Configuration(host=API_URL))
    account_api = AccountApi(api_client)

    try:
        resp = await account_api.account(by="index", value=str(ACCOUNT_INDEX))
        if resp.accounts and len(resp.accounts) > 0:
            acct = resp.accounts[0]
            collateral = float(acct.collateral)
            results.balance_usdc = collateral
            print(f"  Balance:           ${collateral:.2f} USDC")

            # Check position
            positions = getattr(acct, "positions", None) or []
            has_position = False
            for pos in positions:
                pos_sign = getattr(pos, "sign", 0)
                pos_size = getattr(pos, "position", "0")
                market = getattr(pos, "market_index", None)
                if market is not None and int(market) == MARKET_INDEX and float(pos_size) != 0:
                    side = "LONG" if int(pos_sign) == 1 else "SHORT"
                    results.position_str = f"{side} {pos_size} ETH"
                    has_position = True
                    print(f"  Position:          {results.position_str} (WARNING: existing position)")
                    break

            if not has_position:
                results.position_str = "FLAT"
                print("  Position:          FLAT")

            if collateral < 5.0:
                print(f"  WARNING: Balance below $5. Taker test may fail.")
        else:
            print("  Account query returned no data")
    except Exception as e:
        print(f"  Account query:     WARN ({e})")

    await api_client.close()

    results.preflight_ok = True
    print()
    return signer


# ------------------------------------------------------------------
# Test 2: Taker Order Latency (WebSocket)
# ------------------------------------------------------------------
def _sign_order(signer, size_wei, is_ask, worst_price):
    """Sign a market order. Returns (order_index, payload_dict, error)."""
    order_index = int(time.time() * 1000) % 2**31

    api_key_index, nonce = signer.nonce_manager.next_nonce()
    tx_type, tx_info, tx_hash, err = signer.sign_create_order(
        market_index=MARKET_INDEX,
        client_order_index=order_index,
        base_amount=size_wei,
        price=worst_price,
        is_ask=is_ask,
        order_type=signer.ORDER_TYPE_MARKET,
        time_in_force=signer.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
        order_expiry=signer.DEFAULT_IOC_EXPIRY,
        nonce=nonce,
        api_key_index=api_key_index,
    )
    if err is not None:
        return order_index, None, f"sign: {err}"

    payload = {
        "type": "jsonapi/sendtx",
        "data": {
            "id": f"req_{int(time.time()*1000)}",
            "tx_type": tx_type,
            "tx_info": json.loads(tx_info),
        },
    }
    return order_index, payload, None


async def _send_and_measure(signer, order_ws, size_wei, is_ask, worst_price):
    """Sign and send a market order, capturing granular timestamps.

    Returns (order_index, t0, t1, t3, ws_response, error).
    t0 = signal (before signing), t1 = signing done, t3 = ack received.
    """
    t0 = time.perf_counter()
    order_index, payload, err = _sign_order(signer, size_wei, is_ask, worst_price)
    t1 = time.perf_counter()

    if err is not None:
        return order_index, t0, t1, None, None, err

    await order_ws.send(json.dumps(payload))

    try:
        ws_resp = await asyncio.wait_for(order_ws.recv(), timeout=ORDER_TIMEOUT)
        t3 = time.perf_counter()
        return order_index, t0, t1, t3, ws_resp, None
    except asyncio.TimeoutError:
        return order_index, t0, t1, None, None, "ws response timeout"


def _is_fill_for_order(msg, order_index, is_ask):
    """Check if a WS trade message corresponds to our order.

    For a BUY (is_ask=False), we are the bid side.
    For a SELL (is_ask=True), we are the ask side.
    """
    msg_type = msg.get("type", "")
    # Accept both account_all_trades and account_all update messages
    if "update" not in msg_type:
        return False

    # Look for trades in the message — the format may be:
    # {"trades": {"0": [...]}} keyed by market_index, or
    # {"trades": [...]} as a flat list
    trades = msg.get("trades", {})
    if isinstance(trades, dict):
        # Keyed by market_index
        market_trades = trades.get(str(MARKET_INDEX), [])
    elif isinstance(trades, list):
        market_trades = trades
    else:
        return False

    for trade in market_trades:
        # Match by account_id
        if is_ask:
            acct_id = trade.get("ask_account_id")
            client_id = trade.get("ask_client_id")
        else:
            acct_id = trade.get("bid_account_id")
            client_id = trade.get("bid_client_id")

        if acct_id is not None and int(acct_id) != ACCOUNT_INDEX:
            continue

        # If client_order_index is present, match it
        if client_id is not None and int(client_id) == order_index:
            return True

        # If no client_id field or it doesn't match, accept by account_id alone
        if acct_id is not None and int(acct_id) == ACCOUNT_INDEX:
            return True

    return False


async def _wait_for_fill(fill_ws, order_index, is_ask, timeout=FILL_TIMEOUT):
    """Wait for a fill notification on the fill listener WS.

    Handles ping/pong and filters for our specific order.
    Returns the fill message or None on timeout.
    """
    deadline = time.perf_counter() + timeout
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return None
        try:
            raw = await asyncio.wait_for(fill_ws.recv(), timeout=remaining)
        except asyncio.TimeoutError:
            return None
        msg = json.loads(raw)
        if msg.get("type") == "ping":
            await fill_ws.send(json.dumps({"type": "pong"}))
            continue
        if _is_fill_for_order(msg, order_index, is_ask):
            return msg


async def _setup_fill_listener():
    """Connect fill listener WS and subscribe to account trades.

    Returns (fill_ws, setup_ms) or (None, None) on failure.
    """
    t_start = time.perf_counter()
    try:
        fill_ws = await asyncio.wait_for(
            websockets.connect(WS_URL, ping_interval=None, close_timeout=5),
            timeout=GEO_BLOCK_TIMEOUT,
        )
    except Exception as e:
        print(f"  Fill Listener:     FAIL connect ({e})")
        return None, None

    # Drain "connected" message
    try:
        init_msg = await asyncio.wait_for(fill_ws.recv(), timeout=5)
        init_data = json.loads(init_msg)
        if init_data.get("type") != "connected":
            print(f"  Fill Listener:     unexpected init: {init_data.get('type')}")
    except Exception as e:
        print(f"  Fill Listener:     no connected msg ({e})")
        await fill_ws.close()
        return None, None

    # Subscribe to account trades
    channel = f"account_all_trades/{ACCOUNT_INDEX}"
    await fill_ws.send(json.dumps({"type": "subscribe", "channel": channel}))

    # Drain subscription snapshot
    try:
        while True:
            raw = await asyncio.wait_for(fill_ws.recv(), timeout=5)
            data = json.loads(raw)
            msg_type = data.get("type", "")
            if msg_type == "ping":
                await fill_ws.send(json.dumps({"type": "pong"}))
                continue
            if "subscribed" in msg_type:
                break
            # If we get an error, the channel might not exist
            if "error" in msg_type or data.get("error"):
                err_msg = data.get("error", msg_type)
                print(f"  Fill Listener:     subscription error: {err_msg}")
                print(f"  Fill Listener:     falling back to account_all/{ACCOUNT_INDEX}")
                # Fallback: try account_all channel
                channel = f"account_all/{ACCOUNT_INDEX}"
                await fill_ws.send(json.dumps({"type": "subscribe", "channel": channel}))
                raw2 = await asyncio.wait_for(fill_ws.recv(), timeout=5)
                data2 = json.loads(raw2)
                if "subscribed" in data2.get("type", ""):
                    break
                print(f"  Fill Listener:     fallback also failed")
                await fill_ws.close()
                return None, None
    except asyncio.TimeoutError:
        print(f"  Fill Listener:     subscription timeout")
        await fill_ws.close()
        return None, None

    setup_ms = (time.perf_counter() - t_start) * 1000
    print(f"  Fill Listener:     ready ({setup_ms:.0f}ms, channel={channel})")
    return fill_ws, setup_ms


def _check_ack_error(ws_resp, label):
    """Parse WS ack response and check for errors. Returns error message or None."""
    if not ws_resp:
        return None
    try:
        resp_data = json.loads(ws_resp)
        resp_str = json.dumps(resp_data, indent=None)
        if len(resp_str) > 200:
            resp_str = resp_str[:200] + "..."
        print(f"  WS Response:       {resp_str}")
        err_msg = resp_data.get("error") or resp_data.get("data", {}).get("error")
        if err_msg:
            print(f"  {label} rejected:     {err_msg}")
            return f"{label.lower()} rejected: {err_msg}"
    except json.JSONDecodeError:
        print(f"  WS Response:       {ws_resp[:200]}")
    return None


async def test_taker_latency(signer, best_ask, best_bid):
    """Place market BUY via WS, then SELL to flatten. Measure signal-to-fill latency."""
    print("[2/2] Taker Signal-to-Fill Latency Test")

    if best_ask <= 0 or best_bid <= 0:
        print("  SKIP: No valid orderbook data")
        results.taker_error = "no orderbook data"
        print()
        return

    fill_ws = None
    order_ws = None

    try:
        # --- Setup fill listener WS (must be ready before placing orders) ---
        fill_ws, setup_ms = await _setup_fill_listener()
        if fill_ws is None:
            print("  WARNING: Fill listener failed — will measure ack latency only")
        else:
            results.fill_listener_setup_ms = setup_ms

        # --- Setup order sender WS ---
        try:
            order_ws = await asyncio.wait_for(
                websockets.connect(WS_URL, ping_interval=None),
                timeout=ORDER_TIMEOUT,
            )
            init_msg = await asyncio.wait_for(order_ws.recv(), timeout=5)
            init_data = json.loads(init_msg)
            if init_data.get("type") == "connected":
                print(f"  Order WS:          connected")
            else:
                print(f"  Order WS:          unexpected init: {init_data.get('type')}")
        except Exception as e:
            print(f"  Order WS Connect:  FAIL ({e})")
            results.taker_error = f"order ws connect: {e}"
            print()
            return

        size_wei = TEST_SIZE_WEI
        size_eth = size_wei / 1e4

        # --- Market BUY ---
        print(f"  Market BUY {size_eth:.4f} ETH")
        worst_buy_price = int(best_ask * (1 + SLIPPAGE) * 100)

        oidx, t0, t1, t3, ws_resp, err = await _send_and_measure(
            signer, order_ws, size_wei, False, worst_buy_price
        )

        if err is not None:
            print(f"  BUY failed:        {err}")
            if size_wei == TEST_SIZE_WEI:
                print(f"  Retrying with {FALLBACK_SIZE_WEI / 1e4:.4f} ETH...")
                size_wei = FALLBACK_SIZE_WEI
                size_eth = size_wei / 1e4
                worst_buy_price = int(best_ask * (1 + SLIPPAGE) * 100)
                oidx, t0, t1, t3, ws_resp, err = await _send_and_measure(
                    signer, order_ws, size_wei, False, worst_buy_price
                )

        if err is not None:
            print(f"  BUY failed:        {err}")
            results.taker_error = err
            print()
            return

        ack_err = _check_ack_error(ws_resp, "BUY")
        if ack_err:
            results.taker_error = ack_err
            print()
            return

        # Store signing and ack timing
        results.taker_buy_signing_ms = (t1 - t0) * 1000
        results.taker_buy_send_to_ack_ms = (t3 - t1) * 1000
        print(f"  BUY Signing:       {results.taker_buy_signing_ms:.0f}ms")
        print(f"  BUY Send->Ack:     {results.taker_buy_send_to_ack_ms:.0f}ms")

        # Wait for fill
        if fill_ws is not None:
            fill_msg = await _wait_for_fill(fill_ws, oidx, False)
            if fill_msg is not None:
                t4 = time.perf_counter()
                results.taker_buy_ack_to_fill_ms = (t4 - t3) * 1000
                results.taker_buy_s2f_ms = (t4 - t0) * 1000
                print(f"  BUY Ack->Fill:     {results.taker_buy_ack_to_fill_ms:.0f}ms")
                print(f"  BUY Total S2F:     {results.taker_buy_s2f_ms:.0f}ms")
            else:
                print(f"  BUY Ack->Fill:     TIMEOUT ({FILL_TIMEOUT}s) — IOC likely expired unfilled")
                # Still report what we have (ack-based)
                results.taker_buy_s2f_ms = None
        else:
            # No fill listener — report ack-based timing only
            results.taker_buy_s2f_ms = (t3 - t0) * 1000
            print(f"  BUY Total (ack):   {results.taker_buy_s2f_ms:.0f}ms (no fill listener)")

        # --- Market SELL (flatten) ---
        print(f"  Market SELL {size_eth:.4f} ETH (flatten)")
        worst_sell_price = int(best_bid * (1 - SLIPPAGE) * 100)

        for attempt in range(3):
            oidx, t0, t1, t3, ws_resp, err = await _send_and_measure(
                signer, order_ws, size_wei, True, worst_sell_price
            )

            if err is not None:
                print(f"  SELL failed:       {err} (attempt {attempt+1}/3)")
                if attempt < 2:
                    await asyncio.sleep(1)
                    continue
                results.taker_error = f"sell: {err}"
                break

            ack_err = _check_ack_error(ws_resp, "SELL")
            if ack_err:
                if attempt < 2:
                    print(f"  (attempt {attempt+1}/3)")
                    await asyncio.sleep(1)
                    continue
                results.taker_error = ack_err
                break

            # Store signing and ack timing
            results.taker_sell_signing_ms = (t1 - t0) * 1000
            results.taker_sell_send_to_ack_ms = (t3 - t1) * 1000
            print(f"  SELL Signing:      {results.taker_sell_signing_ms:.0f}ms")
            print(f"  SELL Send->Ack:    {results.taker_sell_send_to_ack_ms:.0f}ms")

            # Wait for fill
            if fill_ws is not None:
                fill_msg = await _wait_for_fill(fill_ws, oidx, True)
                if fill_msg is not None:
                    t4 = time.perf_counter()
                    results.taker_sell_ack_to_fill_ms = (t4 - t3) * 1000
                    results.taker_sell_s2f_ms = (t4 - t0) * 1000
                    print(f"  SELL Ack->Fill:    {results.taker_sell_ack_to_fill_ms:.0f}ms")
                    print(f"  SELL Total S2F:    {results.taker_sell_s2f_ms:.0f}ms")
                else:
                    print(f"  SELL Ack->Fill:    TIMEOUT ({FILL_TIMEOUT}s)")
                    results.taker_sell_s2f_ms = None
            else:
                results.taker_sell_s2f_ms = (t3 - t0) * 1000
                print(f"  SELL Total (ack):  {results.taker_sell_s2f_ms:.0f}ms (no fill listener)")
            break

    finally:
        if order_ws is not None:
            try:
                await order_ws.close()
            except Exception:
                pass
        if fill_ws is not None:
            try:
                await fill_ws.close()
            except Exception:
                pass

    print()


# ------------------------------------------------------------------
# Cleanup: Verify final account state
# ------------------------------------------------------------------
async def cleanup(signer):
    """Query final account state and warn if position is open."""
    print("[Cleanup]")

    api_client = lighter.ApiClient(configuration=lighter.Configuration(host=API_URL))
    account_api = AccountApi(api_client)

    try:
        resp = await account_api.account(by="index", value=str(ACCOUNT_INDEX))
        if resp.accounts and len(resp.accounts) > 0:
            acct = resp.accounts[0]
            collateral = float(acct.collateral)
            results.cleanup_balance = collateral

            positions = getattr(acct, "positions", None) or []
            has_position = False
            for pos in positions:
                pos_sign = getattr(pos, "sign", 0)
                pos_size = getattr(pos, "position", "0")
                market = getattr(pos, "market_index", None)
                if market is not None and int(market) == MARKET_INDEX and float(pos_size) != 0:
                    side = "LONG" if int(pos_sign) == 1 else "SHORT"
                    results.cleanup_position = f"{side} {pos_size} ETH"
                    has_position = True
                    print(f"  Position:          {results.cleanup_position}")
                    print(f"  WARNING: Account has open position! Flatten manually.")
                    break

            if not has_position:
                results.cleanup_position = "FLAT (verified)"
                print(f"  Position:          FLAT (verified)")

            print(f"  Balance:           ${collateral:.2f} USDC")
    except Exception as e:
        print(f"  Cleanup query:     WARN ({e})")

    await api_client.close()

    # Close signer client
    try:
        await signer.close()
    except Exception:
        pass

    print()


# ------------------------------------------------------------------
# SIGINT handler
# ------------------------------------------------------------------
def _sigint_handler(sig, frame):
    global _cleanup_done
    if _cleanup_done:
        sys.exit(130)

    print("\n\nInterrupted! Attempting cleanup...")
    _cleanup_done = True

    if _signer_client is not None:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_emergency_cleanup())
            else:
                loop.run_until_complete(_emergency_cleanup())
        except Exception as e:
            print(f"  Cleanup failed: {e}")

    sys.exit(130)


async def _emergency_cleanup():
    """Best-effort cleanup on Ctrl+C."""
    if _signer_client is None:
        return
    try:
        # Cancel all orders as a safety measure
        await _signer_client.cancel_all_orders(
            time_in_force=_signer_client.CANCEL_ALL_TIF_IMMEDIATE,
            timestamp_ms=int(time.time() * 1000),
        )
        print("  Cancelled all orders.")
    except Exception as e:
        print(f"  Cancel all orders failed: {e}")

    try:
        await _signer_client.close()
    except Exception:
        pass


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
async def main():
    signal.signal(signal.SIGINT, _sigint_handler)

    _print_header()

    # Test 0: Binance latency (baseline)
    await test_binance_latency()

    # Test 1: Geo-block
    await test_geo_block()

    if results.geo_blocked:
        _print_summary()
        return 1

    if results.best_bid is None or results.best_bid <= 0:
        print("ERROR: Could not get orderbook data. Aborting.")
        _print_summary()
        return 3

    # Pre-flight
    signer = await pre_flight()
    if signer is None:
        _print_summary()
        return 2

    # Test 2: Taker latency (buy + sell)
    await test_taker_latency(signer, results.best_ask, results.best_bid)

    # Cleanup
    await cleanup(signer)

    _print_summary()

    # Determine exit code
    if results.taker_error:
        return 3
    return 0


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
