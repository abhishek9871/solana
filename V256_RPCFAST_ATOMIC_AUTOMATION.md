# V256 RPCFast Atomic Automation

Date: 2026-05-28

## Purpose

V256 wraps the proven V255 route into a repeatable set-and-forget loop.

The loop does not change the winning trade path:

```text
fresh scan -> exact-positive atomic tx simulation -> RPCFast direct send -> wallet/token verification
```

## Script

Run from the local Windows workspace:

```powershell
powershell -ExecutionPolicy Bypass -File .\v256_rpcfast_atomic_loop.ps1 -TargetWins 10 -MaxCycles 30 -MaxMinutes 60
```

Useful smoke run:

```powershell
powershell -ExecutionPolicy Bypass -File .\v256_rpcfast_atomic_loop.ps1 -TargetWins 1 -MaxCycles 2 -MaxMinutes 8
```

## Safety Rules

- Remote clean-state check before every cycle.
- Fresh scan before each send attempt.
- Upload only the latest candidate file.
- V255 sends only if exact simulation shows positive final payer wallet delta.
- Stop on negative wallet delta.
- Stop on token residual.
- Stop when target wins is reached.

## Smoke Validation

The first V256 smoke run passed:

- Target wins: 1
- Wins: 1
- Net wallet delta: +1084 lamports
- Token accounts: 0
- Nonzero tokens: 0

V256 also patches `v246_wallet_check.py` to avoid public RPC 429 failures by using SolanaVibeStation by default and retrying transient 429s.

## Profit Selector Upgrade

The automation now asks V255 to keep scanning past the first tiny positive route until either:

- an exact-positive route reaches the configured profit target, or
- the internal search window expires, in which case V255 uses the best exact-positive route found so far.

Default:

```powershell
-ProfitTargetLamports 3000 -SearchSeconds 70 -SendLimit 80
```

Validation:

- Target wins: 1
- Wins: 1
- Net wallet delta: +3409 lamports
- Token accounts: 0
- Nonzero tokens: 0

This keeps the original safety model intact while avoiding unnecessary penny-sized first-positive sends when a better exact-positive route is available in the same fresh scan.
