# Signal-to-Fill Latency Measurement

**Date:** 2026-02-18
**Status:** Ready for planning

## What We're Building

Modify `test_lighter_connectivity.py` to measure true **signal-to-fill latency** — the time from when the script decides to trade (signal) to when the matching engine confirms the trade was executed (fill).

The current script only measures WebSocket round-trip (send order -> receive server ack), which is not fill confirmation.

## Why This Approach

- **Dual-WebSocket architecture**: One WS subscribes to `account_all_trades/{ACCOUNT_ID}` for fill notifications; a second WS sends the signed order. This avoids race conditions and ensures the fill listener is ready before the order is placed.
- **Full latency breakdown**: Report sub-components (signing, send-to-ack, ack-to-fill) so the user can identify bottlenecks, not just total latency.
- **`account_all_trades` channel**: Only surfaces the user's own fills — no filtering needed compared to the public `trade/{MARKET_INDEX}` channel.

## Key Decisions

1. **Fill detection via `account_all_trades/{ACCOUNT_ID}` WebSocket channel** — receives fill messages only for the authenticated account
2. **Dual-WebSocket pattern** — separate connections for fill listening vs. order sending
3. **Full latency breakdown** — report: signing time + send-to-ack + ack-to-fill = total signal-to-fill
4. **Use `time.perf_counter()`** for all local timestamps (monotonic, high-resolution)

## Latency Breakdown

```
t0 = perf_counter()          # Signal: decision to trade
sign_order()
t1 = perf_counter()          # Signing complete
ws_order.send(order)
t2 = perf_counter()          # Order sent
ack = ws_order.recv()        # Server acknowledgment
t3 = perf_counter()          # Ack received
fill = ws_fills.recv()       # Fill notification from matching engine
t4 = perf_counter()          # Fill received

# Breakdown:
# Signing:      t1 - t0
# Send-to-ack:  t3 - t1
# Ack-to-fill:  t4 - t3
# Total S2F:    t4 - t0
```

## Open Questions

- Does `account_all_trades` require authentication (API key) or just the account ID in the channel name?
- What does the fill message look like exactly — does it contain a server-side timestamp we could compare against?
- Should we also capture the matching engine's own timestamp from the fill message for cross-validation?
- Timeout handling: how long to wait for a fill before declaring failure?
