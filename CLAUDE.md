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

Egyetlen `asyncio` processz (`python -m app`), öt párhuzamos taskkal, amelyeket az `ShadowSignalApp.run()` indít:

- **`market_loop`** — `aggTrade` + kline WS (`wss://fstream.binance.com/market/stream`). Ez a fő eseményforrás: minden `aggTrade` frissíti a VWAP-ot, az 1 perces CVD bucketet, az outcome trackereket, és **itt fut a teljes state machine**. Nincs külön ticker loop; a stratégia trade-eseményvezérelt.
- **`public_loop`** — `bookTicker` WS (`wss://fstream.binance.com/public/stream`), külön endpoint, külön reconnect backoff. Csak bid/ask-ot és frissességet tart karban.
- **`heartbeat_loop`** — `heartbeatSec`-enként állapotdoc a `heartbeats` collectionbe.
- **`status_loop`** — `logStatusSec`-enként egy összefoglaló + symbolonként egy állapotsor a logba (`_log_status` / `_status_line`).
- **`loop_lag_loop`** — 1 másodperces alvás tényleges késését méri; a `max_loop_lag_sec` a `STATUS` sorba és a heartbeatbe kerül. Ez a diagnosztika arra, hogy telített event loop okoz-e WebSocket keepalive timeoutot.

Modulok: `main.py` (app + state machine + outcome mérés), `binance_feed.py` (WS/REST I/O + `StreamStats`), `indicators.py` (tiszta függvények: EMA, Wilder ATR, CVD polyfit, VWAP predikátumok, spread, return), `validation.py` (a signal validáció tiszta függvényként, `ValidationResult`), `symbols.py` (`select_symbols` tiszta szűrés/rendezés), `models.py` (`Candle`, `FlowBucket`, `OutcomeTracker`, `SymbolRuntime`), `config.py` (default doc + validáció + `symbolOverrides` merge), `storage.py` (Mongo, indexek, collection bootstrap), `telegram.py`.

A futtatandó symbolok listája **nem** a `ShadowSignalApp` dolga: az `async_main` oldja fel (`resolve_symbols`), és konstruktorparaméterként adja át. Auto-populate hibája vagy üres eredménye mindig a `config.symbols`-ra esik vissza — nulla symbollal soha nem indulunk.

A per-symbol állapot teljes egészében egy in-memory `SymbolRuntime` objektumban él (`app.runtimes[symbol]`). A Mongo **nem** state store a stratégia számára: restart után csak a `signals` collection utolsó eleméből áll vissza a COOLDOWN, minden más újraszámolódik REST kline bootstrapből.

### Kritikus invariánsok

Ezek nem stilisztikai döntések, hanem a specifikáció adatintegritási szabályai — módosításnál tudatosan kell eldönteni, hogy megszeged-e őket:

1. **Csak lezárt candle megy indikátorba.** A `_apply_closed_candle` inkrementálisan lépteti az EMA/ATR-t (`ema_next` / `wilder_atr_next`); a nyitott candle sosem módosítja a bandeket. A `_handle_kline` eldobja a `k.x != true` eseményeket, a `fetch_closed_klines` eldobja a jövőbeli `closeTime`-ú sort.
2. **Részlegesen látott ablak nem használható.** Indulás és WS reconnect után a `_mark_market_gap()` nullázza a VWAP-ot, a CVD dequet, a `previous_price`-t, és `market_stream_continuous = False`-ra állít. Az első hiányosan látott 15m VWAP-ablak (`vwap_complete=False`) és az első hiányos 1m CVD bucket (`complete_capture=False` → `None` a dequeben) nem használható signalhoz. Ne "pótold" kitalált adattal.
3. **`_indicator_data_aligned`** minden trade előtt ellenőrzi, hogy az entry/exit indikátor tényleg a *közvetlenül előző* lezárt candle-ig van léptetve; ha nem, nincs signal. Ez fogja el a néma kline-lemaradást.
4. **Re-entry, nem szint alatti tartózkodás.** `LONG_ARMED`-ből csak `previous_price <= lowerEntry and price > lowerEntry` átlépéskor van signal-kísérlet. Entry candle záráskor `previous_price = None`, tehát candle-határon nem keletkezhet hamis crossing.
5. **Sikertelen validáció nem lép ki az ARMED állapotból.** Csak kiírt signal visz `COOLDOWN`-ba.
6. **A program soha nem küld ordert**, a 20 perces mérés nem pozíciózárás. Megszakadt mérés `INTERRUPTED`, nem interpolált érték (`_interrupt_active_measurements`, illetve induláskor `mark_stale_active_measurements_interrupted`).

