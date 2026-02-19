---
title: "feat: Signal-to-fill latency measurement"
type: feat
date: 2026-02-18
---

# feat: Signal-to-fill latency measurement

## Overview

Modify the taker latency test in `test_lighter_connectivity.py` to measure true **signal-to-fill latency** — from the moment the script decides to trade to when the matching engine confirms the fill — using a dual-WebSocket architecture and the `account_all_trades` channel. Replace the current WebSocket round-trip measurement with a full latency breakdown: signing, send-to-ack, ack-to-fill, and total signal-to-fill.

## Problem Statement / Motivation

The current taker latency test measures WebSocket round-trip time (send order → receive server ack), which is **not** fill confirmation. The ack only means the server received the order — it says nothing about when the matching engine executed the trade. For a taker strategy, the operationally relevant metric is signal-to-fill: how long from "I want to trade" to "my trade was executed."

## Proposed Solution

### Architecture: Dual WebSocket

```
                    ┌─────────────────────────┐
                    │   Lighter WS Server      │
                    │                          │
  Fill Listener WS ─┤◄── account_all_trades    │
  (receives fills)  │    notifications         │
                    │                          │
  Order Sender WS ──┤──► send signed order     │
  (sends orders)    │◄── server ack            │
                    └─────────────────────────┘
```

1. **Fill Listener WS** — connects first, subscribes to `account_all_trades/{ACCOUNT_INDEX}`, drains snapshot, then waits for live fill notifications
2. **Order Sender WS** — connects second, sends signed IOC market orders and receives acks (existing pattern)

### Timing Points

```python
t0 = perf_counter()           # Signal: decision to trade
sign_order()
t1 = perf_counter()           # Signing complete
order_ws.send(payload)
t2 = perf_counter()           # Order sent (captured but not in summary)
ack = order_ws.recv()
t3 = perf_counter()           # Ack received
fill = fill_ws.recv()         # Wait for fill (with ping handling + timeout)
t4 = perf_counter()           # Fill received

# Reported breakdown:
# Signing:      t1 - t0
# Send-to-Ack:  t3 - t1
# Ack-to-Fill:  t4 - t3
# Total S2F:    t4 - t0
```

### Summary Output (New Format)

```
── Lighter ──────────────────────────────────────────
  WS connect:        45ms
  OB subscribe:      12ms
  Best bid / ask:    3024.50 / 3025.00
  Fill listener:     52ms  (setup)

  Taker BUY:
    Signing:          2ms
    Send → Ack:      38ms
    Ack → Fill:      11ms
    Total S2F:       51ms

  Taker SELL:
    Signing:          1ms
    Send → Ack:      35ms
    Ack → Fill:       9ms
    Total S2F:       45ms

  Average S2F:       48ms
```

## Implementation Phases

### Phase 0: Channel Discovery (Manual / Empirical)

Before writing code, verify the channel name and message format by connecting to the WS and subscribing.

**Tasks:**

- [x] Connect to `wss://mainnet.zklighter.elliot.ai/stream`
- [x] Subscribe to `account_all_trades/{ACCOUNT_INDEX}` — observe if `subscribed/account_all_trades` arrives
- [x] If that fails, try `account_all/{ACCOUNT_INDEX}` and inspect messages for trade data
- [x] Place a small market order via the existing script while logging all raw messages on the fill listener
- [x] Document: exact channel name, subscription confirmation message type, fill update message type, fill message structure, whether `client_order_index` is present in fill data, whether auth is required

**Output:** Confirmed channel name, message format, and correlation field(s).

### Phase 1: Update Results Class and Summary

**File:** `test_lighter_connectivity.py`

- [x] Add new fields to `Results` class (around line 51):
  - `fill_listener_setup_ms: float = 0` — fill listener connection + subscription time
  - `taker_buy_signing_ms: float = 0`
  - `taker_buy_send_to_ack_ms: float = 0`
  - `taker_buy_ack_to_fill_ms: float = 0` — `None` if fill times out
  - `taker_buy_s2f_ms: float = 0` — total signal-to-fill
  - Same four fields for SELL (`taker_sell_signing_ms`, etc.)
  - Keep `taker_buy_ms` and `taker_sell_ms` as the old round-trip for backward compatibility, or remove them

- [x] Update `_print_summary()` (around line 97) to display the new breakdown format shown above

### Phase 2: Fill Listener Setup

**File:** `test_lighter_connectivity.py`

Add fill listener setup inside `_test_taker_latency()` (around line 440), **before** the order sender WS.

- [x] Open fill listener WebSocket to `WS_URL`
- [x] Drain initial `"connected"` message (critical — learned from previous bug)
- [x] Subscribe to the confirmed channel (e.g., `account_all_trades/{ACCOUNT_INDEX}`)
- [x] Drain subscription snapshot message (`subscribed/account_all_trades`)
- [x] Record `fill_listener_setup_ms` in Results
- [x] Wrap in try/except with timeout (reuse `GEO_BLOCK_TIMEOUT`)

### Phase 3: Refactor Order Sending for Granular Timestamps

**File:** `test_lighter_connectivity.py`

Replace the current BUY/SELL flow (lines ~467-560) with granular timestamp capture.

- [x] Create helper `_send_and_measure()` that captures t0, t1, t2, t3 individually:
  ```python
  async def _send_and_measure(signer, order_ws, size_wei, is_ask, worst_price):
      t0 = time.perf_counter()
      order_index, payload, sign_err = _sign_order(signer, size_wei, is_ask, worst_price)
      t1 = time.perf_counter()
      if sign_err:
          return None, sign_err
      await order_ws.send(json.dumps(payload))
      t2 = time.perf_counter()
      ack = await asyncio.wait_for(order_ws.recv(), timeout=ORDER_TIMEOUT)
      t3 = time.perf_counter()
      return TimingResult(t0, t1, t2, t3, order_index, ack), None
  ```
