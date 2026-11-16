from flask import Flask, jsonify, request, render_template
import os, time, statistics, requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

TV_TOKEN = os.getenv("TV_WEBHOOK_TOKEN", "CHANGE_ME")

latest = {
    "signal": "NO TRADE",
    "score": 0,
    "symbol": "USDNGN_otc",
    "timeframe": "5min",
    "setup": "Waiting",
    "reasons": ["Waiting for live market data"],
    "updated": 0
}
last_alert = ""

# OTCharts candle cache and local request guard
CANDLE_CACHE = {}
DAILY_CALLS = 0
DAILY_CALLS_DATE = ""
DAILY_BUDGET = 6000
SCAN_RESULT_CACHE = {}
OTC_SYMBOL_CACHE = {"symbols": [], "fetched_at": 0}

REFRESH_EVERY = {
    "1min": 15,
    "5min": 45,
    "15min": 60,
    "1h": 120
}
OTC_SYMBOL_REFRESH_SECONDS = 300


def _budget_reset_if_needed():
    global DAILY_CALLS, DAILY_CALLS_DATE
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if DAILY_CALLS_DATE != today:
        DAILY_CALLS_DATE = today
        DAILY_CALLS = 0


def _cache_key(symbol, interval):
    return (symbol, interval)


def _cached_candles(symbol, interval):
    item = CANDLE_CACHE.get(_cache_key(symbol, interval))
    if not item:
        return None, None

    age = time.time() - item["fetched_at"]
    ttl = REFRESH_EVERY.get(interval, 20 * 60)

    if age <= ttl:
        return item["candles"], age

    return None, age


def _refresh_allowed():
    _budget_reset_if_needed()
    return DAILY_CALLS < DAILY_BUDGET


def _credit_safe_error():
    _budget_reset_if_needed()
    return (
        f"Daily data-request safety limit reached "
        f"({DAILY_CALLS}/{DAILY_BUDGET}). "
        "Cached data will be used until the next UTC day. "
        "This local safety limit helps protect the OTCharts daily request allowance."
    )


def f(v):
    return float(v)


def sma(values, n):
    if len(values) < n:
        return None
    return sum(f(v) for v in values[-n:]) / float(n)


def avg_range(candles):
    vals = [f(x["high"]) - f(x["low"]) for x in candles]
    return statistics.mean(vals) if vals else 0.0


def near(a, b, distance):
    return abs(a - b) <= distance


