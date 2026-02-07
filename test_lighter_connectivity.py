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

# WS URL derived from API URL
WS_URL = API_URL.replace("https://", "wss://") + "/stream"


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
        self.taker_buy_ms = None
        self.taker_sell_ms = None
        self.taker_error = None
        self.cleanup_position = "UNKNOWN"
        self.cleanup_balance = None


results = Results()

# Global state for cleanup on Ctrl+C
_signer_client = None
_cleanup_done = False


def _print_header():
    print("=" * 60)
    print("LIGHTER CONNECTIVITY & LATENCY TEST")
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

    if results.geo_blocked:
        print("  Geo-Blocked:        YES")
        print("  (Test skipped)")
    else:
        print("  Geo-Blocked:        NO")

    if results.ws_connect_ms is not None:
        print(f"  WS Connect:         {results.ws_connect_ms:.0f}ms")
    if results.orderbook_sub_ms is not None:
        print(f"  Orderbook Sub:      {results.orderbook_sub_ms:.0f}ms")

    if results.taker_buy_ms is not None:
        print(f"  Taker BUY Latency:  {results.taker_buy_ms:.0f}ms")
    elif results.taker_error:
        print(f"  Taker Test:         FAILED ({results.taker_error})")

    if results.taker_sell_ms is not None:
        print(f"  Taker SELL Latency: {results.taker_sell_ms:.0f}ms")

    if results.taker_buy_ms is not None and results.taker_sell_ms is not None:
        avg = (results.taker_buy_ms + results.taker_sell_ms) / 2
        print(f"  Average Latency:    {avg:.0f}ms")

    print("=" * 60)


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
async def _send_market_order_ws(signer, order_ws, size_wei, is_ask, worst_price):
    """Sign a market order and send via WebSocket. Returns (order_index, ws_response, error)."""
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

    await order_ws.send(json.dumps(payload))

    try:
        ws_resp = await asyncio.wait_for(order_ws.recv(), timeout=ORDER_TIMEOUT)
        return order_index, ws_resp, None
    except asyncio.TimeoutError:
        return order_index, None, "ws response timeout"


async def test_taker_latency(signer, best_ask, best_bid):
    """Place market BUY via WS, then SELL to flatten. Measure round-trip."""
    print("[2/2] Taker Latency Test (WebSocket)")

    if best_ask <= 0 or best_bid <= 0:
        print("  SKIP: No valid orderbook data")
        results.taker_error = "no orderbook data"
        print()
        return

    # Open order WS and drain initial "connected" message
    try:
        order_ws = await asyncio.wait_for(
            websockets.connect(WS_URL, ping_interval=None),
            timeout=ORDER_TIMEOUT,
        )
        # Drain the "connected" message so it doesn't interfere with order responses
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

    t_buy_start = time.perf_counter()
    oidx, ws_resp, err = await _send_market_order_ws(signer, order_ws, size_wei, False, worst_buy_price)
    buy_ms = (time.perf_counter() - t_buy_start) * 1000

    if err is not None:
        print(f"  BUY failed:        {err}")
        # Try with larger size
        if size_wei == TEST_SIZE_WEI:
            print(f"  Retrying with {FALLBACK_SIZE_WEI / 1e4:.4f} ETH...")
            size_wei = FALLBACK_SIZE_WEI
            size_eth = size_wei / 1e4
            worst_buy_price = int(best_ask * (1 + SLIPPAGE) * 100)
            t_buy_start = time.perf_counter()
            oidx, ws_resp, err = await _send_market_order_ws(signer, order_ws, size_wei, False, worst_buy_price)
            buy_ms = (time.perf_counter() - t_buy_start) * 1000

    if err is not None:
        print(f"  BUY failed:        {err}")
        results.taker_error = err
        await order_ws.close()
        print()
        return

    # Parse WS response to check for errors
    resp_data = None
    if ws_resp:
        try:
            resp_data = json.loads(ws_resp)
            resp_str = json.dumps(resp_data, indent=None)
            if len(resp_str) > 200:
                resp_str = resp_str[:200] + "..."
            print(f"  WS Response:       {resp_str}")
        except json.JSONDecodeError:
            print(f"  WS Response:       {ws_resp[:200]}")

    if resp_data:
        err_msg = resp_data.get("error") or resp_data.get("data", {}).get("error")
        if err_msg:
            print(f"  BUY rejected:      {err_msg}")
            results.taker_error = f"buy rejected: {err_msg}"
            await order_ws.close()
            print()
            return

    results.taker_buy_ms = buy_ms
    print(f"  BUY round-trip:    {buy_ms:.0f}ms")

    # --- Market SELL (flatten) ---
    print(f"  Market SELL {size_eth:.4f} ETH (flatten)")
    worst_sell_price = int(best_bid * (1 - SLIPPAGE) * 100)

    for attempt in range(3):
        t_sell_start = time.perf_counter()
        oidx, ws_resp, err = await _send_market_order_ws(signer, order_ws, size_wei, True, worst_sell_price)
        sell_ms = (time.perf_counter() - t_sell_start) * 1000

        if err is not None:
            print(f"  SELL failed:       {err} (attempt {attempt+1}/3)")
            if attempt < 2:
                await asyncio.sleep(1)
                continue
            results.taker_error = f"sell: {err}"
            break

        # Parse response
        resp_data = None
        if ws_resp:
            try:
                resp_data = json.loads(ws_resp)
                resp_str = json.dumps(resp_data, indent=None)
                if len(resp_str) > 200:
                    resp_str = resp_str[:200] + "..."
                print(f"  WS Response:       {resp_str}")
            except json.JSONDecodeError:
                print(f"  WS Response:       {ws_resp[:200]}")

            err_msg = resp_data.get("error") or resp_data.get("data", {}).get("error") if resp_data else None
            if err_msg:
                print(f"  SELL rejected:     {err_msg} (attempt {attempt+1}/3)")
                if attempt < 2:
                    await asyncio.sleep(1)
                    continue
                results.taker_error = f"sell rejected: {err_msg}"
                break

        results.taker_sell_ms = sell_ms
        print(f"  SELL round-trip:   {sell_ms:.0f}ms")
        break

    await order_ws.close()
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
