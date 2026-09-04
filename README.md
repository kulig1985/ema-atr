# Binance USDⓈ-M Futures Shadow Signal

Standalone, egyszerű Python `asyncio` alkalmazás Binance USDⓈ-M Futures shadow signalokhoz.

A program **nem küld Binance ordert**. Csak market adatot olvas, LONG/SHORT signalokat generál, Telegramra küldi őket, MongoDB-be ment, majd 20 percig méri a signal utáni ármozgást.

## Mi van benne

- Binance USDⓈ-M Futures `aggTrade` WebSocket: live ár, realtime VWAP, 1 perces CVD.
- Binance USDⓈ-M Futures `bookTicker` WebSocket: bid/ask és spread.
- Binance Futures kline: csak **lezárt** 15m és 1h candle-ek kerülnek az EMA/ATR-ba.
- Induláskori candle bootstrap: `GET /fapi/v1/klines`.
- Saját EMA és Wilder ATR implementáció; nincs TA-Lib és nincs pandas-ta.
- NumPy `polyfit` a CVD slope és curvature számításhoz.
- Telegram signal.
- MongoDB mint egyetlen persistent datastore.
- Docker Compose: egy Python alkalmazás + MongoDB.

Nincs FastAPI, frontend, Redis, Kafka, HMM, ML, RSI, Bollinger, MACD, Donchian, orderküldés vagy többrétegű alkalmazás-architektúra.

## Projektstruktúra

```text
binance-shadow-signal/
├── app/
│   ├── __init__.py
│   ├── __main__.py
│   ├── binance_feed.py
│   ├── config.py
│   ├── indicators.py
│   ├── main.py
│   ├── models.py
│   ├── storage.py
│   └── telegram.py
├── tests/
│   ├── test_indicators.py
│   └── test_models.py
├── .dockerignore
├── .env.example
├── .gitignore
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── requirements-dev.txt
└── README.md
```

## Reszletes mukodes es config

A config betoltes, automatikus default config letrehozas, collection letrehozas, symbol beallitas, runtime flow es outcome meres reszletes leirasa a `MUKODES.md` fajlban talalhato.

## State machine

Symbolonként kizárólag ez a négy stratégiai state létezik:

- `IDLE`
- `LONG_ARMED`
- `SHORT_ARMED`
- `COOLDOWN`

### LONG

15m entry band:

```text
lowerEntry = EMA15m - xEntry * ATR15m
```

1. `IDLE` és live `price < lowerEntry` → `LONG_ARMED`.
2. Signal-kísérlet csak re-entrynél:

```text
previousPrice <= lowerEntry
currentPrice  >  lowerEntry
```

3. A re-entry pillanatában mindennek teljesülnie kell:
   - `currentPrice > candleOpen`
   - `candleOpen < VWAP < currentPrice`
   - `cvdSlope > 0`
   - `cvdCurvature >= 0`
   - `spreadBps <= maxSpreadBps`
   - friss `aggTrade`
   - friss `bookTicker`
4. Ha valid → LONG signal, Telegram, `COOLDOWN`.
5. Ha nem valid → marad `LONG_ARMED`. Új signal-kísérlethez az árnak újra a band alsó oldaláról kell re-entryt csinálnia.

### SHORT

15m entry band:

```text
upperEntry = EMA15m + xEntry * ATR15m
```

Signal-kísérlet:

```text
previousPrice >= upperEntry
currentPrice  <  upperEntry
```

Validáció:

```text
currentPrice < candleOpen
currentPrice < VWAP < candleOpen
cvdSlope < 0
cvdCurvature <= 0
spreadBps <= maxSpreadBps
```

A többi feltétel a LONG tükörképe.

## EMA és ATR

Az EMA és ATR kizárólag lezárt candle-ekből számolódik. A nyitott 15m/1h candle nem módosítja a bandeket.

EMA seed:

```text
EMA = mean(first N closes)
```