def analyze_existing(c):
    if len(c) < 40:
        return "NO TRADE", 0, "Waiting", ["Not enough candles"]

    closes = [f(x["close"]) for x in c]
    e9 = sma(closes, 9)
    e21 = sma(closes, 21)

    if e9 is None or e21 is None:
        return "NO TRADE", 0, "Waiting", ["Not enough candles"]

    last = c[-1]
    prev = c[-2]
    prev2 = c[-3]

    o = f(last["open"])
    h = f(last["high"])
    l = f(last["low"])
    cl = f(last["close"])

    po = f(prev["open"])
    ph = f(prev["high"])
    pl = f(prev["low"])
    pc = f(prev["close"])
    p2c = f(prev2["close"])

    rng = max(h - l, 1e-9)
    body = abs(cl - o)
    body_ratio = body / rng
    upper = h - max(o, cl)
    lower = min(o, cl) - l

    recent = c[-31:-1]
    support = min(f(x["low"]) for x in recent)
    resistance = max(f(x["high"]) for x in recent)
    ar = avg_range(c[-14:])
    level_dist = max(ar * 0.50, 1e-9)

    near_support = near(cl, support, level_dist)
    near_resistance = near(cl, resistance, level_dist)

    bullish_trend = e9 > e21
    bearish_trend = e9 < e21

    bullish_rejection = (
        cl > o and lower >= max(body * 1.20, rng * 0.28)
    )
    bearish_rejection = (
        cl < o and upper >= max(body * 1.20, rng * 0.28)
    )

    sr_buy = near_support and bullish_rejection
    sr_sell = near_resistance and bearish_rejection

    bullish_engulf = (
        pc < po and cl > o and o <= pc and cl >= po and body_ratio >= 0.45
    )
    bearish_engulf = (
        pc > po and cl < o and o >= pc and cl <= po and body_ratio >= 0.45
    )

    bullish_pin = cl > o and lower >= rng * 0.45 and body_ratio <= 0.55
    bearish_pin = cl < o and upper >= rng * 0.45 and body_ratio <= 0.55

    candle_buy = bullish_engulf or bullish_pin
    candle_sell = bearish_engulf or bearish_pin

    pre_recent = c[-21:-2]
    prior_res = max(f(x["high"]) for x in pre_recent)
    prior_sup = min(f(x["low"]) for x in pre_recent)

    fake_break_down = pl < prior_sup and pc > prior_sup
    fake_break_up = ph > prior_res and pc < prior_res

    fake_buy = fake_break_down and cl > pc
    fake_sell = fake_break_up and cl < pc

    trap_buy = fake_break_down and cl > o and cl > pc and lower > body
    trap_sell = fake_break_up and cl < o and cl < pc and upper > body

    bullish_momentum = cl > pc > p2c
    bearish_momentum = cl < pc < p2c

    pa_buy = bullish_momentum and body_ratio >= 0.55 and cl > o
    pa_sell = bearish_momentum and body_ratio >= 0.55 and cl < o

    candidates = []

    if trap_buy:
        candidates.append((
            "BUY", 95, "Trap / failed-break reversal",
            ["Failed downside break", "Bullish reversal"]
        ))

    if trap_sell:
        candidates.append((
            "SELL", 95, "Trap / failed-break reversal",
            ["Failed upside break", "Bearish reversal"]
        ))

    if fake_buy:
        candidates.append((
            "BUY", 92, "Fake breakout",
            ["Break below support failed", "Price closed back up"]
        ))

    if fake_sell:
        candidates.append((
            "SELL", 92, "Fake breakout",
            ["Break above resistance failed", "Price closed back down"]
        ))

    if sr_buy:
        candidates.append((
            "BUY", 90, "Support + rejection",
            ["Near support", "Bullish rejection"]
        ))

    if sr_sell:
        candidates.append((
            "SELL", 90, "Resistance + rejection",
            ["Near resistance", "Bearish rejection"]
        ))

    if candle_buy:
        setup = "Bullish candle structure"
        why = (
            ["Bullish engulfing"]
            if bullish_engulf else ["Bullish pin-bar structure"]
        )
        candidates.append(("BUY", 88, setup, why))

    if candle_sell:
        setup = "Bearish candle structure"
        why = (
            ["Bearish engulfing"]
            if bearish_engulf else ["Bearish pin-bar structure"]
        )
        candidates.append(("SELL", 88, setup, why))

    if pa_buy:
        candidates.append((
            "BUY", 86, "Price action momentum",
            ["3-candle bullish momentum", "Strong bullish candle"]
        ))

    if pa_sell:
        candidates.append((
            "SELL", 86, "Price action momentum",
            ["3-candle bearish momentum", "Strong bearish candle"]
        ))

    directions = {x[0] for x in candidates}

    if len(directions) > 1:
        return (
            "NO TRADE", 0, "Conflict",
            ["Opposite setups detected", "Waiting for cleaner confirmation"]
        )

    if candidates:
        best = max(candidates, key=lambda x: x[1])
        return best[0], best[1], best[2], best[3]

    reasons = []

    if bullish_trend:
        reasons.append("Bullish trend")
    elif bearish_trend:
        reasons.append("Bearish trend")
    else:
        reasons.append("Trend unclear")

    reasons.append("No qualifying setup on current candle")
    return "NO TRADE", 0, "Waiting", reasons


def _rsi14(closes):
    if len(closes) < 15:
        return None

    changes = [
        closes[i] - closes[i - 1]
        for i in range(len(closes) - 14, len(closes))
    ]

    gains = sum(max(x, 0.0) for x in changes) / 14.0
    losses = sum(max(-x, 0.0) for x in changes) / 14.0

    if losses == 0:
        return 100.0 if gains > 0 else 50.0

    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))


