# Exchange E2E

This runbook verifies the real exchange flow with Docker-backed services and no mocked exchange components.

## Command

```bash
tools/e2e/run_exchange_e2e.sh --package --build --reset --keep-running
```

Use `--package` after code changes so Docker images copy fresh service jars, and use `--build` when you want the app images rebuilt. Use `--reset` when you want a clean Kafka/Postgres/Redis/Vault state. Omit `--keep-running` if you want the script to stop the main app containers after the flow completes.

## What It Verifies

- Wallet preferences are loaded from `/preferences.yml` and seed ETH/USDT currencies.
- Seller and buyer receive real wallet deposits into `MAIN` wallets.
- Matching Gateway accepts authenticated E2E orders via `X-Opex-User`.
- Accountant approves order creation and reserves funds from `MAIN` to `EXCHANGE`.
- Matching Engine matches an `ETH_USDT` ask/bid pair through Kafka.
- Market consumes the resulting trade and exposes it through seller/buyer user trade history for the current run.
- Wallet settlement is completed and final balances reflect trade fees.
- An unmatched GTC ask appears in the user's open orders and public order book.
- Canceling that unmatched order removes it from open orders and releases the reserved ETH back to the user's MAIN wallet.
- A partial fill leaves the remaining ask quantity in the order book, and canceling the remainder releases the still-reserved ETH.
- An IOC order with no available liquidity is canceled immediately, never remains open, and releases its reserved ETH.
- An IOC market ask consumes available bid liquidity, leaves the maker's remaining bid in the book, and releases that remaining USDT after cancel.
- An IOC market ask can sweep multiple bid price levels, fill the best bid first, partially fill the next bid, and release the remaining maker reservation after cancel.
- An IOC market bid can sweep multiple ask price levels, fill the best ask first, partially fill the next ask, and release the remaining maker reservation after cancel.
- Price priority is respected: when `110` and `140` bids are open, an ask crossing both fills at `140` first while the lower bid stays open.
- Time priority is respected at the same price: when two `125` bids are open, an ask fills the older bid first while the newer bid stays open.
- A second order cannot over-reserve base or quote funds already locked by an open order.
- Cancel authorization is enforced asynchronously: an intruder cancel submit may be accepted by the gateway, but the engine rejects it and the owner's open order remains reserved.
- Duplicate cancel is safe: resubmitting a cancel for an already canceled order does not release funds twice.
- An unsupported FOK order does not remain in user-visible market order state and releases its reserved ETH.
- Underfunded ask and bid orders are rejected and never appear in market order state.
- Invalid order parameters such as zero quantity, invalid price, malformed pair, or price/quantity precision mismatch are rejected before reaching market state.
- Duplicate deposit `transferRef` values are rejected and do not double-credit the receiver wallet.
- Withdraw requests cover invalid request rejection, owner-only cancel authorization, `CREATED -> CANCELED`, `CREATED -> PROCESSING -> DONE`, `CREATED -> PROCESSING -> REJECTED`, and rejection of terminal-state reprocessing without balance mutation.
- The public `ETH_USDT` ask and bid order books are empty after all open-order scenarios are cleaned up.
- The public `ETH_USDT` recent-trades feed contains exactly the expected trade count and price/quantity distribution.
- Wallet, Accountant, and Market Postgres tables contain the expected persisted settlement invariants after all API checks pass, including internally consistent Market trade projections.
- Restarting Market after settlement preserves the public empty order book and recent-trades distribution.
- Restarting Matching Engine while an ask is open preserves matchability: the restarted engine accepts a crossing bid and settles the trade.
- Restarting Wallet after funds are reserved does not break the subsequent trade settlement.
- Restarting Accountant after funds are reserved does not break later trade settlement and financial action processing.
- Restarting Matching Gateway does not break new order submission, matching, market visibility, or wallet settlement.
- Restarting all core exchange services together still allows fresh deposits, order submission, matching, market visibility, and wallet settlement.
- Restarting the Kafka broker still allows producers/consumers to reconnect and settle a fresh trade.
- Restarting Wallet, Accountant, and Market Postgres datastores still allows services to reconnect and settle a fresh trade.
- `BTC_USDT` is exercised independently from `ETH_USDT`, including persisted trade visibility and empty final books.
- Concurrent `BTC_USDT` takers against one resting ask fully settle all takers and leave no reserved `EXCHANGE` balance.
- Concurrent overfill is bounded: only available maker quantity fills, exactly one residual taker bid remains open, its `EXCHANGE` reservation matches the open quantity, and cancel releases it.
- BTC-specific Wallet, Accountant, and Market tables contain the expected ledger counts, final wallet-type balances, processed actions, and total matched quantity.

