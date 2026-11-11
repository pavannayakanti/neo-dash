# Market Flow: NSE breadth + OI build-up dashboard with a guarded order desk (Kotak Neo)

A live dashboard that shows Nifty-50 stocks flowing from a breadth bar into gainer and loser tiles.
Each stock is tagged with its F&O build-up type, a conviction score and its pivot support/resistance.

```
Kotak Neo SFeed websocket ──► feed.py ──► analytics.py (MarketState) ──► app.py ──ws──► static/index.html (D3)
   (nse_cm + nse_fo ticks)    normalise    %chg, OI build-up, pivots,     push every 1s     flows, tiles, cards
                                           score, breadth, sectors
```

## 1. Setup

Run these commands from the project directory. Python 3 and `venv` must be installed.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

On Windows PowerShell, activate the environment with `.venv\\Scripts\\Activate.ps1` instead.

## 2. Run with demo data (no credentials)

```bash
FEED_MODE=mock ./.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000 in your browser. Keep the server running in the terminal; press `Ctrl+C` to stop it.

## 3. Go live on Kotak Neo

**One-time setup in the Neo app**
1. Go to More → Trade API → **Create New Application** and copy the token. That token is your `NEO_CONSUMER_KEY`.
2. On the same page, open **TOTP Registration** and scan the QR code. Keep the secret key; that is `NEO_TOTP_SECRET`.
3. Copy `.env.example` to `.env` and fill in the values. The UCC is shown in the Neo app's Profile section.

**Instrument tokens** (redo after every monthly expiry)
```bash
pip install kotakneoapi
# Save the nse_cm and nse_fo scrip-master CSVs. Use the SDK's scrip-master function,
# documented at github.com/Kotak-Neo/kotak-neo-python under docs/functions.
python tools/fill_tokens.py --cm nse_cm.csv --fo nse_fo.csv
```

**Previous-day data** (every morning before 09:15)
Download yesterday's CM and F&O **UDiFF** bhavcopies from nseindia.com → All Reports, then run:
```bash
python tools/prep_prevday.py --cm BhavCopy_NSE_CM_...csv --fo BhavCopy_NSE_FO_...csv
```
Without this file the dashboard still runs, but with these gaps:
- No pivots.
- No relative volume.
- OI change is measured from the first OI seen today instead of yesterday's close.

**Run**
```bash
FEED_MODE=kotak NEO_DEBUG_FIELDS=1 uvicorn app:app --port 8000
```

### ⚠ Verify on your first live run
Kotak's README confirms only three message fields: `last_traded_price`, `instrument_token` and `trading_symbol`.
- **OI, volume and previous close field names are unconfirmed.** `feed.py` tries several common names for each. With `NEO_DEBUG_FIELDS=1`, the server prints the real field names from the first message. Put them at the top of the lists in `feed.py` (`OI_FIELDS`, `VOLUME_FIELDS`, `PREV_CLOSE_FIELDS`).
- **Scrip-master column names may differ.** If `fill_tokens.py` can't find a column, edit `COLS` in that file.
- **The stock list may be out of date.** Check `data/universe.csv` against the current Nifty 50, for example post-demerger tickers like TATAMOTORS. You can add any F&O stock with its sector.

## Swing mode (header toggle: INTRADAY | SWING)

Swing mode is built for **10–15 minutes a day after the close**. Positions are held for days up to about 3 weeks, as Delivery (CNC), and are planned on **daily candles**. The intraday features are unchanged; the toggle only switches the right-hand panel and the tile highlights.

**Daily history (one-time setup, then nightly):**
```bash
python tools/fetch_bhavcopy.py --days 260 --out bhav/   # or download the UDiFF files manually
python tools/build_daily.py bhav/                       # -> data/daily/<SYMBOL>.csv
```
- **Nightly:** after about 18:00, add that day's bhavcopy and re-run `build_daily.py`.
- **Splits and bonuses** aren't adjusted for; rebuild the affected stock if one happens.
- **Demo mode** generates synthetic history so you can try the screen.

**Setups** (rule-based, not advice; long only):

| Setup | Rule | Stop |
|---|---|---|
| **BREAKOUT** | Close above the prior 20-day high and the 50-day average, on ≥1.5× average volume | 2×ATR below entry |
| **PULLBACK** | Uptrend (20-day average above the 50-day and rising), dips to the 20-day average, closes up, RSI 40–60 | Under the 5-day low |

**Targets:** 2R for both setups.

**Time stop:** exit after 15 trading days if the target hasn't been hit.

**Sizing:** quantity = `SWING_RISK_PER_TRADE` ÷ (entry − stop), capped by `SWING_MAX_ORDER_VALUE`.

**Confidence score:** goes up in a "risk-on" market (most stocks above their 50-day average) and when the stock is above its 200-day average.

**Holdings actions** (shown each evening):
- **STOP HIT / TARGET HIT:** exit or book the profit.
- **RAISE STOP:** at +1R, move the stop to your entry.
- **TRAIL STOP:** at +2R, move the stop under the 20-day average. One tap on **APPLY**; stops only ever move up.
- **TIME STOP / TREND BROKEN:** the target didn't come in time, or the price closed below the 50-day average.

**Orders and stops:**
- **PLAN TRADE:** queues a limit buy. After the close, paper orders queue for the **next session**. Live orders can't be queued unless Kotak's API supports AMO (it's in the support questions), so place them at 09:15.
- **Paper mode:** a **simulated GTT (OCO)** exits automatically at the stop or the target. You don't need to watch.
- **Live mode:** after the buy fills, place a **GTT OCO** with the same levels in the Neo app. It's a one-minute task, and it keeps the stop enforced while you're at work.

**Monthly loss limit:** month-to-date net P&L counts realised trades plus any losses on open positions, across intraday and swing. At −`MONTHLY_LOSS_LIMIT` (₹8,000 by default), **all new buys are blocked** until next month. Exits are always allowed.

## Signals: when to buy, when to sell

The **SIGNALS** panel (right side) contains **rule-based setups**. They are not predictions or advice. Each one shows which rule fired and gives an exact entry, stop-loss and target.

**Header phase chip:**

| Time | Phase | What it means |
|---|---|---|
| 09:15–09:30 | OPENING | Wait. The opening range is forming and prices are noisy |
| 09:30–11:30 | PRIME | Best window for breakouts |
| 11:30–13:30 | MIDDAY | Lower volume, so confidence drops |
| 13:30–14:45 | AFTERNOON | Continuation setups only |
| 14:45 → square-off | LATE | No new intraday entries; manage your exits |

**Buy setups** (long only):
- **ORB (Opening Range Breakout):** price breaks above the first-15-minute high, above VWAP, on at least 1.2× normal volume.
  - Stop: VWAP or the middle of the opening range.
  - Target: 2R (twice the risk).
- **VWAP Pullback:** a stock up more than 0.5% dips back to VWAP and turns up.
  - Stop: just under VWAP.
  - Target: 2R.

**Every signal card shows:**
- **Buy price:** the maximum limit price to pay.
- **Stop and target levels.**
- **Quantity:** sized so that hitting the stop loses about `RISK_PER_TRADE` (₹50 by default), then capped by your order limits.
- **A confidence score,** built from transparent points:
  - +15 for strong volume.
  - ±10–15 for market breadth.
  - ±10 for the stock's sector.
  - −10 in the midday window.
- **TRADE button:** pre-fills the order panel with the plan.

**EXTENDED · WAIT:** the price already ran past the entry. Don't chase it; the card shows the pullback level to wait for.

**AVOID BUYING:** the stock broke below its opening-range low and VWAP.

**Exit alerts on your positions:**
- **STOP HIT:** exit to cap the loss.
- **TARGET HIT:** book the profit or trail your stop.
- **TRAIL STOP:** you're up 1R, so move the stop to cost.
- **MOMENTUM LOST:** the price crossed below VWAP.
- **TIME EXIT:** close intraday positions before the square-off.

**Stop-losses are not placed with Kotak.** The app alerts you and you exit manually. Check the order panel's breakeven too: on tight intraday stops, charges can be a large share of the planned risk.

### Scorecard: does the rule actually work?

Every BUY signal is logged when it fires. The app then records whether the price hit the target or the stop first, or where it stood at the time exit.
- **Today's results** appear in the panel.
- **History** is kept in `data/signal_journal.jsonl`.

The scorecard assumes you got filled at the signal price and **ignores charges**. Run paper mode for several weeks and judge each setup by its total R before trusting it with money.

### Demo day

With `FEED_MODE=mock`, a full simulated trading day (09:15 → 15:30) is replayed at 20× speed, so you can watch every phase, signal and square-off in about 20 minutes. Mock prices are random walks, so the scorecard there shows how the app behaves, not whether the rules are any good.

## Stock-first mode (default)

With `ALLOW_FO=0` (the default) the UI hides all futures and OI elements and shows cash-market signals instead.

**Tile tags:**
- **HIGH / LOW**: trading near the day's high or low.
- **▲VW / ▼VW**: above or below VWAP.

**Cards and order panel:**
- A day-range bar with a VWAP marker.
- Volume compared with a usual day.
- The score uses distance from VWAP in place of OI.

**Order panel:** two plain choices, **INTRADAY** (buy and sell today) or **DELIVERY** (keep in demat).

You don't need futures tokens or the F&O bhavcopy in this mode. They only power OI.

### Buying and selling the same day

| You buy as… | Can you sell today? | How it's charged |
|---|---|---|
| **Intraday (MIS)** | Yes, and you must. The broker auto-exits at its cut-off (`SQUAREOFF_TIME`) and charges a fee | Intraday rates |
| **Delivery (CNC)** | Yes. A buy and sell on the same day net off, so no delivery happens | **Intraday rates** (the app handles this automatically) |
| Delivery, sold next day | Yes, even before the shares show in your demat (T+1). This is called **BTST**. Small risk: if the seller fails to deliver, an exchange auction or penalty can apply | Delivery rates |
| Delivery, held longer | Yes | Delivery rates + DP charge |

**Breakevens before you buy:** for a Delivery buy, the panel shows **two** breakeven prices, one if you sell today and one if you hold overnight. The overnight figure is much higher because of delivery STT (0.1% on each side) and 0.20% brokerage.

**How paper mode follows the broker's rules:**
- At `SQUAREOFF_TIME`, open intraday positions are auto-exited at LTP, with the fee included.
- New intraday orders are blocked after that time.
- Unfilled orders expire at the end of the day.
- Delivery holdings carry over to the next day and are tagged **HELD**.

## Order desk (paper by default)

Click any tile or card to open the order panel.

- **Two-step flow:** choose Stock or Futures, Buy or Sell, product, quantity and limit price, then press **Review**. The server validates the order and returns a ticket valid for 30 s. **Confirm** sends it. Before sending, the server checks everything again against the latest price, so the browser can't skip a check.

**Guard rails.** All are set in `.env` and enforced on the server:

| Rule | Default |
|---|---|
| Order type | **Limit only.** SEBI doesn't allow market orders for retail API trading |
| Stock order | ≤ 10 shares and ≤ ₹20,000 |
| Futures | **Off.** When enabled: 1 lot, ≤ ₹15 lakh notional |
| Price band | Limit price within ±1% of LTP (fat-finger guard) |
| Daily cap | 20 orders |
| Spacing | At least 2 s between orders. Kotak allows 10 orders/s; this is far below that |
| Kill switch | The header button halts all new orders instantly |
| Retries | Never automatic. A failed live order is reported, not resent (avoids duplicates) |

**Audit log.** Every order is recorded in `data/orders_<mode>.jsonl`.

**Paper mode** (`TRADING_MODE=paper`):
- A marketable limit order fills at LTP.
- Any other order rests until the price crosses it.
- Positions and P&L show in the panel and the header.

Run in paper mode for a few weeks first.

### Going live (`TRADING_MODE=live`)
1. **Static IP is mandatory.** Since 1 Apr 2026, Kotak accepts order calls only from a whitelisted static IP, and the session must be created from that same IP. Whitelist your IP in the Neo app (More → Trade API → Add IP). Run this app on that machine or VPS; check the IP with `curl https://api.ipify.org`.
2. **Set the safeguards.** Set `ORDER_API_TOKEN` to a long random string. Keep uvicorn on `127.0.0.1`, which is its default. To reach it remotely, use an SSH tunnel; never expose it publicly.
3. **Feed must be live.** Live trading requires `FEED_MODE=kotak`. The app refuses to start otherwise.
4. **First live test: 1 share at a limit far from the market.** Confirm it appears in the Neo app, then cancel it there. Live orders can't be cancelled from this panel yet, so use the Neo app for that.
5. **Futures quantity check.** The order sends `quantity = lots × lot_size`. Do one 1-lot test and confirm Kotak reads that as units, not lots, before relying on it.