def analyze_added_setups(c):
    """Additional filters/setups; does not replace the original analyzer."""
    if len(c) < 40:
        return None

    o = [f(x["open"]) for x in c]
    h = [f(x["high"]) for x in c]
    l = [f(x["low"]) for x in c]
    cl = [f(x["close"]) for x in c]

    bodies = [abs(cl[i] - o[i]) for i in range(len(c))]
    ranges = [max(h[i] - l[i], 1e-12) for i in range(len(c))]

    last_body_ratio = bodies[-1] / ranges[-1]
    if last_body_ratio < 0.12:
        return None

    rsi = _rsi14(cl)
    e9 = sma(cl, 9)
    e21 = sma(cl, 21)

    recent_high = max(h[-21:-1])
    recent_low = min(l[-21:-1])
    avg_recent_range = statistics.mean(ranges[-14:])
    tol = max(avg_recent_range * 0.15, 1e-12)

    bullish_trend = e9 > e21
    bearish_trend = e9 < e21

    older_high = max(h[-20:-10])
    newer_high = max(h[-10:])
    older_low = min(l[-20:-10])
    newer_low = min(l[-10:])

    structure_up = newer_high > older_high and newer_low > older_low
    structure_down = newer_high < older_high and newer_low < older_low

    sweep_low = (
        l[-1] < recent_low and cl[-1] > recent_low and cl[-1] > o[-1]
    )
    sweep_high = (
        h[-1] > recent_high and cl[-1] < recent_high and cl[-1] < o[-1]
    )

    prior_resistance = max(h[-22:-2])
    prior_support = min(l[-22:-2])

    breakout_up_prev = cl[-2] > prior_resistance and cl[-2] > o[-2]
    breakout_down_prev = cl[-2] < prior_support and cl[-2] < o[-2]

    retest_buy = (
        breakout_up_prev and l[-1] <= prior_resistance + tol
        and cl[-1] > prior_resistance and cl[-1] > o[-1]
    )
    retest_sell = (
        breakout_down_prev and h[-1] >= prior_support - tol
        and cl[-1] < prior_support and cl[-1] < o[-1]
    )

    extra = []

    if sweep_low and (structure_up or bullish_trend) and (rsi is None or rsi >= 45):
        extra.append((
            "BUY", 94, "Liquidity sweep + bullish structure",
            [
                "Recent low swept and price closed back above it",
                "Bullish structure/trend confirmation",
                f"RSI(14): {rsi:.1f}" if rsi is not None else "RSI unavailable"
            ]
        ))

    if sweep_high and (structure_down or bearish_trend) and (rsi is None or rsi <= 55):
        extra.append((
            "SELL", 94, "Liquidity sweep + bearish structure",
            [
                "Recent high swept and price closed back below it",
                "Bearish structure/trend confirmation",
                f"RSI(14): {rsi:.1f}" if rsi is not None else "RSI unavailable"
            ]
        ))

    if retest_buy and (rsi is None or rsi > 50):
        extra.append((
            "BUY", 93, "Breakout + retest",
            [
                "Previous candle broke resistance",
                "Latest candle retested and held the broken level",
                f"RSI(14): {rsi:.1f}" if rsi is not None else "RSI unavailable"
            ]
        ))

    if retest_sell and (rsi is None or rsi < 50):
        extra.append((
            "SELL", 93, "Breakout + retest",
            [
                "Previous candle broke support",
                "Latest candle retested and held below the broken level",
                f"RSI(14): {rsi:.1f}" if rsi is not None else "RSI unavailable"
            ]
        ))

    if len(c) >= 25:
        i = len(c) - 5
        pattern_bodies = bodies[-5:]
        recent_body_window = sorted(bodies[-25:-5])

        median_body = (
            statistics.median(recent_body_window)
            if recent_body_window else 0
        )
        upper_quartile_rank = (
            sorted(recent_body_window)[int(0.75 * (len(recent_body_window) - 1))]
            if recent_body_window else 0
        )

        no_doji = all(
            pattern_bodies[j] / ranges[-5 + j] >= 0.12
            for j in range(5)
        )
        big_enough = pattern_bodies[0] >= upper_quartile_rank and pattern_bodies[0] > 0
        small_enough = (
            pattern_bodies[1] < pattern_bodies[0]
            and pattern_bodies[1] <= median_body
        )

        buy_pattern = (
            no_doji and big_enough and small_enough
            and cl[i] > o[i] and cl[i + 1] < o[i + 1]
            and h[i + 1] <= h[i] and l[i + 1] >= l[i]
            and h[i + 2] > max(h[i], h[i + 1])
            and cl[i + 2] > max(h[i], h[i + 1])
            and cl[i + 3] < o[i + 3] and cl[i + 4] < o[i + 4]
        )

        sell_pattern = (
            no_doji and big_enough and small_enough
            and cl[i] < o[i] and cl[i + 1] > o[i + 1]
            and h[i + 1] <= h[i] and l[i + 1] >= l[i]
            and l[i + 2] < min(l[i], l[i + 1])
            and cl[i + 2] < min(l[i], l[i + 1])
            and cl[i + 3] > o[i + 3] and cl[i + 4] > o[i + 4]
        )

        if buy_pattern:
            extra.append((
                "BUY", 96,
                "Big candle -> small candle -> immediate breakout pattern",
                [
                    "Big green candle followed by smaller red candle",
                    "Immediate breakout above combined high",
                    "Next two candles were red",
                    "No doji in the five-candle pattern"
                ]
            ))

        if sell_pattern:
            extra.append((
                "SELL", 96,
                "Big candle -> small candle -> immediate breakdown pattern",
                [
                    "Big red candle followed by smaller green candle",
                    "Immediate breakdown below combined low",
                    "Next two candles were green",
                    "No doji in the five-candle pattern"
                ]
            ))

    if not extra:
        return None

    directions = {x[0] for x in extra}
    if len(directions) > 1:
        return (
            "NO TRADE", 0, "Added setups conflict",
            ["New confirmation methods disagree; risk filter says wait"]
        )

    return max(extra, key=lambda x: x[1])


