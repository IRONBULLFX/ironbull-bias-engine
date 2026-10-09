# IRONBULL FX — Daily Bias Intelligence Terminal

## Principle
Numbers first, AI second. Both engines are deterministic Python. The LLM only
writes the "WHY IS THIS THE BIAS TODAY?" text from the engines' output and
is never allowed to change a score, level or release number.

## Layout
```
engines/
  indicators.py     EMA, RSI, MACD, ATR (pure functions, causal)
  structure.py      confirmed swings, BOS, CHoCH, premium/discount
  technical.py      technical score -100..+100 (D1/H4/H1/M15)
  surprise.py       actual-vs-forecast surprise (z-score, direction by indicator meaning)
  fundamental.py    per-asset weighted factor engine
  scoring.py        thresholds, classification, confidence, agreement
  sessions.py       SAST conversion, US DST aware sessions
config/
  settings.py       weights, thresholds, indicator settings (all configurable)
tests/              pytest suite
```

## Independence rule
`technical.py` imports nothing from `fundamental.py` and vice versa. Both only
depend on `scoring.py`. `tests/test_independence.py` enforces this.

## No look-ahead / no repaint
Swings are confirmed only after N bars have closed on the right. The last
(forming) candle is dropped before any structure decision. Recomputing on a
prefix of the data must give identical results for that prefix (tested).

## Data (next phase, needs your keys / existing backend)
| Need | Candidate | Key? |
|---|---|---|
| OHLC for XAUUSD, US30, NAS100, DXY | existing macro backend feed, else Twelve Data / OANDA | yes |
| Treasury yields, real yields | FRED API | free key |
| Calendar, forecast/actual | existing manual calendar, else paid calendar API | depends |
| Releases | BLS / BEA / Fed (primary) | free |

Unavailable inputs are returned as `None` and the engine reports DATA UNAVAILABLE.
It never turns missing data into neutral.

## Hosting
Railway: `service/app.py` (FastAPI) + Railway Postgres. Background scheduler inside the app (one replica).
Netlify: single static page calling the Railway API (next step).
Technical bias comes from member chart uploads (`data/chart_vision.py` reads, `engines/vision_scoring.py` scores).
`engines/technical.py` + `data/ohlc.py` (computed from price data) are kept but unused until a price feed is added.

## Next
Netlify dashboard page, AI "why is this the bias" commentary, Telegram alerts.
