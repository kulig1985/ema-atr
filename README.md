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
│   ├── messages.py
│   ├── models.py
│   ├── storage.py
│   ├── symbols.py
│   ├── telegram.py
│   └── validation.py
├── tests/
│   ├── test_app_smoke.py
│   ├── test_config.py
│   ├── test_indicators.py
│   ├── test_messages.py
│   ├── test_models.py
│   ├── test_storage_config.py
│   ├── test_symbols.py
│   └── test_validation.py
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

## Melyik timeframe mire valo

| | Timeframe | Szerep |
|---|---|---|
| `entryTimeframe` | 15m | **Ez dont.** Az EMA/ATR ebbol adja a belepo savot mindket iranyba (`lowerEntry` a LONG-hoz, `upperEntry` a SHORT-hoz), es a realtime VWAP ablaka is ez. |
| `exitTimeframe` | 1h | **Csak tajekoztat.** Egy szamot ad a Telegram uzenetbe es a signal dokumentum `exitGuideline.price` mezojebe. Ez a ket hely az egesz kodban, ahol elofordul. |

A CVD ettol fuggetlenul mindig fix 1 perces bucketekbol szamol.

Az "1h kilepesi iranymutato" tehat **nem zar semmit** — nincs is mit zarnia, mert a
program nem nyit poziciot. Csak annyit mond: az 1h EMA/ATR alapjan itt lenne egy
ertelmes kiszallasi szint, LONG-nal `EMA1h + xExit * ATR1h` (az ar felett), SHORT-nal
`EMA1h - xExit * ATR1h` (az ar alatt). Neked szol, ha kezzel kereskedsz utana.

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
  "telegramStatusSec": 3600,
  "symbolOverrides": {},
  "logStatusSec": 60,
  "symbolAutoPopulate": false,
  "quoteAsset": "USDT",
  "minQuoteVolume24h": 500000000,
  "maxSymbols": 5,
  "updatedAt": null
}
```

Ha egy korabbi verzio config dokumentuma mar letezik, indulaskor a hianyzo mezok
automatikusan bekerulnek a default ertekukkel; a meglevo ertekek nem valtoznak.

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

### Symbol auto-populate

`symbolAutoPopulate: true` eseten a program induláskor nem a `symbols` listat hasznalja,
hanem maga valasztja ki a legforgalmasabb szerzodeseket:

1. `GET /fapi/v1/exchangeInfo` — csak `PERPETUAL` szerzodes, `TRADING` statusz, es a
   `quoteAsset` mezo pontosan a configban megadott quote asset (default `USDT`).
   Minden mas quote asset — USDC, BTC, coin-margined — kimarad.
2. `GET /fapi/v1/ticker/24hr` — a 24 oras `quoteVolume` a quote assetben.
3. Szures `minQuoteVolume24h`-ra, rendezes csokkeno forgalom szerint, vagas `maxSymbols`-ra.

A kivalasztott lista a logba es a heartbeatbe kerul. A `config.symbols` mezot **nem**
irja felul: az kezi listakent es fallbackkent marad meg. Ha a szures ures eredmenyt ad,
vagy a Binance nem elerheto, a program a `config.symbols` listaval indul.

Minden symbol harom market streamet (aggTrade + ket kline) es egy bookTicker streamet
jelent, es minden `aggTrade` vegigfut a teljes state machine-en. A `maxSymbols` ezert
eroforras-korlat is: erdemes alacsonyan kezdeni, es a `STATUS` sor `loop_lag_max`
erteket figyelni.

### Telegram uzenetek

Ketfele uzenet megy ki.

Mindketto **magyarul** megy ki (a kod es a log angol marad).

**Signal** — minden kiirt signalnal azonnal. Nem csak az adatokat tartalmazza, hanem azt
is, hogy *miert* keletkezett: melyik sav szelet lepte at az ar, hol allt a VWAP a
nyitashoz es az arhoz kepest, mennyi a CVD slope/curvature, a spread es az adatfrissesseg.
A "Levels" blokkban a belepo sav es az 1h exit guideline a szamitasaval egyutt.

**Statusz osszefoglalo** — `telegramStatusSec` masodpercenkent (default 3600, `0` kikapcsolja).
Az elso digest az indulas utan ~30 masodperccel megy ki, hogy azonnal lasd, mukodik-e.
Tartalma: az elmult 24 ora signaljai (darabszam, LONG/SHORT bontas, atlagos 20 perces
hozam, nyeresges/veszteseges arany, legjobb es legrosszabb), symbolonkent az aktualis
state es a savtol mert tavolsag ATR-ben, valamint a stream egeszseg es a loop lag.

A `telegramStatusSec` **nem** azonos a `heartbeatSec`-kel: utobbi a Mongo `heartbeats`
collectionbe ir, es nem kuld Telegram uzenetet.

### Runtime log

`logStatusSec` masodpercenkent egy osszefoglalo sor a kapcsolatokrol, majd symbolonkent
egy allapotsor:

```text
STATUS loop_lag_max=12ms market[612.4s, 45210 msgs, 73.8/s] public[8.1s, 0 msgs, 0.0/s]
  BTCUSDT    LONG_ARMED  px=79357.3 band=[79104.52..80367.21] +0.70atr_in_band
             waiting=re-entry_cross_up_through_79104.52 vwap=ready cvd=5/5
             spread=0.01bps age(trade/book)=0.3s/0.1s meas=0