def analyze(c):
    """Keep original logic; add extra confirmations and setup types on top."""
    old_signal, old_score, old_setup, old_reasons = analyze_existing(c)
    added = analyze_added_setups(c)

    if added is None:
        return old_signal, old_score, old_setup, old_reasons

    new_signal, new_score, new_setup, new_reasons = added

    if new_signal == "NO TRADE":
        if old_signal in ("BUY", "SELL"):
            return (
                "NO TRADE", 0, new_setup,
                new_reasons + [
                    f"Original setup ({old_signal}) not taken because added risk filter disagreed"
                ]
            )
        return new_signal, new_score, new_setup, new_reasons

    if old_signal in ("BUY", "SELL") and old_signal != new_signal:
        return (
            "NO TRADE", 0, "Signal conflict / risk check",
            [
                f"Original setup suggested {old_signal}",
                f"Added confirmations suggested {new_signal}",
                "Conflicting directions: no trade"
            ]
        )

    if old_signal == new_signal:
        return (
            old_signal, max(old_score, new_score),
            f"{old_setup} + {new_setup}",
            old_reasons + new_reasons
        )

    return new_signal, new_score, new_setup, new_reasons
    FX_TZ = ZoneInfo("America/New_York")
IST = timezone(timedelta(hours=5, minutes=30))

# OTCharts supplies the available open Quotex OTC forex symbols.
SCAN_PAIRS = ["USDNGN_otc"]


def fx_market_open(now_utc=None):
    """Approximate global spot-FX open window."""
    now_utc = now_utc or datetime.now(timezone.utc)
    et = now_utc.astimezone(FX_TZ)
    wd = et.weekday()

    if wd == 5:
        return False
    if wd == 6:
        return et.time() >= datetime.strptime("17:00", "%H:%M").time()
    if wd == 4:
        return et.time() < datetime.strptime("17:00", "%H:%M").time()
    return True


def india_time_string(dt=None):
    dt = dt or datetime.now(timezone.utc)
    return dt.astimezone(IST).strftime("%d-%m-%Y %I:%M:%S %p")