For the default `1 ETH @ 100 USDT` flow, the final balances are:

- Seller: `1 ETH`, `99 USDT`.
- Buyer: `0.99 ETH`, `900 USDT`.
- Matching Engine restart seller: `0.5 ETH`, `54.945 USDT` after a resting `0.5 ETH @ 111 USDT` ask survives engine restart and is filled.
- Matching Engine restart buyer: `0.495 ETH`, `44.5 USDT`.
- Wallet restart seller: `0.6 ETH`, `44.352 USDT` after a resting `0.4 ETH @ 112 USDT` ask remains reserved across Wallet restart and settles after fill.
- Wallet restart buyer: `0.396 ETH`, `55.2 USDT`.
- Accountant restart seller: `0.7 ETH`, `33.561 USDT` after a resting `0.3 ETH @ 113 USDT` ask remains open across Accountant restart and settles after fill.
- Accountant restart buyer: `0.297 ETH`, `66.1 USDT`.
- Matching Gateway restart seller: `0.8 ETH`, `22.572 USDT` after submitting and filling `0.2 ETH @ 114 USDT` after Gateway restart.
- Matching Gateway restart buyer: `0.198 ETH`, `77.2 USDT`.
- Core services restart seller: `0.8 ETH`, `22.77 USDT` after submitting and filling `0.2 ETH @ 115 USDT` after restarting Gateway, Matching Engine, Accountant, Wallet, and Market together.
- Core services restart buyer: `0.198 ETH`, `77 USDT`.
- Kafka broker restart seller: `0.8 ETH`, `22.968 USDT` after submitting and filling `0.2 ETH @ 116 USDT` after broker restart.
- Kafka broker restart buyer: `0.198 ETH`, `76.8 USDT`.
- Postgres datastore restart seller: `0.8 ETH`, `23.166 USDT` after submitting and filling `0.2 ETH @ 117 USDT` after Wallet, Accountant, and Market Postgres restart.
- Postgres datastore restart buyer: `0.198 ETH`, `76.6 USDT`.
- Cancel scenario owner: `1 ETH` after reserve and cancel release.
- Partial seller: `1.6 ETH`, `47.52 USDT` after `0.4 ETH @ 120 USDT` fills and the remaining `0.6 ETH` is canceled.
- Partial buyer: `0.396 ETH`, `2 USDT`.
- IOC no-liquidity owner: `1 ETH` after immediate cancel release.
- Market seller: `0.8 ETH`, `25.74 USDT` after selling `0.2 ETH` into a `130 USDT` bid.
- Market buyer: `0.198 ETH`, `14 USDT` after the remaining `0.1 ETH` bid is canceled.
- Multi-level sweep seller: `0.7 ETH`, `42.57 USDT` after selling `0.3 ETH` across `150` and `140` bid levels.
- Multi-level sweep high buyer: `0.099 ETH`, `5 USDT`; low buyer ends with `0.198 ETH`, `22 USDT` after canceling the remaining `0.1 ETH @ 140` bid.
- Multi-level bid sweep buyer: `0.297 ETH`, `51 USDT` after buying `0.3 ETH` across `90` and `100` ask levels.
- Multi-level bid sweep sellers: low seller ends with `0.9 ETH`, `8.91 USDT`; high seller ends with `0.8 ETH`, `19.8 USDT` after canceling the remaining `0.1 ETH @ 100` ask.
- Price-priority seller: `0.8 ETH`, `27.72 USDT` after selling into the `140 USDT` bid.
- Price-priority high buyer: `0.198 ETH`, `2 USDT`; low buyer returns to `30 USDT` after cancel.
- Time-priority seller: `0.8 ETH`, `24.75 USDT` after selling into the older `125 USDT` bid.
- Time-priority first buyer: `0.198 ETH`, `5 USDT`; second buyer returns to `30 USDT` after cancel.
- Over-reserve owner: `1 ETH` after a `0.7 ETH` ask is reserved, a second `0.5 ETH` ask is rejected, and the first ask is canceled.
- Bid over-reserve owner: `100 USDT` after a `0.8 ETH @ 80 USDT` bid is reserved, a second `0.5 ETH @ 80 USDT` bid is rejected, and the first bid is canceled.
- Cancel-auth owner: `1 ETH` after an intruder cancel is rejected, the owner cancels the order, and the same owner cancel is submitted again.
- Unsupported FOK owner: `1 ETH` after reject release.
- Underfunded reject owners never get user-visible market orders.
- Invalid order owner keeps `1 ETH` and `100 USDT` after malformed, non-positive, and precision-mismatched orders, with no user-visible market orders.
- Duplicate deposit owner keeps `5 USDT` after the first deposit succeeds and the second deposit with the same `transferRef` is rejected.
- Withdraw owner keeps `6 USDT` after one canceled, one accepted, and one rejected withdrawal; intruder cancel, processing cancel, duplicate accept, and terminal-state admin/user transitions are rejected without additional wallet movement.
- Final public order book state has `0` ask levels and `0` bid levels.
- Final public recent-trades state has `16` trades with aggregate quantities: `90 -> 0.1`, `100 -> 1.2`, `111 -> 0.5`, `112 -> 0.4`, `113 -> 0.3`, `114 -> 0.2`, `115 -> 0.2`, `116 -> 0.2`, `117 -> 0.2`, `120 -> 0.4`, `125 -> 0.2`, `130 -> 0.2`, `140 -> 0.4`, `150 -> 0.1`.
- Final database state has zero E2E `EXCHANGE` wallet balances, wallet transaction counts of `41 DEPOSIT`, `38 ORDER_CREATE`, `12 ORDER_CANCEL`, `32 TRADE`, `32 FEE`, and `1 ORDER_FINALIZED`, processed Accountant financial action counts of `38 SubmitOrderEvent`, `12 RejectOrderEvent`, and `65 TradeEvent`, no pending/given-up Accountant retries, zero Market `open_orders`, internally consistent Market trade rows, and the same persisted trade distribution as the public recent-trades API.
- After a Market restart, public ask/bid books remain empty and the recent-trades API still exposes the expected `16` persisted trades.
- BTC-specific final database state has wallet transaction counts of `10 DEPOSIT`, `10 ORDER_CREATE`, `1 ORDER_CANCEL`, `12 TRADE`, and `12 FEE`; processed Accountant action counts of `10 SubmitOrderEvent`, `24 TradeEvent`, and `1 RejectOrderEvent`; zero residual BTC/USDT `EXCHANGE` balances; and `6` persisted BTC trades totaling `0.006 BTC`.

## E2E Kafka Settings

`tools/e2e/docker-compose.e2e.yml` intentionally makes Kafka less fragile for local/CI runs:

- Core services bootstrap against `kafka-1:29092`.
- Matching Engine creates E2E topics with `partitions=1`, `replicas=1`, and `min.insync.replicas=1`.
- Accountant and Market create `richOrder`/`richTrade` E2E topics with the same single-broker-safe settings.
- Consumers use `auto.offset.reset=earliest` so startup timing does not drop orders published just before assignment.
- The script waits for accountant, matching-engine, and market Kafka assignments before submitting orders.

These overrides are E2E-specific and do not change the default production compose topology.
