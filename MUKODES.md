# Binance Futures Shadow Signal - pontos mukodes es konfiguracio

Ez a dokumentum a projekt tenyleges kodjanak mukodeset irja le.

## 1. Indulasi folyamat

A container a `python -m app` parancsot inditja, ami az `app/main.py` `async_main()` fuggvenyebe jut.

Indulaskor a sorrend:

1. Betolti a `.env` fajlt.
2. Kiolvassa a `MONGO_URI`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `LOG_LEVEL` ertekeket.
3. Letrehozza a MongoDB kapcsolatot.
4. `ping` paranccsal ellenorzi a MongoDB elerhetoseget.
5. Ellenorzi az alkalmazas 5 collectionjet, es amelyik hianyzik, explicit letrehozza.
6. Letrehozza a szukseges indexeket.
7. Megkeresi a `config` collectionben az `_id: "strategy"` dokumentumot.
8. Ha ez a dokumentum nem letezik, beszurja a teljes default strategia configot.
9. Validalja a configot. Hibas vagy hianyos config eseten nem indul el csendben mas ertekekkel, hanem hibat dob.
10. A `symbols` tomb minden elemehez letrehoz egy runtime objektumot.
11. Symbolonkent leker 500 lezart entry es exit timeframe candle-t a Binance REST API-bol.
12. Ezeket elmenti a `candles` collectionbe, es kiszamolja a kezdo EMA/ATR ertekeket.
13. Megnezi a symbol legutobbi signaljat. Ha annak cooldownja meg aktiv, a runtime `COOLDOWN` allapotban indul.
14. Elindul a Binance market WebSocket, a bookTicker WebSocket es a heartbeat loop.

## 2. Melyik MongoDB adatbazist hasznalja?

Az adatbazis a `MONGO_URI`-bol jon.

Pelda:

```dotenv
MONGO_URI=mongodb://mongo:27017/shadow_signals
```

Ebben az esetben az adatbazis neve `shadow_signals`.

Ha a connection stringben nincs adatbazisnev, a kod fallbackkent a `shadow_signals` nevet hasznalja.

## 3. Collectionok letrehozasa

Az alkalmazas pontosan ezt az ot collectiont kezeli:

```text
config
candles
flow_1m
signals
heartbeats
```

A `Storage.initialize()` indulaskor meghivja a MongoDB `list_collection_names()` muveletet, majd minden hianyzo collectionre `create_collection()` fut.

Tehat:

- teljesen ures MongoDB eseten mind az 5 letrejon;
- ha csak nehany van meg, csak a hianyzok jonnek letre;
- az alkalmazas nem hoz letre hatodik sajat collectiont;
- ha az adatbazisban korabbi/idegen extra collection van, azt nem torli, csak warningot ir a logba.

Letrehozott indexek:

- `candles`: egyedi `symbol + timeframe + openTime`
- `flow_1m`: egyedi `symbol + bucketStart`
- `signals`: `symbol + signalAt`
- `signals`: `measurementStatus + signalAt`
- `heartbeats`: `ts`

A `config` collectionben az `_id` alap MongoDB index elegendo a `strategy` dokumentumhoz.

## 4. Mi tortenik, ha nincs config?

Indulaskor ezt keresi:

```javascript
db.config.findOne({_id: "strategy"})
```

Ha nincs ilyen dokumentum, a program automatikusan beszurja ezt:

```json
{
  "_id": "strategy",
  "symbols": [
    "BTCUSDT",
    "ETHUSDT"
  ],
  "entryTimeframe": "15m",
  "exitTimeframe": "1h",
  "emaPeriod": 20,
  "atrPeriod": 14,
  "xEntry": 1.75,
  "xExit": 1.75,
  "cvdLookback": 5,
  "maxSpreadBps": 10,
  "bookMaxAgeSec": 3,
  "tradeMaxAgeSec": 3,
  "cooldownSec": 600,
  "heartbeatSec": 3600,
  "symbolOverrides": {},
  "updatedAt": null
}
```