def interval_seconds(interval):
    return {
        "1min": 60,
        "5min": 300,
        "15min": 900,
        "1h": 3600
    }.get(interval, 300)


def completed_candles(values, interval):
    """Normalize OTCharts epoch-time candles and keep only fully closed candles."""
    sec = interval_seconds(interval)
    now = datetime.now(timezone.utc)
    completed = []

    for item in values:
        try:
            raw_time = item.get("time", item.get("datetime"))
            if raw_time is None:
                continue

            if isinstance(raw_time, (int, float)) or str(raw_time).strip().isdigit():
                stamp = float(raw_time)
                if stamp > 10_000_000_000:
                    stamp /= 1000.0
                dt = datetime.fromtimestamp(stamp, tz=timezone.utc)
            else:
                dt = datetime.fromisoformat(
                    str(raw_time).replace("Z", "+00:00")
                )
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                else:
                    dt = dt.astimezone(timezone.utc)

            if dt + timedelta(seconds=sec) <= now:
                completed.append({
                    "open": f(item["open"]),
                    "high": f(item["high"]),
                    "low": f(item["low"]),
                    "close": f(item["close"]),
                    "_dt": dt
                })

        except (KeyError, TypeError, ValueError, OverflowError):
            continue

    completed.sort(key=lambda row: row["_dt"])

    if not completed:
        return []

    age = (now - completed[-1]["_dt"]).total_seconds()
    max_age = max(sec * 3, 15 * 60)

    if age > max_age:
        return []

    return [
        {key: value for key, value in row.items() if key != "_dt"}
        for row in completed
    ]


def normalize_otc_symbol(symbol):
    """Convert forex names such as EUR/USD to Quotex OTC IDs."""
    cleaned = (
        str(symbol or "")
        .strip()
        .upper()
        .replace("/", "")
        .replace(" ", "")
    )

    if cleaned.endswith("_OTC"):
        cleaned = cleaned[:-4]

    if len(cleaned) != 6 or not cleaned.isalpha():
        raise ValueError(
            "Use a six-letter forex pair, for example USDNGN or USDNGN_otc"
        )

    return cleaned + "_otc"


def get_open_otc_symbols(force_refresh=False):
    """Load open OTC forex instruments from the selected Quotex book."""
    global DAILY_CALLS

    key = os.getenv("OTCHARTS_API_KEY", "").strip()

    if not key:
        raise RuntimeError(
            "OTCHARTS_API_KEY is missing from Render Environment"
        )

    cached = OTC_SYMBOL_CACHE["symbols"]
    cache_age = time.time() - OTC_SYMBOL_CACHE["fetched_at"]

    if cached and cache_age < OTC_SYMBOL_REFRESH_SECONDS and not force_refresh:
        return cached

    if not _refresh_allowed():
        if cached:
            return cached
        raise RuntimeError(_credit_safe_error())

    response = requests.get(
        "https://otcharts.com/v1/symbols",
        params={"venue": "quotex"},
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json"
        },
        timeout=15
    )

    DAILY_CALLS += 1
    response.raise_for_status()

    payload = response.json()
    rows = payload.get("symbols", []) if isinstance(payload, dict) else []

    if isinstance(payload.get("data"), dict):
        rows = payload["data"].get("symbols", rows)

    active = []

    for row in rows:
        symbol = str(row.get("symbol", ""))

        if (
            row.get("isOtc") is True
            and row.get("open") is True
            and symbol.lower().endswith("_otc")
            and len(symbol[:-4]) == 6
            and symbol[:-4].isalpha()
        ):
            active.append(row)

    active.sort(
        key=lambda row: (row.get("payout") or 0),
        reverse=True
    )

    symbols = [row["symbol"] for row in active[:5]]

    if not symbols:
        raise RuntimeError(
            "OTCharts returned no currently open Quotex OTC forex pairs"
        )

    OTC_SYMBOL_CACHE["symbols"] = symbols
    OTC_SYMBOL_CACHE["fetched_at"] = time.time()

    return symbols


