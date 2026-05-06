# Forced Edge Scanner

Read-only scanner for opportunities where the edge can be calculated before any execution.

It does **not** load private keys and does **not** send orders or transactions.

## Modules

- `compound` — Base Compound III USDC discounted collateral sale scanner. Checks `buyCollateral` quote against Uniswap V3 exit quote, then subtracts Base gas and a safety buffer.
- `triangular` — Binance spot 3-hop scanner. Uses top-of-book only as a rough prefilter, then re-simulates through real order-book depth.
- `carry` — Binance spot/perp funding carry scanner. Estimates one funding event after spot/perp spread and taker fees. This module is informational because the trade is not atomic.
- `polymarket` — binary complete-set scanner. Checks whether both outcomes can be bought below the $1 payout after estimated taker fee. This is not atomic; live mode would need fill-and-kill handling and a jurisdiction check.

## Run

```powershell
py -3 forced_edge_scanner.py --once --modules compound,triangular,carry,polymarket --capital-usd 28 --min-net-usd 0.03
```

Continuous read-only scan:

```powershell
py -3 forced_edge_scanner.py --modules compound,triangular,carry,polymarket --poll-seconds 30
```

Optional Compound account liquidation sweep:

```powershell
py -3 forced_edge_scanner.py --once --modules compound --compound-scan-accounts --compound-max-accounts 30
```

Public RPC account sweeps are slow. Keep account limits tight unless using a reliable indexed RPC.

## Output

Console lines show the current decision:

```text
EDGE    compound   base:cUSDCv3   discount_sale  WETH/USDC  gross=$+0.1200 cost=$0.0570 net=$+0.0630
NO_EDGE triangular binance_spot   3hop_depth     USDT       gross=$-0.1947 cost=$0.0000 net=$-0.1947
```

Each cycle is also appended to:

```text
logs/forced_edge_events.jsonl
```

## Execution Rule

Execution should not be added until the scanner repeatedly finds positive net opportunities:

```text
net = exit_value - entry_cost - gas - fees - safety_buffer
execute only if net >= minimum_required_profit
```