Utána:

```text
alpha = 2 / (N + 1)
EMA_t = alpha * close_t + (1-alpha) * EMA_(t-1)
```

True Range:

```text
TR = max(
    high - low,
    abs(high - previousClose),
    abs(low - previousClose)
)
```

Wilder ATR seed:

```text
ATR = mean(first N TR)
```

Utána:

```text
ATR_t = ((N-1) * ATR_previous + TR_t) / N
```

## 1h exit guideline

Az 1h adat nem generál signalokat és nem zár pozíciót.

```text
LONG:  EMA1h + xExit * ATR1h
SHORT: EMA1h - xExit * ATR1h
```

A guideline bekerül a Telegram üzenetbe és a `signals` dokumentumba.

## Realtime VWAP

A jelenlegi nyitott entry timeframe candle `aggTrade` eseményeiből:

```text
notional = price * qty
VWAP = sum(price * qty) / sum(qty)
```

A program minden 15m boundaryn nullázza az akkumulátorokat. A `candleOpen` az adott live 15m ablak első feldolgozott `aggTrade.price` értéke.

**Adatintegritási szabály:** processzindulás vagy market WebSocket reconnect után a már folyamatban lévő, részlegesen látott 15m ablak nem használható valid VWAP-ként. Az első teljesen megfigyelt új 15m ablak már használható. Ez azért szükséges, mert a specifikáció szerint a realtime VWAP kizárólag `aggTrade` adatokból készül, így a processz indulása előtti trade-eket nem szabad kitalálni.

## Normalized CVD

Minden `aggTrade` esetén:

- `m == true` → buyer maker → agresszor SELL
- `m == false` → agresszor BUY

```text
notional = price * qty
```

Lezárt 1 perces bucket:

```text
delta = buyNotional - sellNotional
total = buyNotional + sellNotional
normalizedDelta = delta / total     # ha total > 0
```

Az utolsó `cvdLookback` lezárt bucketből:

```text
C1 = d1
C2 = d1 + d2
...
Cn = d1 + ... + dn
```

Ez a `normalizedCvdSeries`.

```python
x = numpy.linspace(-1, 1, len(cvdSeries))
slope = numpy.polyfit(x, cvdSeries, 1)[0]
a, b, c = numpy.polyfit(x, cvdSeries, 2)
curvature = 2 * a
```

Nincs magnitude threshold.

Reconnect/indulás után a részlegesen látott első 1 perces bucket nem kerül be a CVD lookbackbe. A signalhoz `cvdLookback` darab teljes, lezárt, értelmezhető bucket szükséges.

## Spread

`bookTicker` alapján:

```text
mid = (bid + ask) / 2
spreadBps = (ask - bid) / mid * 10000
```

Signal csak akkor lehet, ha a spread nem nagyobb `maxSpreadBps` értéknél, és a book adat nem régebbi `bookMaxAgeSec` értéknél.

## Outcome mérés

Signal után a rendszer továbbra sem nyit vagy zár pozíciót. Csak méri az árat.

LONG:

```text
returnPct = (currentPrice - signalPrice) / signalPrice * 100
```

SHORT:

```text
returnPct = (signalPrice - currentPrice) / signalPrice * 100
```

A `signals` dokumentumban:

- `return1m`
- `return3m`
- `return5m`
- `return10m`
- `return15m`
- `return20m`
- `MFE`
- `MAE`
- `timeToMFE`
- `timeToMAE`

A checkpoint ára az első olyan `aggTrade.price`, amelynek trade timestampje elérte az adott időpontot. `MFE` a legnagyobb direction-adjusted return, `MAE` a legkisebb direction-adjusted return a 20 perces megfigyelési ablakban. Az idők másodpercben értendők a signal óta.

20 perc után csak a mérés lesz `COMPLETED`; ez nem position exit.