def fetch_market_candles(symbol, interval, force_refresh=False):
    """Fetch closed Quotex OTC candles from OTCharts."""
    global DAILY_CALLS

    symbol = normalize_otc_symbol(symbol)

    if interval not in ("1min", "5min"):
        raise ValueError("Only 1min and 5min intervals are supported")

    _budget_reset_if_needed()

    cached, age = _cached_candles(symbol, interval)

    if cached is not None and not force_refresh:
        return cached

    if not _refresh_allowed():
        item = CANDLE_CACHE.get(_cache_key(symbol, interval))

        if item and len(item["candles"]) >= 40:
            return item["candles"]

        raise RuntimeError(_credit_safe_error())

    key = os.getenv("OTCHARTS_API_KEY", "").strip()

    if not key:
        raise RuntimeError(
            "OTCHARTS_API_KEY is missing from Render Environment"
        )

    tf = str(interval_seconds(interval))

    response = requests.get(
        "https://otcharts.com/v1/candles",
        params={
            "venue": "quotex",
            "symbol": symbol,
            "tf": tf,
            "limit": 100
        },
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json"
        },
        timeout=15
    )

    DAILY_CALLS += 1
    response.raise_for_status()

    payload = response.json()
    candle_rows = (
        payload.get("candles", [])
        if isinstance(payload, dict) else []
    )

    if isinstance(payload.get("data"), dict):
        candle_rows = payload["data"].get("candles", candle_rows)

    if not candle_rows:
        detail = (
            payload.get("message")
            or payload.get("error")
            or "No candles returned"
        )
        raise RuntimeError(str(detail))

    candles = completed_candles(candle_rows, interval)

    if len(candles) < 40:
        raise RuntimeError(
            f"Only {len(candles)} fresh closed candles received; "
            "at least 40 are needed for analysis"
        )

    CANDLE_CACHE[_cache_key(symbol, interval)] = {
        "candles": candles,
        "fetched_at": time.time()
    }

    return candles