**Not built yet:**
- Stop-loss orders.
- A daily max-loss lockout based on live P&L.
- Cancelling or modifying live orders from the panel.

## Net P&L: "if I sell now, what do I keep?"

When you open a stock you already hold, the panel switches to **SELL** with your quantity filled in. It then shows **PROFIT / LOSS ₹x net of all charges**, which updates live as the price or your limit changes. Tap *charges breakdown* to see each charge.

**Before you buy,** the panel shows the **breakeven exit price**: how far the stock must move just to cover the round-trip charges.

**Positions.** Each position shows gross P&L and **net if you exit now**. The header shows total net P&L.

**Live mode:** live positions aren't read from Kotak yet. Type your average buy price into *Your avg price* and the same net calculation runs.

### What's in the charges (`charges.py`)

| | Delivery (CNC) | Intraday (MIS) | Futures |
|---|---|---|---|
| Brokerage (Kotak, set in `.env`) | 0.20% | min(₹10, 0.05%) | ₹10/order |
| STT | 0.1% buy + sell | 0.025% sell | 0.05% sell (from 1 Apr 2026) |
| NSE transaction | 0.00307% | 0.00307% | 0.00183% |
| SEBI fee | ₹10/crore | ₹10/crore | ₹10/crore |
| Stamp duty (buy only) | 0.015% | 0.003% | 0.002% |
| DP charge (sell) | ₹20 per scrip (set in `.env`) | – | – |
| GST | 18% on brokerage + exchange + SEBI + DP | | |

