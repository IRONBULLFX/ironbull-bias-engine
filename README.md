# IRONBULL FX Daily Bias Terminal: backend (Railway)

Runs entirely on Railway. Nothing runs on your computer.

## What it does
- **Fundamental bias (automatic):** every 10 minutes (every 2 minutes from 30 min before to 60 min after a high-impact
  USD release) it pulls your macro backend (calendar, yields, Fed stance) plus FRED (real yields, breakevens, VIX,
  high-yield credit spread). It then scores USD, XAUUSD, US30 and NASDAQ and stores the result and any bias change in Postgres.
- **Technical bias (chart uploads):** a member sends a chart screenshot plus their access code. Claude vision reads it,
  the code scores it. Each member's technical bias uses only their own charts. D1 + H4 is the minimum for a bias.

## Endpoints
| Endpoint | Who | What |
|---|---|---|
| GET /health | anyone | last calculation, stale flag, failing sources |
| GET /api/bias | anyone (+ header X-Access-Code for your own technical) | full dashboard payload |
| GET /api/history?asset=USD&days=7 | anyone | bias changes + fundamental score over time |
| POST /api/analyze (form: asset, tf, file) | header X-Access-Code | upload one chart |
| POST /api/refresh | header X-Admin-Token | force a fundamental recalculation now |

## Railway variables
| Variable | Purpose |
|---|---|
| ANTHROPIC_API_KEY | reads chart screenshots |
| FRED_KEY | free key from fred.stlouisfed.org, needed for gold, US30 and risk factors |
| ACCESS_CODES | member codes, comma separated, e.g. `IBX-7F3K,IBX-Q92M` |
| ADMIN_TOKEN | long random string, only for you |
| DAILY_UPLOAD_LIMIT | charts per member per day (default 40) |
| DAILY_GLOBAL_LIMIT | charts per day for everyone (default 400), caps your Anthropic bill |
| ALLOWED_ORIGINS | `*` for now; set to your Netlify URL once the page is live |
| DATABASE_URL | added automatically when you attach Railway Postgres |
| MACRO_API_BASE | optional, defaults to your existing macro backend |

## Honest limits
- Chart reading is an AI visual read of a screenshot, not calculated from price data. Prices come off the axis labels,
  so misreads are possible. Confidence is capped at MODERATE.
- No earnings or AI/semis data yet, so those fundamental factors are empty and coverage shows it.
- Weights, thresholds, surprise sizes and VIX/credit levels are starting assumptions, not validated results.
- The Fed stance comes from your backend's 2Y-yield proxy, not FedWatch odds.
- Run as ONE Railway replica (railway.json sets this); more replicas would duplicate the scheduler.

## Tests (for development only)
`pip install -r requirements-dev.txt && python -m pytest -q` runs 97 tests. Verified on Python 3.12 and against Postgres 16.
