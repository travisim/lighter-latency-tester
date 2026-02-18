---
title: Add Binance Perps Ticker Latency Test
type: feat
date: 2026-02-17
---

# Add Binance Perps Ticker Latency Test

## Overview

Add a new test phase that measures latency to Binance USDM perpetual futures WebSocket for BTCUSDT bookTicker data. Runs **before** the existing Lighter tests. Measures both server-timestamp-delta one-way latency and WebSocket ping RTT.

## Proposed Solution

Add a `test_binance_latency()` async function to `test_lighter_connectivity.py` that:

1. Connects to `wss://fstream.binance.com/ws/btcusdt@bookTicker`
2. Measures WS handshake time
3. Sends a WS ping and measures RTT
4. Collects ~20 bookTicker messages
5. For each message, computes one-way latency: `local_time_ms - message["E"]`
6. Reports min/median/max latency, ping RTT, and best bid/ask

No authentication required — this is a public stream.

## Implementation

### Changes to `test_lighter_connectivity.py`

#### 1. Add constants (after line 43)

```python
# Binance Futures
BINANCE_WS_URL = "wss://fstream.binance.com/ws/btcusdt@bookTicker"
BINANCE_SAMPLE_COUNT = 20
BINANCE_TIMEOUT = 10  # seconds
```

#### 2. Add fields to `Results.__init__` (after line 61)

```python
# Binance
self.binance_ws_connect_ms = None
self.binance_ping_rtt_ms = None
self.binance_latency_min_ms = None
self.binance_latency_median_ms = None
self.binance_latency_max_ms = None
self.binance_best_bid = None
self.binance_best_ask = None
self.binance_error = None
```

#### 3. Add `test_binance_latency()` function (before `test_geo_block`, around line 115)

```python
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
        for i in range(BINANCE_SAMPLE_COUNT):
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
```

**Key details:**
- `ws.ping()` is the `websockets` library's built-in ping/pong — returns an awaitable that completes when pong arrives
- `time.time() * 1000` for epoch ms comparison against Binance `E` field (both epoch ms)
- `time.perf_counter()` for connect/ping timing (relative, not epoch)
- Binance bookTicker is push-only — no subscription message needed, the stream name is in the URL

#### 4. Update `_print_header()` (line 72)

Change the title to reflect both tests:

```python
def _print_header():
    print("=" * 60)
    print("CONNECTIVITY & LATENCY TEST")
    print("=" * 60)
    # ... rest unchanged
```

#### 5. Update `_print_summary()` (after line 87)

Add Binance section before the Lighter section:

```python
def _print_summary():
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)

    # Binance section
    if results.binance_ws_connect_ms is not None:
        print("  --- Binance BTCUSDT Perps ---")
        print(f"  WS Connect:         {results.binance_ws_connect_ms:.0f}ms")
        if results.binance_ping_rtt_ms is not None:
            print(f"  WS Ping RTT:        {results.binance_ping_rtt_ms:.0f}ms (one-way est: {results.binance_ping_rtt_ms/2:.0f}ms)")
        if results.binance_latency_median_ms is not None:
            print(f"  Ticker Latency:     {results.binance_latency_median_ms:.0f}ms (median)")
            print(f"    Min: {results.binance_latency_min_ms:.0f}ms  Max: {results.binance_latency_max_ms:.0f}ms")
        if results.binance_best_bid is not None:
            print(f"  Best Bid: ${results.binance_best_bid:.2f}  Best Ask: ${results.binance_best_ask:.2f}")
        if results.binance_error:
            print(f"  Error:              {results.binance_error}")
        print()

    # Lighter section (existing, prefixed)
    print("  --- Lighter ETH/USDT.p ---")
    # ... existing geo_blocked, ws_connect, orderbook, taker lines ...
```

#### 6. Update `main()` flow (line 552)

Insert Binance test as the first test, update step numbering:

```python
async def main():
    signal.signal(signal.SIGINT, _sigint_handler)

    _print_header()

    # Test 0: Binance latency (baseline)
    await test_binance_latency()

    # Test 1: Geo-block (update print inside test_geo_block)
    await test_geo_block()
    # ... rest unchanged
```

Update `test_geo_block` print from `[1/2]` to `[1/3]` and `test_taker_latency` from `[2/2]` to `[2/3]`.

Wait — the Binance test is unnumbered (it's a baseline). Better: keep numbering for Lighter tests only, and label Binance as `[Binance]` as shown above.

## Acceptance Criteria

- [ ] `test_binance_latency()` connects to `wss://fstream.binance.com/ws/btcusdt@bookTicker`
- [ ] WS handshake time measured and reported
- [ ] WS ping RTT measured via `ws.ping()` and reported with one-way estimate
- [ ] 20 bookTicker messages collected, one-way latency computed from `E` field
- [ ] Min/median/max latency reported
- [ ] Best bid/ask from BTCUSDT displayed
- [ ] Binance test runs before Lighter tests
- [ ] Binance failure does not prevent Lighter tests from running
- [ ] Summary output shows Binance section followed by Lighter section
- [ ] No new dependencies (uses existing `websockets` library)

## Notes

- **Clock sync matters:** One-way latency accuracy depends on NTP. On AWS instances this is typically <1ms drift. On consumer machines, expect 5-20ms systematic offset. The ping RTT measurement serves as a clock-independent cross-check.
- **No auth needed:** bookTicker is a public stream — no API keys or signing required.
- **Binance rate limits:** Single stream connections are not rate-limited. No concern here.