The model was checked against a published Kotak delivery example and gave the same total, ₹715.58.

**Not included:**
- Income tax on profits.
- Auto square-off and call-and-trade fees (₹49 + GST each).
- MTF interest.

STT and exchange fees change with Budgets and circulars, so re-check the rates once a year. Then compare against one real contract note.

## How the signals work

| Price (futures) | OI | Label |
|---|---|---|
| ↑ | ↑ | Long Build-up (LB) |
| ↓ | ↑ | Short Build-up (SB) |
| ↑ | ↓ | Short Covering (SC) |
| ↓ | ↓ | Long Unwinding (LU) |

- **Score (0–100)** adds up four parts:
  - Momentum: up to 45 points, from |%chg| (full at 3%).
  - OI change: up to 25 points, from |OI%| (full at 8%).
  - Relative volume: up to 20 points.
  - Alignment: 10 points when the build-up confirms the price direction.

  The weights live in `analytics.score()` and are meant to be tuned to your own trading style.
- **Support / Resistance** are the nearest classic floor pivots (S2, S1, P, R1, R2) below and above LTP, calculated from the previous day's high, low and close.

## Next steps
- Add Nifty, BankNifty and option-chain PCR / max-pain using the SDK's option-chain function.
- For 200+ instruments, move the tile layer from SVG to Canvas so it stays smooth.
- Run it on a small VM with systemd and restart it daily at 09:00. Each restart logs in again, which is what keeps the daily session working.
- Data from Kotak is licensed for your own use. Redistributing it, for example on a public site, needs an NSE data licence.