```

- `loop_lag_max` — mennyit kesett a leghosszabb 1 masodperces alvas. Tartosan magas
  ertek telitett event loopot jelez, ami WebSocket keepalive timeoutot is okozhat.
- `band` — a belepo sav, es hogy az ar hol all benne ATR-ben merve.
- `waiting` — mi kell ahhoz, hogy tovabblepjen ez a symbol. Ez mondja meg, miert nem
  tortenik semmi.
- `cvd=5/5` — hany teljes 1 perces bucket all rendelkezesre a lookbackhez.

Indulaskor egy osszefoglalo blokk kiirja a sav kepletet, a signal osszes feltetelet es
a cooldown/meres parametereket, igy a log a dokumentacio nelkul is ertelmezheto.

Sikertelen re-entry validacio egy sorban, okkal es szamokkal:

```text
BTCUSDT LONG re-entry REJECTED at 79780.1: cvd_direction (slope=-0.210000 curv=+0.030000)
```

Lehetseges okok: `vwap_not_ready`, `vwap_condition`, `cvd_not_enough_buckets`,
`cvd_direction`, `book_missing`, `spread_too_wide`, `trade_missing`, `trade_stale`,
`book_stale`.

### `candles`

A rendszer által ténylegesen használt lezárt 15m/1h candle-eket tárolja. Egyedi kulcs: `symbol + timeframe + openTime`.

### `flow_1m`

Lezárt 1 perces CVD bucketek: `buyNotional`, `sellNotional`, `delta`, `total`, `normalizedDelta`, trade count és az, hogy teljesen megfigyelt bucket volt-e.

### `signals`

Signal pillanat, ár, entry EMA/ATR/band, 1h guideline, VWAP/CVD/spread validáció, config snapshot és outcome mezők.

#### Outcome mezok es mertekegysegeik

**Minden `returnNm`, `MFE` es `MAE` mezo szazalek.** Nem tizedes tort — a `0.2575` az
**+0,2575%**.

**Az elojel mar iranyhoz van igazitva: a pozitiv ertek mindig azt jelenti, hogy a signal
iranya bejott** — LONG-nal es SHORT-nal egyarant. A program SHORT eseten nem a nyers
arvaltozast tarolja, hanem annak ellentettjet:

```text
LONG:   return = (aktualis ar - signal ar) / signal ar * 100
SHORT:  return = (signal ar - aktualis ar) / signal ar * 100
```

100-as signal arral:

| Oldal | Ar kesobb | `returnNm` | Jelentes |
|---|---|---|---|
| LONG | 101 | +1,0% | jo, az ar emelkedett |
| LONG | 99 | -1,0% | rossz, az ar esett |
| SHORT | 99 | +1,0% | jo, az ar esett |
| SHORT | 101 | -1,0% | rossz, az ar emelkedett |

Ezert lehet a ket iranyt egy atlagba osszevonni: az `MFE` mindig >= 0 (a legjobb
pillanat), az `MAE` mindig <= 0 (a legrosszabb pillanat), oldaltol fuggetlenul.

| Mezo | Mertekegyseg | Jelentes |
|---|---|---|
| `return1m` … `return20m` | % | Az ar valtozasa a signal ota 1 / 3 / 5 / 10 / 15 / 20 perccel. `return10m` = a 10. percnel mert hozam. |
| `MFE` | % | Maximum Favorable Excursion: a legnagyobb *javunkra* torteno elmozdulas a 20 perces ablakban. Pozitiv vagy 0. |
| `MAE` | % | Maximum Adverse Excursion: a legnagyobb *ellenunk* torteno elmozdulas ugyanabban az ablakban. Negativ vagy 0. |
| `timeToMFE`, `timeToMAE` | masodperc | Mennyi idovel a signal utan kovetkezett be. |
| `signalPrice` | ar | A signal pillanatanak `aggTrade` ara. |
| `validation.spreadBps` | bazispont | 1 bps = 0,01%. |
| `validation.tradeAgeSec`, `bookAgeSec` | masodperc | Az adat kora a signal pillanataban. |
| `measurementStatus` | — | `ACTIVE` (meres fut), `COMPLETED` (20 perc kesz), `INTERRUPTED` (megszakadt, nincs kitalalt ertek). |

A checkpoint ara az elso olyan `aggTrade.price`, amelynek trade timestampje elerte az
adott idopontot. Ha nem volt trade, a mezo `null` marad.

Pelda: `return20m: 0.2575` egy LONG signalnal azt jelenti, hogy 20 perccel a signal utan
az ar 0,2575%-kal volt magasabb a signal aranal. Ez **nem** nyereseg: a program nem nyit
poziciot, nincs koltseg, slippage vagy tokeattet beleszamolva.

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
