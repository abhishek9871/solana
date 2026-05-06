# Prediction Hunter Bot

Small-bankroll prediction-market bot focused on paid maker incentives.

Default mode is dry-run. Live mode only posts post-only maker orders and refuses to run without credentials.

## What It Does

- `reward` module:
  - pulls Polymarket reward markets
  - filters for markets the bankroll can qualify for
  - ranks by reward/day, reserve required, complete-set edge, and competition
  - maintains two maker BUY quotes, one on each binary outcome

- `crypto_maker` module:
  - finds BTC/ETH/SOL binary markets
  - plans maker quotes only
  - live execution is disabled by default

- `event_watch` module:
  - builds a watchlist for markets that may become event-lag trades
  - does not place orders

## Dry Run

```powershell
py -3 prediction_hunter_bot.py --once --dry-run --reward-markets 1 --max-capital-usd 20
```

Continuous dry-run:

```powershell
py -3 prediction_hunter_bot.py --dry-run --poll-seconds 20
```

## Live Guard

Live mode requires:

```env
POLYMARKET_DRY_RUN=0
POLYMARKET_PRIVATE_KEY=...
POLYMARKET_FUNDER_ADDRESS=...
POLYMARKET_SIGNATURE_TYPE=0
POLYMARKET_MAX_CAPITAL_USD=20
```

Run live only after dry-run plans look correct:

```powershell
py -3 prediction_hunter_bot.py --no-dry-run --reward-markets 1 --max-capital-usd 20
```

## Output

State snapshot:

```text
data/prediction_hunter_state.json
```

Cycle log:

```text
logs/prediction_hunter_events.jsonl
```

