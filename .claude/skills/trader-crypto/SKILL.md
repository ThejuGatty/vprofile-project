---
name: trader-crypto
description: >-
  Crypto Trader, the crypto add-on to the day-trader pack. Spot crypto only
  (BTC, ETH, SOL and other coins on your list), long only, no leverage, paper
  by default, never without your yes. Gets free delayed prices (Yahoo
  Finance, then Coinbase's public API), scans your coins on daily bars
  (20-day closing high, reclaimed 20-day average, oversold, oversold bounce,
  RSI, ATR), sets a stop and a target with a time stop instead of a market
  close, replays the next 24 hours 10,000 times from the last 60 days of
  hourly bars, sizes the order to your max loss after fees and slippage,
  checks it against your limits (PASS or BLOCK), shows the ticket in plain
  words with the exact JSON for Alpaca's crypto order tool, records your
  yes, builds the protective stop after the fill, and watches the stop, the
  target and the time stop. Use when the user says "crypto", "bitcoin",
  "BTC", "ETH", "Solana", "trade crypto", "scan crypto", "buy bitcoin",
  "what's bitcoin doing", "crypto stop loss", or names a coin.
argument-hint: "[coin]"
---

# trader-crypto

Crypto is different from the stock skills in four ways, and this skill is
built around them:

- **It never closes.** There is no opening range, no NYSE clock and no "flat
  by close". Days are UTC days (for the daily limits). A plan has a time stop
  instead: after `max_hours` (24 by default) with neither the stop nor the
  target hit, `watch` says to decide.
- **No bracket orders.** Alpaca's crypto orders are market, limit and
  stop-limit only, good until canceled (gtc) or immediate or cancel (ioc).
  So the entry goes first; right after it fills, a separate stop-limit sell
  protects it, and the target is watched by `watch`.
- **Fees are big enough to matter.** Every sum here takes off `fee_pct` each
  way (0.25 % by default) and assumes the stop fills `slippage_pct` below the
  stop (0.5 %). A plan that looks like 2R before costs is often near 1R after.
  That is why the default target is 3R.
- **Fractional coins.** Quantities go to `qty_decimals` places (6), always
  rounded down.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/crypto.py" price BTC ETH --write          # -> trader/crypto/prices.json
python3 "${CLAUDE_SKILL_DIR}/crypto.py" scan                           # your coins list -> watchlist.json
python3 "${CLAUDE_SKILL_DIR}/crypto.py" plan BTC --stop 1.5atr --target 3R   # or --stop 3% / a price, --entry PRICE
python3 "${CLAUDE_SKILL_DIR}/crypto.py" sim --hours 24                 # 10,000 replays + history
python3 "${CLAUDE_SKILL_DIR}/crypto.py" size                           # -> orders.json
python3 "${CLAUDE_SKILL_DIR}/crypto.py" check                          # PASS / BLOCK
python3 "${CLAUDE_SKILL_DIR}/crypto.py" ticket show
python3 "${CLAUDE_SKILL_DIR}/crypto.py" ticket approve --index 1 --reply "<the user's words>"
python3 "${CLAUDE_SKILL_DIR}/crypto.py" ticket result --index 1 --broker-id ID --filled-qty 0.0049 --filled-price 101234.5
python3 "${CLAUDE_SKILL_DIR}/crypto.py" ticket protect                 # the stop-limit sell after the fill
python3 "${CLAUDE_SKILL_DIR}/crypto.py" ticket exit --symbol BTC [--limit PRICE]
python3 "${CLAUDE_SKILL_DIR}/crypto.py" ticket fill --symbol BTC --side sell --qty 0.0049 --price 104000
python3 "${CLAUDE_SKILL_DIR}/crypto.py" watch                          # exit 0 = something rang
python3 "${CLAUDE_SKILL_DIR}/crypto.py" ticket log
```

`--now "2026-11-01 15:00"` pins the clock (UTC). `--offline DIR` reads saved
chart files (`BTC-1h.json`, `BTC-1d.json`, Yahoo's chart format) instead of
the network. `--root` moves the folder (default `trader/crypto`).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/crypto/config.json` | your limits (copy `config.example.json` from this folder) |
| `trader/crypto/prices.json` | latest prices with their time; `check` only uses one under `max_price_age_min` |
| `trader/crypto/watchlist.json` | the scan |
| `trader/crypto/plan.json` | entry, stop, target, time stop |
| `trader/crypto/orders.json` | the order(s) waiting for a check and a yes |
| `trader/crypto/portfolio.json` | cash and coins held (starts at `account_usd` cash), updated by fills |
| `trader/crypto/state/risk.json` | verdicts by order fingerprint |
| `trader/crypto/state/broker.json` | every yes, no, placed, failed and fill |
| `trader/crypto/state/watch.json` | which alerts rang, so each rings once |

These files are separate from the stock skills' `trader/` files, so the
stock limits and the crypto limits never mix.

## Steps: one trade

1. **Price and scan.** `price BTC --write`, or `scan` for the whole coins
   list. Say which setups fired and that prices are free and delayed.
2. **Plan.** `plan BTC`. The default stop is 1.5 times the 14-day ATR, a
   stop sized to how much the coin really moves; the default target is 3R.
   It refuses a stop at or above the entry, a target at or below it, or a stop
   inside the slippage allowance.
3. **Sim.** `sim`. Report the three odds, the average R after fees, the
   history line and the verdict, word for word. It is a simulation of the
   last 60 days, not a forecast: say so.
4. **Size.** `size`. Says which of four limits decided the quantity (max loss,
   max order, position cap, cash after buffer). It refuses a plan whose reward
   to risk after fees is under `min_reward_risk`, and an order under
   `min_order_usd`. A refusal is the answer; never shrink the stop to make it fit.
5. **Price again, then check.** `price BTC --write`, then `check`. Only PASS
   goes on. On BLOCK, say each reason.
6. **Ticket and the yes.** `ticket show`. Show ONE ticket and ask "Place this
   order? Reply yes or no." Run `ticket approve` with the reply word for word.
   If it prints "Not recorded", ask again; never turn a reply into a yes.
7. **Place.** Only after "Recorded your yes": call the broker's crypto order
   tool (Alpaca: `place_crypto_order`) with exactly the printed tool input.
   If the tool's schema names a field differently (for example `order_type`
   instead of `type`), map the name and keep every value exactly as printed.
   Never change a quantity or a price. The tool's own Allow once / Deny card
   then asks the user again.
