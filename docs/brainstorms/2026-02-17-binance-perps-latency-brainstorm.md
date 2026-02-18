# Binance Perps Ticker Latency Test

**Date:** 2026-02-17
**Status:** Ready for planning

## What We're Building

Add a Binance perpetual futures latency test that measures how fast bookTicker data (best bid/ask) arrives from Binance's USDM futures WebSocket. This runs **before** the existing Lighter tests as a baseline/reference measurement.

**Pair:** BTCUSDT perpetual
**Stream:** `btcusdt@bookTicker` via `wss://fstream.binance.com/ws/btcusdt@bookTicker`
**No authentication required** — bookTicker is a public stream.

## Why This Approach

- bookTicker is the lowest-latency public stream Binance offers for price data
- Binance futures bookTicker messages include server-side event timestamp (`E` field, epoch ms), enabling true one-way latency measurement
- BTCUSDT is the most liquid perps pair, ensuring consistent update frequency
- Running before Lighter tests gives a baseline without interference

## Key Decisions

1. **Two measurement methods:**
   - **Server timestamp delta:** `local_receive_time_ms - message["E"]` for each bookTicker message. Measures real data propagation latency. Requires NTP-synced clock.
   - **WebSocket ping round-trip:** WS ping/pong RTT divided by 2. Measures transport-layer latency independent of clock sync.

2. **Integration point:** New test phase that runs first, before geo-block and taker tests.

3. **Sampling:** Collect ~10-20 bookTicker messages over a few seconds, report min/median/max for timestamp delta. Single ping measurement for WS RTT.

4. **Output format:** Add Binance section to the existing summary output block.

## Expected Output

```
============================================================
BINANCE PERPS LATENCY (BTCUSDT)
------------------------------------------------------------
  WS Connect:          XXms
  WS Ping RTT:         XXms (one-way est: XXms)
  Ticker Latency:      XXms (median, N=20)
    Min: XXms  Max: XXms
  Best Bid: $XXXXX.XX  Best Ask: $XXXXX.XX
============================================================
```

## Open Questions

- None — ready for implementation.