- [x] Extract signing logic from `_send_market_order_ws` into `_sign_order()` that returns payload + order_index without sending
- [x] Keep retry logic (fallback size, 3 SELL attempts) around the new helper

### Phase 4: Fill Wait Loop

**File:** `test_lighter_connectivity.py`

After receiving the ack, wait for the fill notification on the fill listener WS.

- [x] Create `_wait_for_fill()` helper:
  ```python
  FILL_TIMEOUT = 5  # seconds

  async def _wait_for_fill(fill_ws, order_index, is_ask, timeout=FILL_TIMEOUT):
      deadline = time.perf_counter() + timeout
      while True:
          remaining = deadline - time.perf_counter()
          if remaining <= 0:
              return None  # timeout
          raw = await asyncio.wait_for(fill_ws.recv(), timeout=remaining)
          msg = json.loads(raw)
          if msg.get("type") == "ping":
              await fill_ws.send(json.dumps({"type": "pong"}))
              continue
          if _is_fill_for_order(msg, order_index, is_ask):
              return msg
  ```
- [x] Implement `_is_fill_for_order()` correlation logic:
  - Match by `ACCOUNT_INDEX` (bid_account_id for BUY, ask_account_id for SELL)
  - Match by `client_order_index` if available in the message (bid_client_id / ask_client_id)
  - Match by market_index
  - If correlation fields are absent in WS message, accept any fill on the correct market (document this limitation)

- [x] After receiving fill, capture `t4 = time.perf_counter()`
- [x] If fill times out, report `ack_to_fill` as "TIMEOUT" and store only the ack-based metrics

### Phase 5: Wire It All Together

**File:** `test_lighter_connectivity.py`

Update the main flow in `_test_taker_latency()`:

```
1. Setup fill listener WS (Phase 2)
2. Setup order sender WS (existing, drain "connected")
3. BUY:
   a. _send_and_measure() → timing + order_index
   b. Check ack for errors
   c. _wait_for_fill() → t4 or timeout
   d. Store all timing in Results
4. SELL:
   a-d. Same pattern
5. Close both WebSockets in finally block
6. Cleanup (existing)
```

- [x] Add `try/finally` around both WebSocket connections to ensure cleanup
- [x] Update SIGINT handler to close fill listener WS if open

### Phase 6: Edge Case Handling

- [x] **Zero-fill IOC:** If ack succeeds but no fill arrives within `FILL_TIMEOUT`, print warning: "Order ack'd but no fill — IOC likely expired unfilled" and report ack-to-fill as N/A
- [x] **Fill before ack processed:** Not a problem — fill is buffered in the websockets library. The `t4` measurement will be slightly inflated (fill may have arrived during ack processing). Document this limitation
- [x] **Multiple fills per order:** Capture `t4` on the **first** fill notification (earliest knowledge of execution). This is the operationally relevant metric for a taker
- [x] **Retry with fallback size:** If BUY fails with `TEST_SIZE_WEI` and retries with `FALLBACK_SIZE_WEI`, any stale fill messages from the failed attempt should be ignored (correlation by `client_order_index` handles this since each attempt generates a new index)

## Acceptance Criteria

- [x] Fill listener subscribes to correct channel and receives fill notifications
- [x] Signal-to-fill latency is measured as t0 (decision) → t4 (fill received)
- [x] Latency breakdown reported: signing, send-to-ack, ack-to-fill, total
- [x] Fill listener setup time reported separately (not included in per-order S2F)
- [x] Ping/pong handled on fill listener WS
- [x] Fill timeout (5s) prevents hanging on zero-fill IOC orders
- [x] Both WebSocket connections cleaned up on error and Ctrl+C
- [x] Existing test functionality (Binance baseline, geo-block detection) unchanged

## Technical Considerations

- **Timing precision:** `time.perf_counter()` is monotonic and high-resolution (~ns). Ideal for elapsed time measurement
- **Single-threaded async:** Both WebSockets are in the same asyncio event loop. Sequential `await` means fill measurement may be slightly inflated when the fill arrives during ack processing. This is an inherent limitation of the architecture and is documented
- **Channel name uncertainty:** Phase 0 resolves this empirically before any code is written. If `account_all_trades` doesn't exist, fallback to `account_all` and filter for trade messages
- **No auth assumed:** The existing `account_all` channel works without auth headers. If `account_all_trades` requires auth, add `SignerClient.create_auth_token_with_expiry()` as a connection header

## Dependencies & Risks

| Risk | Mitigation |
|------|------------|
| `account_all_trades` channel doesn't exist or has different name | Phase 0 empirical discovery; fallback to `account_all` |
| Channel requires authentication | Add auth token header using existing `create_auth_token_with_expiry()` |
| Fill message lacks correlation fields | Accept any fill on the correct market + account (document limitation) |
| Zero-fill IOC hangs the script | 5-second timeout with graceful degradation |
| Fill arrives before ack is processed | Buffered by websockets library; t4 slightly inflated (documented) |

## Constants

```python
FILL_TIMEOUT = 5          # seconds to wait for fill notification after ack
FILL_CHANNEL = f"account_all_trades/{ACCOUNT_INDEX}"  # to be confirmed in Phase 0
```

## References

- Brainstorm: `docs/brainstorms/2026-02-18-signal-to-fill-latency-brainstorm.md`
- Lighter WS API: https://apidocs.lighter.xyz/docs/websocket-reference
- Current taker test: `test_lighter_connectivity.py:397-562`
- WsClient helper: `lighter/ws_client.py`
- SignerClient: `lighter/signer_client.py`
- Previous fill detection removal: commit `583a6be`