Ha a processz a 20 perces mérés közben leáll/újraindul, vagy az `aggTrade` market stream megszakad, az érintett mérés `INTERRUPTED` lesz. A program nem tölti ki utólag kitalált értékekkel a hiányzó market pathot.

## MongoDB collectionök

A projekt pontosan ezeket az alkalmazás-collectionöket hozza létre és használja:

```text
config
candles
flow_1m
signals
heartbeats
```

### `config`

Első induláskor automatikusan létrejön az alábbi dokumentum:

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

A konfigurációt induláskor olvassa be a processz. Config módosítás után indítsd újra csak az alkalmazás-containert:

```bash
docker compose restart shadow-signal
```

Példa config módosításra:

```bash
docker compose exec mongo mongosh shadow_signals --quiet --eval '
db.config.updateOne(
  {_id: "strategy"},
  {$set: {xEntry: 2.0, updatedAt: new Date()}}
)'
```

### `symbolOverrides`

A globális 15m/1h timeframe megmarad közösnek. Symbolonként az alábbi stratégiai numerikus paraméterek írhatók felül:

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

Példa:

```json
{
  "symbolOverrides": {
    "BTCUSDT": {
      "xEntry": 2.0,
      "maxSpreadBps": 5
    }
  }
}
```

### `candles`

A rendszer által ténylegesen használt lezárt 15m/1h candle-eket tárolja. Egyedi kulcs: `symbol + timeframe + openTime`.

### `flow_1m`

Lezárt 1 perces CVD bucketek: `buyNotional`, `sellNotional`, `delta`, `total`, `normalizedDelta`, trade count és az, hogy teljesen megfigyelt bucket volt-e.

### `signals`

Signal pillanat, ár, entry EMA/ATR/band, 1h guideline, VWAP/CVD/spread validáció, config snapshot és outcome mezők.

### `heartbeats`

`heartbeatSec` időközönként a processz állapota és symbolonként a legfontosabb adatfrissességi/ready információk.

## `.env`

Másold le:

```bash
cp .env.example .env
```

Csak ezek vannak benne:

```dotenv
MONGO_URI=mongodb://user:pass@host:27017/shadow_signals?authSource=admin
TELEGRAM_BOT_TOKEN=replace_me
TELEGRAM_CHAT_ID=replace_me
LOG_LEVEL=INFO
```

Kulso (nem compose-beli) MongoDB eseten az `authSource` general `admin`, mert a user
ott van letrehozva. Ha kimarad, a driver az URI utvonalan levo adatbazishoz probal
autentikalni, es `AuthenticationFailed` (code 18) hibaval all le az indulas.
A jelszo specialis karaktereit URL-kodolni kell (`+` -> `%2B`, `@` -> `%40`).

Nincs Binance API key, mert a projekt kizárólag publikus market adatot olvas és nem küld ordert.

## Indítás Dockerrel

```bash
docker compose up --build -d
```

Log:

```bash
docker compose logs -f shadow-signal
```

Leállítás:

```bash
docker compose down
```

A Mongo volume megmarad. Ha **szándékosan minden Mongo adatot is törölni** akarsz:

```bash
docker compose down -v
```

## Tesztek

Lokálisan Python 3.12+ környezetben:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

A tesztek ellenőrzik többek között:

- EMA SMA seed + rekurzió
- Wilder ATR
- CVD slope/curvature előjelek
- VWAP strict feltételek
- spread bps
- LONG/SHORT directional return
- `aggTrade.m` BUY/SELL agresszor mapping

## Binance adatkapcsolat

A projekt 2026-os USDⓈ-M Futures publikus market stream struktúrához készült:

- `aggTrade` + kline: `wss://fstream.binance.com/market/stream`
- `bookTicker`: `wss://fstream.binance.com/public/stream`
- történeti kline bootstrap: `https://fapi.binance.com/fapi/v1/klines`

Az eseményekben, ha jelen van az `st` mező, a projekt csak `st == 1` (USDⓈ-M) adatot dolgoz fel.