8. **Record.** `ticket result` with the broker's id and, once filled, the
   filled quantity and price. On a failure: `--status failed`.
9. **Protect.** Straight after the fill: `ticket protect`, `check`, `ticket
   show`, the user's yes, place it. Until the stop is in, say plainly that the
   position has no stop at the broker.
10. **Watch.** Run `watch` every few minutes from a scheduled task or `/loop`
    while the position is open. It rings once each for the stop, the target
    and the time stop. On the target or the time stop: cancel the protective
    stop (its own yes), then `ticket exit`, `check`, the user's yes. Never act
    for them.

## Broker setup

Alpaca's MCP server (see `/trader-broker`, Setup A) includes crypto when its
`trading` toolset is on. In the Alpaca paper account crypto trading needs no
extra step; on a live account it has to be enabled in the dashboard. Then:

- `place_crypto_order`: **Needs approval**, never Always allow. The stock
  skills tell you to block it; turn it from Blocked to Needs approval only if
  you use this add-on.
- `cancel_order_by_id`: Needs approval.
- In Claude Code: `"permissions": {"ask": ["mcp__alpaca__place_crypto_order"]}`.
- Check your coins are tradable on Alpaca (the read-only asset tools, or the
  dashboard) and keep only those in `coins`.

Keys never go in the chat. Paper first: `"paper": true` is the default, and
live trading needs `"paper": false` and `"allow_live": true` both set on purpose.

## Data

- Yahoo Finance chart JSON, `BTC-USD`: unofficial, free, delayed, can change
  or rate-limit. One coin at a time, half a second apart.
- Fallback: Coinbase Exchange public candles and ticker
  (`api.exchange.coinbase.com`), no key.
- Prices differ a little between exchanges. The broker's price is the one that
  fills. Never present a quote as live.

## Rules

- Long spot only. No margin, no leverage, no shorts, no futures, no perps, no
  staking, no options. A sell is only ever for coins already held.
- Getting out is never blocked by a day limit.
- One order per yes. Never "yes to all".
- Crypto can move 10 % in an hour and gap through a stop-limit's limit price,
  leaving it unfilled. Say so when the stop goes in, and keep `watch` running.
- Not financial advice. Most people who trade crypto short-term lose money.
