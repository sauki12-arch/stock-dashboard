# AI Stock Scanner Pro – Quant Edition: Specification (Phase 1)

Decision-support system for US equities. **Not** a brokerage bot: there is no
order-routing code in this repository. "Paper trading" is a local ledger only.

## 1. Fixed configuration

| Item | Value |
|---|---|
| Platform | Windows desktop (PySide6), Hebrew RTL |
| Universe | 500 configurable symbols (`config/universe_default.csv`) |
| Strategies | Swing (≈2–10 trading days), Long-Term (months–years) |
| Risk per new trade | 1 % of current equity |
| Max simultaneous positions | 10 |
| Alerts | Critical only, Telegram |
| Data (dev) | Yahoo Finance via `yfinance` (unofficial, no SLA, personal use) |
| Mode | Analysis + paper trading only |

## 2. Package layout

```
aiscanner/
  paths.py config.py i18n.py secrets.py models.py logging_setup.py
  data/        calendar.py provider.py yahoo.py mock.py store.py
               validation.py universe.py ratelimit.py engine.py
  discovery/   indicators.py patterns.py scoring.py scanner.py
  brain/       regime.py
  portfolio/   store.py risk.py broker.py monitor.py importer.py
  alerts/      manager.py telegram.py
  backtest/    engine.py walkforward.py metrics.py
  review/      weekly.py
  scheduler.py diagnostics.py cli.py
  ui/          theme.py models.py charts.py workers.py screens*.py main_window.py
tests/         pytest suite (no network, no GUI required except ui tests)
```

Dependency direction (no cycles): `data → discovery → brain → portfolio → alerts → ui`.
`backtest` and `review` consume `data` + `discovery`.

## 3. Layer interfaces

### Layer 1 – `DataProvider` (ABC)
```
name: str ; is_synthetic: bool
fetch_daily(symbols, start, end) -> dict[symbol, DataFrame | ProviderError]
fetch_intraday(symbol, interval, period) -> DataFrame
```
DataFrame contract: index = tz-naive `DatetimeIndex` of **exchange-local trading
dates** (daily) or **UTC timestamps** (intraday); columns `open high low close volume`,
all prices split/dividend adjusted. Providers never raise for a single bad symbol –
they return a `ProviderError` entry. `DataEngine` isolates provider failures,
retries with exponential backoff + jitter, and rate-limits.

### Look-ahead guards
* A daily bar is only accepted once its session is complete (close + grace).
  `drop_incomplete_bars()` removes the in-progress bar.
* All indicators are causal (`rolling`/`ewm`); weekly values use only completed weeks.
* Backtest decisions at bar *t* are executed at the open of bar *t+1*.

### Layer 2 – `RegimeEngine.assess(...) -> RegimeAssessment`
Regime ∈ {RISK_ON, NEUTRAL, RISK_OFF}; `score` 0-100, `confidence` 0-1,
`calibration` = `"uncalibrated"` until a backtest calibration record exists,
`evidence / counterarguments / invalidations` as structured codes rendered by `i18n`.

### Layer 3 – Scanner / scoring
Points: trend 20, momentum 15, volume 15, setup 15, relative strength 10,
volatility 10, risk/reward 15 = 100. Bands (provisional): ≥85 high priority,
70-84 strong watchlist, 55-69 monitoring, <55 low. Hard gates (liquidity, price,
valid/fresh history, computable risk, spread proxy) are applied **before** ranking.

### Layer 4 – Risk (deterministic, no AI)
`shares = floor(equity × 0.01 / |entry − stop|)`; long only; rejection reasons:
invalid stop, insufficient cash, position-size cap, 10-position cap, aggregate
open-risk cap, sector cap, stale/unreliable data, duplicate symbol, entry far from
last price. `PaperBroker` only accepts an `ApprovedOrder` minted by `RiskEngine`.

### Layer 5 – UI + Telegram
Nine screens (overview, regime, discovery, swing, long-term, portfolio, risk,
alerts, settings/diagnostics). Telegram: critical only, de-duplicated, quiet hours,
rate-limited, token never in source (stored via DPAPI on Windows).

## 4. SQLite schema (single file `scanner.db`)

```
bars(symbol, interval, ts, open, high, low, close, volume, PRIMARY KEY(symbol,interval,ts))
fetch_log(id, ts, symbol, interval, status, rows, message)
symbol_meta(symbol, last_full_refresh, last_bar, note)
scan_runs(id, ts, as_of, regime, n_universe, n_eligible, provider, synthetic, version)
scan_results(run_id, symbol, strategy, score, klass, validated, eligible, payload_json)
positions(symbol PK, qty, avg_cost, stop, target, sector, strategy, opened_at, notes)
trades(id, ts, symbol, side, qty, price, fees, realized_pl, note)
account(key PK, value)            -- cash, starting_equity
equity_history(date PK, equity, cash)
alerts(id, ts, dedupe_key, severity, kind, symbol, title, body, status, sent_ts, reason)
job_runs(job PK, last_run_ts)
score_versions(version PK, ts, weights_json, approved, note)
```

## 5. Acceptance criteria (mapped to tests)

| # | Criterion | Verified by |
|---|---|---|
| 1 | Launcher exists and starts app | `RUN_APP.bat` (Windows – **manual**), `test_ui_smoke.py` (offscreen) |
| 2 | Hebrew RTL UI | `test_ui_smoke.py` (layout direction, strings) |
| 3 | 500-symbol universe processed | `test_scanner.py::test_full_500_universe_mock` |
| 4 | Explainable rankings | `test_scoring.py` |
| 5 | Portfolio tracking | `test_portfolio.py` |
| 6 | Deterministic risk | `test_risk.py` |
| 7 | Telegram | `test_alerts.py` (mock HTTP) + CLI `telegram-test` (**manual**, needs your token) |
| 8 | Survives partial failures | `test_data_engine.py` |
| 9 | Paper trading | `test_broker.py` |
| 10 | EXE procedure | `BUILD_EXE.bat`, `build/aiscanner.spec` (Windows – **manual**) |

Items marked **manual** cannot be executed in the Linux CI sandbox where this
code was written; see README "Known limitations".
