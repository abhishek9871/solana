# V255 RPCFast Atomic 10W Freeze

Date: 2026-05-28

## Result

V255 proved the live atomic PumpSwap route with RPCFast direct RPC delivery.

- Start wallet: 0.102132876 SOL
- Final wallet: 0.102161304 SOL
- Net wallet delta: +0.000028428 SOL
- Confirmed positive atomic closes: 10
- Negative wallet-delta closes: 0
- Token residual: 0
- Bot processes after run: 0

Each winning send was a single atomic transaction:

```text
buy -> sell -> close token account
```

No directional/unhedged buy was used.

## Winning Path

The winning live path is:

```text
fresh multipool scan
-> build explicit PumpSwap atomic tx
-> exact simulate final payer wallet delta
-> send through RPCFast direct RPC only if exact wallet delta is positive
-> confirm signature
-> verify wallet delta and token residual
```

The key fix was avoiding delivery methods whose fixed tips consumed the edge:

- Helius Sender SWQOS was too expensive for these small edges because of the 5000 lamport tip.
- Jito bundle-only accepted transactions but did not land reliably at affordable tips.
- RPCFast direct RPC landed the no-tip exact-positive atomic transactions.

## Hard Rules

- Do not send unless exact simulation shows positive final payer wallet delta.
- Do not use an unhedged live buy.
- Do not keep a token position after the transaction.
- Verify wallet/token state after each send.
- If a route turns stale or non-executable, rescan instead of grinding stale candidates.

## Freeze Files

Core:

- `v255_jito_inline_atomic.py`
- `v223_gpa_multipool_eval.py`
- `pgg2_v224_pumpswap_multipool_builder.py`
- `pgg2_v225_pumpswap_multipool_bundle_builder.py`
- `v245_fast_single_tx_oracle.py`
- `v246_wallet_check.py`

Support/dependencies:

- `pgg2_v109_no_send_live_bundle_validation.py`
- `pgg2_v108_external_tx_decoder.py`
- `pgg2_v108_bundle_profit_model.py`
- `pgg2_v108_bundle_builder.py`
- `pgg2_v108_jito_bundle_sender.py`
- `pgg2_v219_atomic_route_dislocation_scanner.py`

Prior-stage references:

- `v252_sender_atomic_test.py`
- `v253_hot_watcher.ps1`
- `v253_run_once_wrapper.ps1`
- `v254_batch_sender_atomic.py`