def choose_scan_pairs(interval, available_symbols=None):
    """Rotate through currently open OTC forex pairs."""
    symbols = available_symbols or get_open_otc_symbols()

    if not symbols:
        raise RuntimeError("No open OTC symbols are available")

    now_bucket = int(time.time() // interval_seconds(interval))
    slots = min(2 if interval == "5min" else 1, len(symbols))
    start = now_bucket % len(symbols)

    return [
        symbols[(start + offset) % len(symbols)]
        for offset in range(slots)
    ]


@app.get("/")
def home():
    return render_template("index.html")


@app.get("/api/live")
def live():
    global latest

    interval = request.args.get("interval", "5min")

    if interval not in ("1min", "5min"):
        interval = "5min"

    market = request.args.get("market", "otc").lower()

    if market not in ("otc", "real"):
        market = "otc"

    if market == "real":
        return jsonify({
            "error": (
                "Quotex non-OTC / real-market symbols have not yet "
                "been verified in the OTCharts symbols feed."
            ),
            "real_market_available": False
        }), 400

    try:
        requested_symbol = request.args.get("symbol")

        if requested_symbol:
            symbol = normalize_otc_symbol(requested_symbol)
        else:
            symbol = get_open_otc_symbols()[0]

        candles = fetch_market_candles(symbol, interval)
        signal, score, setup, reasons = analyze(candles)
        price = candles[-1]["close"]

        latest = {
            "signal": signal,
            "score": score,
            "symbol": symbol,
            "timeframe": interval,
            "setup": setup,
            "reasons": reasons + [
                "Signal based on the latest CLOSED Quotex OTC candle"
            ],
            "updated": time.time(),
            "price": price,
            "market": "QUOTEX",
            "otc": True,
            "signal_time_ist": india_time_string(),
            "timezone": "UTC+05:30"
        }

        return jsonify(latest)

    except Exception as e:
        return jsonify({
            "error": str(e),
            "market": "QUOTEX",
            "otc": True
        }), 502


@app.get("/api/scan")
def scan():
    """Scan open Quotex OTC forex pairs; return at most one signal per candle."""
    interval = request.args.get("interval", "5min")

    if interval not in ("1min", "5min"):
        interval = "5min"

    market = request.args.get("market", "otc").lower()

    if market not in ("otc", "real"):
        market = "otc"

    if market == "real":
        return jsonify({
            "market": "QUOTEX_LIVE_SIGNAL_ASSISTANT",
            "market_open": False,
            "otc": False,
            "real_market_available": False,
            "interval": interval,
            "signals": [],
            "count": 0,
            "message": (
                "Real-market instruments are not yet present in the "
                "verified OTCharts Quotex symbol list."
            ),
            "execution": "MANUAL_QUOTEX_ONLY"
        }), 400

    try:
        available_symbols = get_open_otc_symbols()
        active_pairs = choose_scan_pairs(interval, available_symbols)

    except Exception as e:
        return jsonify({
            "market": "QUOTEX_LIVE_SIGNAL_ASSISTANT",
            "market_open": False,
            "otc": True,
            "interval": interval,
            "scanned_pairs": [],
            "signals": [],
            "pair_status": [],
            "errors": [{"error": str(e)}],
            "count": 0,
            "message": (
                "OTC symbols are currently unavailable; no signal generated."
            ),
            "execution": "MANUAL_QUOTEX_ONLY",
            "current_time_ist": india_time_string(),
            "timezone": "UTC+05:30"
        }), 502

    candle_bucket = int(time.time() // interval_seconds(interval))
    cache_key = ("otc", interval, candle_bucket)

    cached_result = SCAN_RESULT_CACHE.get(cache_key)

    if cached_result is not None:
        return jsonify(cached_result)

    results = []
    pair_status = []
    errors = []

    for symbol in active_pairs:
        try:
            candles = fetch_market_candles(
                symbol, interval, force_refresh=True
            )

            signal, score, setup, reasons = analyze(candles)
            price = candles[-1]["close"]

            if signal in ("BUY", "SELL"):
                result = {
                    "signal": signal,
                    "score": score,
                    "symbol": symbol,
                    "timeframe": interval,
                    "interval": interval,
                    "setup": setup,
                    "reasons": reasons + [
                        f"Based on the latest fully CLOSED {interval} OTC candle"
                    ],
                    "price": price,
                    "updated": time.time(),
                    "signal_time_ist": india_time_string(),
                    "entry": f"NEXT {interval.upper()} CANDLE",
                    "expiry": (
                        "1 MINUTE" if interval == "1min" else "5 MINUTES"
                    ),
                    "execution": "MANUAL_QUOTEX_ONLY",
                    "market": "QUOTEX",
                    "otc": True,
                    "timezone": "UTC+05:30"
                }

                results.append(result)

                pair_status.append({
                    "symbol": symbol,
                    "signal": signal,
                    "score": score,
                    "setup": setup,
                    "reasons": result["reasons"]
                })

            else:
                pair_status.append({
                    "symbol": symbol,
                    "signal": "NO TRADE",
                    "score": 0,
                    "setup": setup,
                    "reasons": reasons
                })

        except Exception as e:
            message = str(e)

            errors.append({
                "symbol": symbol,
                "error": message
            })

            pair_status.append({
                "symbol": symbol,
                "signal": "DATA ERROR",
                "score": 0,
                "setup": "Unavailable",
                "reasons": [message]
            })
                results.sort(
        key=lambda item: item.get("score", 0),
        reverse=True
    )
    top_result = results[:1]

    if not top_result and not errors:
        message = (
            f"No qualifying setup on this newly closed {interval} OTC candle."
        )
    elif not top_result and errors:
        message = (
            "Fresh OTCharts data was unavailable for this candle; "
            "no signal generated."
        )
    else:
        message = (
            f"One strongest OTC signal selected for the NEXT {interval} candle."
        )

    response = {
        "market": "QUOTEX_LIVE_SIGNAL_ASSISTANT",
        "market_open": True,
        "otc": True,
        "interval": interval,
        "scanned_pairs": active_pairs,
        "active_pairs_this_scan": active_pairs,
        "signals": top_result,
        "all_signal_count": len(results),
        "pair_status": pair_status,
        "errors": errors,
        "count": len(top_result),
        "current_time_ist": india_time_string(),
        "timezone": "UTC+05:30",
        "display_limit": 1,
        "signal_basis": f"LATEST FULLY CLOSED {interval} OTC CANDLE",
        "entry": f"NEXT {interval.upper()} CANDLE",
        "expiry": (
            "1 MINUTE" if interval == "1min" else "5 MINUTES"
        ),
        "one_signal_per_closed_candle": True,
        "candle_bucket": candle_bucket,
        "api_budget": {
            "local_calls_today": DAILY_CALLS,
            "local_daily_budget": DAILY_BUDGET
        },
        "note": (
            "Only fresh pairs are used for the new candle; repeated "
            "requests for the same candle are served from cache."
        ),
        "message": message
    }

    SCAN_RESULT_CACHE[cache_key] = response

    if len(SCAN_RESULT_CACHE) > 100:
        oldest = sorted(
            SCAN_RESULT_CACHE,
            key=lambda key: key[2]
        )[:-50]

        for key in oldest:
            SCAN_RESULT_CACHE.pop(key, None)

    return jsonify(response)


@app.post("/api/tradingview")
def tradingview():
    if request.headers.get("X-Bot-Token") != TV_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

    global latest

    payload = request.get_json(silent=True) or {}
    latest.update(payload)
    latest["updated"] = time.time()

    return jsonify({"ok": True})


@app.get("/api/latest")
def get_latest():
    return jsonify(latest)


@app.get("/api/otcharts/test")
def otcharts_test():
    key = os.getenv("OTCHARTS_API_KEY", "").strip()

    if not key:
        return jsonify({
            "ok": False,
            "error": "OTCHARTS_API_KEY missing"
        }), 500

    try:
        response = requests.get(
            "https://otcharts.com/v1/venues",
            headers={
                "Authorization": f"Bearer {key}",
                "Accept": "application/json"
            },
            timeout=20
        )

        try:
            data = response.json()
        except ValueError:
            data = {"message": response.text[:500]}

        return jsonify({
            "ok": response.ok,
            "status": response.status_code,
            "data": data
        }), response.status_code

    except requests.RequestException as e:
        return jsonify({
            "ok": False,
            "error": type(e).__name__,
            "details": str(e)
        }), 502


@app.get("/api/otcharts/symbols")
def otcharts_symbols():
    key = os.getenv("OTCHARTS_API_KEY", "").strip()

    if not key:
        return jsonify({
            "ok": False,
            "error": "OTCHARTS_API_KEY missing"
        }), 500

    try:
        response = requests.get(
            "https://otcharts.com/v1/symbols",
            params={"venue": "quotex"},
            headers={
                "Authorization": f"Bearer {key}",
                "Accept": "application/json"
            },
            timeout=15
        )

        try:
            data = response.json()
        except ValueError:
            data = {"error": "Non-JSON response"}

        return jsonify({
            "ok": response.ok,
            "status": response.status_code,
            "data": data
        }), response.status_code

    except requests.RequestException:
        return jsonify({
            "ok": False,
            "error": "OTCharts connection failed"
        }), 502


@app.get("/api/otcharts/candles")
def otcharts_candles():
    key = os.getenv("OTCHARTS_API_KEY", "").strip()

    if not key:
        return jsonify({
            "ok": False,
            "error": "API key missing"
        }), 500

    symbol = request.args.get("symbol", "USDNGN_otc")
    tf = request.args.get("tf", "60")

    if tf not in ("60", "300"):
        return jsonify({
            "ok": False,
            "error": "Use tf=60 or tf=300"
        }), 400

    try:
        response = requests.get(
            "https://otcharts.com/v1/candles",
            params={
                "venue": "quotex",
                "symbol": symbol,
                "tf": tf,
                "limit": 5
            },
            headers={
                "Authorization": f"Bearer {key}",
                "Accept": "application/json"
            },
            timeout=15
        )

        try:
            data = response.json()
        except ValueError:
            data = {"error": "Invalid API response"}

        return jsonify({
            "ok": response.ok,
            "status": response.status_code,
            "data": data
        }), response.status_code

    except requests.RequestException:
        return jsonify({
            "ok": False,
            "error": "Connection failed"
        }), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