Fontos: a `DEFAULT_CONFIG` a Python kodban csak bootstrap sablon. A futas soran hasznalt strategiai config a MongoDB-bol beolvasott `strategy` dokumentum.

Ha a `strategy` dokumentum mar letezik, a program nem irja felul default ertekekkel.

## 5. Mi tortenik, ha a config letezik, de hibas?

A kod fail-fast modon mukodik.

Pelda hibak:

- hianyzik egy kotelezo mezo;
- `symbols` ures;
- ugyanaz a symbol tobbszor szerepel, akar mas kis/nagybetuvel;
- `emaPeriod < 2`;
- `atrPeriod < 2`;
- `cvdLookback < 3`;
- negativ `maxSpreadBps`;
- nem tamogatott `symbolOverrides` mezo.

Ilyenkor a processz hibat dob es nem indul el rejtett/default strategiai ertekekkel.

Docker Compose alatt a `restart: unless-stopped` miatt a container ujra probalkozhat; a konkret config hiba a logban latszik.

## 6. Hol kell megadni a figyelt symbolokat?

Kizarolag a MongoDB `config` collection `strategy` dokumentumanak `symbols` tombjeben.

Pelda:

```json
{
  "_id": "strategy",
  "symbols": [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT"
  ]
}
```

A teljes dokumentum tobbi kotelezo mezojet termeszetesen meg kell tartani.

Docker Compose mellett pelda modositas:

```bash
docker compose exec mongo mongosh shadow_signals --quiet --eval '
db.config.updateOne(
  {_id: "strategy"},
  {$set: {
    symbols: ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"],
    updatedAt: new Date()
  }}
)'
```

Ezutan az alkalmazast ujra kell inditani:

```bash
docker compose restart shadow-signal
```

A config jelenleg szandekosan nem hot-reloados: indulaskor egyszer olvasodik be. Ez egyszerubb es determinisztikusabb mukodest ad. Config vagy symbol lista modositas utan `shadow-signal` restart kell; a Mongo containert nem kell ujrainditani.

A symbolok kisbetuvel is megadhatok, mert a runtime nagybetusre normalizalja oket, de az atlathatosag miatt az uppercase forma javasolt.

## 7. Symbolonkénti override

A globalis config mellett a `symbolOverrides` hasznalhato.

Pelda:

```json
{
  "symbolOverrides": {
    "BTCUSDT": {
      "xEntry": 2.0,
      "maxSpreadBps": 5
    },
    "ETHUSDT": {
      "xEntry": 1.5
    }
  }
}
```

Felulirhato mezok:

```text
emaPeriod
atrPeriod
xEntry
xExit
cvdLookback
maxSpreadBps
bookMaxAgeSec
tradeMaxAgeSec
cooldownSec
```

A `symbols` lista donti el, hogy egy symbol egyaltalan figyelve van-e. Attol, hogy valami szerepel a `symbolOverrides` objektumban, meg nem lesz automatikusan figyelt symbol.

## 8. Hogyan dolgozik fel egy symbolt?

Minden configban szereplo symbolhoz kulon `SymbolRuntime` tartozik.

Ebben vannak az aktualis, nem persistent gyors runtime adatok:

- state;
- entry EMA/ATR;
- exit EMA/ATR;
- elozo es aktualis aggTrade ar;
- legutobbi bid/ask;
- realtime 15m VWAP akkumulator;
- aktualis 1 perces CVD bucket;
- utolso lezart CVD normalizedDelta ertekek;
- cooldown vege.

Persistent adatok MongoDB-be kerulnek; a nagy frekvenciaju, minden trade-re valtozo runtime akkumulatorokat nem irja ki minden aggTrade-nel MongoDB-be.

## 9. Candle es EMA/ATR

Indulaskor a program 500 lezart candle-t ker le symbolonkent.

Entry oldal:

```text
15m
EMA15m
ATR15m
lowerEntry = EMA15m - xEntry * ATR15m
upperEntry = EMA15m + xEntry * ATR15m
```

Exit guideline oldal:

```text
1h
EMA1h
ATR1h
LONG guideline  = EMA1h + xExit * ATR1h
SHORT guideline = EMA1h - xExit * ATR1h
```

A WebSocket kline eventbol csak `x == true`, vagyis lezart candle kerul feldolgozasra.

Nyitott candle nem frissiti az EMA/ATR-t.

EMA sajat implementacio:

```text
alpha = 2 / (N + 1)
seed EMA = first N close atlaga
EMA_t = alpha * close_t + (1-alpha) * EMA_(t-1)
```

ATR sajat Wilder implementacio:

```text
TR = max(high-low, abs(high-previousClose), abs(low-previousClose))
seed ATR = first N TR atlaga
ATR_t = ((N-1) * ATR_previous + TR_t) / N
```

## 10. Live aggTrade feldolgozas

Minden `aggTrade` eventnel:

1. megkeresi a symbol runtimejat;
2. kiolvassa a `price`, `qty`, `T`, `m` mezoket;
3. frissiti a legutobbi live arat;
4. frissiti a 15m realtime VWAP-ot;
5. frissiti az 1 perces CVD bucketet;
6. frissiti az aktiv signal outcome mereseket;
7. kezeli a cooldown lejaratat;
8. megvizsgalja az `IDLE/LONG_ARMED/SHORT_ARMED` state logikat;
9. re-entry eseten lefuttatja a pontos VWAP + CVD + spread + freshness validaciot.

## 11. Realtime VWAP

A VWAP csak az `aggTrade` streambol keszul:

```text
notional = price * qty
VWAP = sum(price * qty) / sum(qty)
```

Minden uj entry timeframe bucketnel nullazodik.

A `candleOpen` az adott teljesen megfigyelt live bucket elso feldolgozott aggTrade ara.

Processzindulas vagy market-stream reconnect utan a mar folyamatban levo, csak reszben latott 15m bucket nem hasznalhato signal-validaciora. A kovetkezo teljesen megfigyelt 15m bucket hasznalhato.

Ez adatminosegi vedelem, nem uj technikai indikator.

## 12. CVD

`aggTrade.m` kezelese:

```text
m == true  -> buyer maker -> agresszor SELL
m == false -> agresszor BUY
```

Minden trade:

```text
notional = price * qty
```

Lezart 1 perces bucket:

```text
buyNotional
sellNotional
delta = buyNotional - sellNotional
total = buyNotional + sellNotional
normalizedDelta = delta / total
```

A slope es curvature csak az utolso `cvdLookback` darab teljes, lezart bucketbol keszul.

```text
normalizedCvdSeries = cumulative sum(normalizedDelta-k)
x = numpy.linspace(-1, 1, len(series))
slope = numpy.polyfit(x, series, 1)[0]
curvature = 2 * numpy.polyfit(x, series, 2)[0]
```

LONG:

```text
slope > 0
curvature >= 0
```

SHORT:

```text
slope < 0
curvature <= 0
```

Nincs magnitude threshold.

## 13. Spread

A kulon `bookTicker` stream frissiti:

```text
bestBid
bestAsk
```

Szamitas:

```text
mid = (bid + ask) / 2
spreadBps = (ask - bid) / mid * 10000
```

Signal nincs, ha:

```text
spreadBps > maxSpreadBps
```

vagy a bookTicker regebbi, mint `bookMaxAgeSec`.

Az aggTrade freshness ugyanigy `tradeMaxAgeSec` alapjan ellenorzott.

## 14. State machine

Strategiai state-kent csak ez a negy ertek letezik:

```text
IDLE
LONG_ARMED
SHORT_ARMED
COOLDOWN
```

Nincs `CONFIRM`, `WAIT`, `BREAKOUT`, `POSITION` vagy `EXIT` strategiai state.

### LONG

`IDLE`:

```text
price < lowerEntry -> LONG_ARMED
```

`LONG_ARMED`:

```text
previousPrice <= lowerEntry
currentPrice > lowerEntry
```

Ekkor ellenorzi:

```text
currentPrice > candleOpen
candleOpen < VWAP < currentPrice
cvdSlope > 0
cvdCurvature >= 0
spreadBps <= maxSpreadBps
aggTrade friss
bookTicker friss
```

Ha minden jo:

```text
LONG SIGNAL
Telegram
COOLDOWN
```

Ha barmelyik validacio nem jo, `LONG_ARMED` marad.

### SHORT

Pontosan tukorkep:

```text
price > upperEntry -> SHORT_ARMED
previousPrice >= upperEntry
currentPrice < upperEntry
currentPrice < candleOpen
currentPrice < VWAP < candleOpen
cvdSlope < 0
cvdCurvature <= 0
```

## 15. Signal mentese

Signal eseten a `signals` dokumentum tartalmazza tobbek kozott:

- symbol;
- LONG/SHORT oldal;
- signal idopont;
- signalPrice;
- entry timeframe;
- entry EMA/ATR;
- xEntry;
- lowerEntry/upperEntry;
- exit timeframe;
- exit EMA/ATR;
- xExit;
- exit guideline price;
- candleOpen;
- VWAP;
- normalizedCvdSeries;
- CVD slope;
- CVD curvature;
- bid/ask;
- spreadBps;
- trade/book age;
- `configSnapshot`;
- Telegram statusz;
- outcome mezok.

A `configSnapshot` azert fontos, mert kesobb is latszik, milyen konkret strategiai parameterekkel szuletett az adott signal.

## 16. Cooldown

Signal utan:

```text
state = COOLDOWN
cooldownUntil = signalAt + cooldownSec
```

Cooldown alatt az adott symbol nem general uj signalt.

A cooldown lejarta utan a kovetkezo aggTrade feldolgozasakor:

```text
COOLDOWN -> IDLE
```

Processz restartnal a program a symbol legutobbi MongoDB signaljabol ujra kiszamolja, hogy a cooldown meg aktiv-e.

## 17. Outcome meres

A signal nem jelent Binance poziciot. Nincs orderkuldes es nincs automatikus exit.

A signal utan a live `aggTrade.price` alapjan mer:

```text
1m
3m
5m
10m
15m
20m
```

LONG:

```text
(currentPrice - signalPrice) / signalPrice * 100
```

SHORT:

```text
(signalPrice - currentPrice) / signalPrice * 100
```

Mentett mezok:

```text
return1m
return3m
return5m
return10m
return15m
return20m
MFE
MAE
timeToMFE
timeToMAE
```

20 percnel a meres `COMPLETED` lesz, de ez nem position exit.

Ha processz restart vagy market-stream gap miatt nincs folyamatos arut, az aktiv meres `INTERRUPTED` lesz, hogy ne keletkezzen hamis outcome statisztika.

## 18. Heartbeat

`heartbeatSec` idokozonkent dokumentum kerul a `heartbeats` collectionbe.

Symbolonkent latszik peldaul:

- aktualis state;
- lastPrice;
- lowerEntry;
- upperEntry;
- trade/book adat kora;
- VWAP teljes-e;
- hany lezart CVD bucket all rendelkezesre;
- hany aktiv outcome meres fut.

## 19. Config modositas utan mi kell?

A config nem hot-reloados.

Tehat:

```text
Mongo config update
        |
        v
docker compose restart shadow-signal
        |
        v
uj config beolvasasa
        |
        v
uj symbol lista + uj runtime-ok
```

A MongoDB-t nem kell restartolni, es a regi `candles`, `flow_1m`, `signals`, `heartbeats` adatok megmaradnak.

## 20. Hol van mindez a kodban?

```text
app/config.py        default config, validacio, symbolOverrides
app/storage.py       Mongo kapcsolat, collectionok, config letrehozas, indexek, mentes
app/binance_feed.py  Binance REST + WebSocket kapcsolatok
app/indicators.py    EMA, Wilder ATR, CVD, VWAP/spread validacio, return
app/models.py        Candle, FlowBucket, SymbolRuntime, OutcomeTracker
app/main.py          teljes runtime flow, state machine, signal es outcome
app/telegram.py      Telegram sendMessage
```
