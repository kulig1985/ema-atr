# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Nyelv

A README.md és a MUKODES.md magyarul íródott; a kód, a logüzenetek és a Mongo mezőnevek angolul. Új dokumentációt magyarul, kódot/logot angolul írj.

## Parancsok

```bash
# Lokális fejlesztés (Python 3.12+)
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q                                  # összes teszt
pytest tests/test_indicators.py -q         # egy fájl
pytest tests/test_indicators.py::test_ema_matches_manual_recursion -q   # egy teszt

# Futtatás
docker compose up --build -d
docker compose logs -f shadow-signal
docker compose restart shadow-signal       # config módosítás után CSAK ezt (a config induláskor olvasódik be)
docker compose down                        # -v csak ha a Mongo adatot is szándékosan törlöd

# Config módosítás
docker compose exec mongo mongosh shadow_signals --quiet --eval '
db.config.updateOne({_id:"strategy"}, {$set:{xEntry:2.0, updatedAt:new Date()}})'
```

Nincs linter/formatter konfigurálva. A futtatáshoz `.env` kell (`cp .env.example .env`), Binance API kulcs nem — csak publikus market adat.

## Architektúra

Egyetlen `asyncio` processz (`python -m app`), három párhuzamos taskkal, amelyeket az `ShadowSignalApp.run()` indít:

- **`market_loop`** — `aggTrade` + kline WS (`wss://fstream.binance.com/market/stream`). Ez a fő eseményforrás: minden `aggTrade` frissíti a VWAP-ot, az 1 perces CVD bucketet, az outcome trackereket, és **itt fut a teljes state machine**. Nincs külön ticker loop; a stratégia trade-eseményvezérelt.
- **`public_loop`** — `bookTicker` WS (`wss://fstream.binance.com/public/stream`), külön endpoint, külön reconnect backoff. Csak bid/ask-ot és frissességet tart karban.
- **`heartbeat_loop`** — `heartbeatSec`-enként állapotdoc a `heartbeats` collectionbe.

Modulok: `main.py` (app + state machine + outcome mérés), `binance_feed.py` (WS/REST I/O), `indicators.py` (tiszta függvények: EMA, Wilder ATR, CVD polyfit, VWAP predikátumok, spread, return), `models.py` (`Candle`, `FlowBucket`, `OutcomeTracker`, `SymbolRuntime`), `config.py` (default doc + validáció + `symbolOverrides` merge), `storage.py` (Mongo, indexek, collection bootstrap), `telegram.py`.

A per-symbol állapot teljes egészében egy in-memory `SymbolRuntime` objektumban él (`app.runtimes[symbol]`). A Mongo **nem** state store a stratégia számára: restart után csak a `signals` collection utolsó eleméből áll vissza a COOLDOWN, minden más újraszámolódik REST kline bootstrapből.

### Kritikus invariánsok

Ezek nem stilisztikai döntések, hanem a specifikáció adatintegritási szabályai — módosításnál tudatosan kell eldönteni, hogy megszeged-e őket:

1. **Csak lezárt candle megy indikátorba.** A `_apply_closed_candle` inkrementálisan lépteti az EMA/ATR-t (`ema_next` / `wilder_atr_next`); a nyitott candle sosem módosítja a bandeket. A `_handle_kline` eldobja a `k.x != true` eseményeket, a `fetch_closed_klines` eldobja a jövőbeli `closeTime`-ú sort.
2. **Részlegesen látott ablak nem használható.** Indulás és WS reconnect után a `_mark_market_gap()` nullázza a VWAP-ot, a CVD dequet, a `previous_price`-t, és `market_stream_continuous = False`-ra állít. Az első hiányosan látott 15m VWAP-ablak (`vwap_complete=False`) és az első hiányos 1m CVD bucket (`complete_capture=False` → `None` a dequeben) nem használható signalhoz. Ne "pótold" kitalált adattal.
3. **`_indicator_data_aligned`** minden trade előtt ellenőrzi, hogy az entry/exit indikátor tényleg a *közvetlenül előző* lezárt candle-ig van léptetve; ha nem, nincs signal. Ez fogja el a néma kline-lemaradást.
4. **Re-entry, nem szint alatti tartózkodás.** `LONG_ARMED`-ből csak `previous_price <= lowerEntry and price > lowerEntry` átlépéskor van signal-kísérlet. Entry candle záráskor `previous_price = None`, tehát candle-határon nem keletkezhet hamis crossing.
5. **Sikertelen validáció nem lép ki az ARMED állapotból.** Csak kiírt signal visz `COOLDOWN`-ba.
6. **A program soha nem küld ordert**, a 20 perces mérés nem pozíciózárás. Megszakadt mérés `INTERRUPTED`, nem interpolált érték (`_interrupt_active_measurements`, illetve induláskor `mark_stale_active_measurements_interrupted`).

### Signal validáció (`_validation_snapshot`)

Egyetlen helyen, `None` visszatéréssel utasít el: VWAP strict feltétel (`bullish_vwap`/`bearish_vwap`) → CVD slope/curvature előjel (`cvd_lookback` darab *teljes* bucketből) → spread bps ≤ `maxSpreadBps` → trade/book frissesség. Új feltételt ide tegyél, ne a `_try_signal`-ba.

### Config

Egyetlen `config` dokumentum (`_id: "strategy"`), első indításkor a `DEFAULT_CONFIG`-ból jön létre. `StrategyConfig.for_symbol()` merge-eli a `symbolOverrides`-t — csak az `OVERRIDABLE_KEYS` írható felül, a timeframe-ek globálisak. Új stratégiai paraméter felvételekor: `DEFAULT_CONFIG` + `validate_symbol_settings` + (ha per-symbol) `OVERRIDABLE_KEYS`.

A `cvdLookback` minimuma 3 a kvadratikus polyfit miatt. A `cvd_deltas` deque `maxlen`-je a **bootstrap-kori** `cvdLookback` — lookback növelése futásidőben nem támogatott.

### Mongo

Pontosan öt collection: `config`, `candles`, `flow_1m`, `signals`, `heartbeats`. Ezen kívüli collectionre a `Storage.initialize` warningot logol. `pymongo` `AsyncMongoClient` (nem motor), `tz_aware=True`. Idempotens írás: `candles` és `flow_1m` unique indexre upsertel.

## Amit szándékosan nem tartalmaz

Nincs FastAPI, frontend, Redis, Kafka, ML/HMM, TA-Lib, pandas, RSI/MACD/Bollinger, orderküldés, rétegzett architektúra. Ne vezess be ilyet a scope kimondott bővítése nélkül.