### Signal validáció (`validation.evaluate_validation`)

Egyetlen helyen dől el, ebben a sorrendben: VWAP strict feltétel (`bullish_vwap`/`bearish_vwap`) → CVD slope/curvature előjel (`cvd_lookback` darab *teljes* bucketből) → spread bps ≤ `maxSpreadBps` → trade/book frissesség. Új feltételt ide tegyél, ne a `_try_signal`-ba.

A visszatérés `ValidationResult`: elfogadásnál `snapshot`, elutasításnál egy stabil `reason` kód és egy `detail` string a számokkal. A `reason` kódok logba és tesztbe is mennek — ha megváltoztatod, a `tests/test_validation.py` elbukik. A függvény tiszta: a `now_ms`-t paraméterként kapja, nem `time.time()`-ot hív.

### Config

Egyetlen `config` dokumentum (`_id: "strategy"`), első indításkor a `DEFAULT_CONFIG`-ból jön létre. Meglévő dokumentumba a `Storage.initialize` bemergeli a hiányzó default kulcsokat, így új config mező felvétele nem töri meg a futó telepítést.

`StrategyConfig.for_symbol()` merge-eli a `symbolOverrides`-t — csak az `OVERRIDABLE_KEYS` írható felül. A `GLOBAL_ONLY_KEYS` halmaz mondja meg, mi **nem** kerül bele a per-symbol settingsbe (és ezzel a `signals.configSnapshot`-ba sem): a timeframe-eken túl a `logStatusSec`, `symbolAutoPopulate`, `minQuoteVolume24h`, `maxSymbols`. Új stratégiai paraméter felvételekor: `DEFAULT_CONFIG` + `validate_symbol_settings` + (ha per-symbol) `OVERRIDABLE_KEYS`; új *globális* paraméternél `DEFAULT_CONFIG` + `GLOBAL_ONLY_KEYS` + `validate_config_document`.

A `cvdLookback` minimuma 3 a kvadratikus polyfit miatt. A `cvd_deltas` deque `maxlen`-je a **bootstrap-kori** `cvdLookback` — lookback növelése futásidőben nem támogatott.

### Mongo

Pontosan öt collection: `config`, `candles`, `flow_1m`, `signals`, `heartbeats`. Ezen kívüli collectionre a `Storage.initialize` egy info sort logol (megosztott Mongónál ez normális). `pymongo` `AsyncMongoClient` (nem motor), `tz_aware=True`. Idempotens írás: `candles` és `flow_1m` unique indexre upsertel.

### Diagnosztika

A `StreamStats` kapcsolatonként számolja a csatlakozásokat és üzeneteket; a `_subscribe_loop` logolja a `SUBSCRIBE` ack-et, és WARNING-ot ad, ha 15 másodpercig egyetlen üzenet sem érkezik. A stream szétesése egysoros WARNING (`LOG_LEVEL=DEBUG`-on van teljes traceback) — ne állítsd vissza `logger.exception`-re, mert percenként ismétlődő reconnectnél olvashatatlanná teszi a logot.

## Amit szándékosan nem tartalmaz

Nincs FastAPI, frontend, Redis, Kafka, ML/HMM, TA-Lib, pandas, RSI/MACD/Bollinger, orderküldés, rétegzett architektúra. Ne vezess be ilyet a scope kimondott bővítése nélkül.
