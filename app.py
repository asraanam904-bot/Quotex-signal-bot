from flask import Flask, jsonify, request, render_template
import os, time, statistics, requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

KEY = os.getenv("TWELVE_DATA_API_KEY", "")
TV_TOKEN = os.getenv("TV_WEBHOOK_TOKEN", "CHANGE_ME")

latest = {
    "signal": "NO TRADE",
    "score": 0,
    "symbol": "EUR/USD",
    "timeframe": "5min",
    "setup": "Waiting",
    "reasons": ["Waiting for live market data"],
    "updated": 0
}
last_alert = ""

# Free-plan friendly market-data cache/budget.
# Twelve Data /time_series costs 1 credit per symbol; batching reduces HTTP
# overhead but does NOT reduce symbol credits. We therefore rotate pairs and
# cache each pair until its next refresh window.
CANDLE_CACHE = {}
DAILY_CALLS = 0
DAILY_CALLS_DATE = ""

# Keep the free 800/day plan below its daily limit. These limits are deliberately
# conservative and leave headroom for occasional manual /api/live requests.
DAILY_BUDGET = 700
SCAN_RESULT_CACHE = {}

REFRESH_EVERY = {
    "1min": 14 * 60,   # one pair refresh roughly every 14 min
    "5min": 20 * 60,   # each pair refreshed roughly every 20 min
    "15min": 60 * 60,  # each pair refreshed roughly every hour
    "1h": 4 * 60 * 60
}

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
        f"Daily data-request safety limit reached ({DAILY_CALLS}/{DAILY_BUDGET}). "
        "Cached data will be used until the next UTC day. "
        "This protects the Twelve Data free 800/day quota."
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

    o = f(last["open"]); h = f(last["high"])
    l = f(last["low"]); cl = f(last["close"])
    po = f(prev["open"]); ph = f(prev["high"])
    pl = f(prev["low"]); pc = f(prev["close"])
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

    # 1) SUPPORT / RESISTANCE PRICE-ACTION REACTION
    bullish_rejection = (
        cl > o and lower >= max(body * 1.20, rng * 0.28)
    )
    bearish_rejection = (
        cl < o and upper >= max(body * 1.20, rng * 0.28)
    )

    sr_buy = near_support and bullish_rejection
    sr_sell = near_resistance and bearish_rejection

    # 2) CANDLE STRUCTURE: engulfing / pin-bar style
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

    # 3) FAKE BREAKOUT: wick through a recent level, then close back inside.
    pre_recent = c[-21:-2]
    prior_res = max(f(x["high"]) for x in pre_recent)
    prior_sup = min(f(x["low"]) for x in pre_recent)

    fake_break_down = (
        pl < prior_sup and pc > prior_sup
    )
    fake_break_up = (
        ph > prior_res and pc < prior_res
    )

    fake_buy = fake_break_down and cl > pc
    fake_sell = fake_break_up and cl < pc

    # 4) "TRAP" HEURISTIC:
    # A failed break followed by reversal/momentum on the current candle.
    trap_buy = fake_break_down and cl > o and cl > pc and lower > body
    trap_sell = fake_break_up and cl < o and cl < pc and upper > body

    # 5) PRICE ACTION / MOMENTUM
    bullish_momentum = cl > pc > p2c
    bearish_momentum = cl < pc < p2c

    pa_buy = bullish_momentum and body_ratio >= 0.55 and cl > o
    pa_sell = bearish_momentum and body_ratio >= 0.55 and cl < o

    # Any one clean setup can trigger a directional signal.
    # Priority: trap/fake-breakout -> S/R reaction -> candle structure -> price action.
    candidates = []

    if trap_buy:
        candidates.append(("BUY", 95, "Trap / failed-break reversal", ["Failed downside break", "Bullish reversal"]))
    if trap_sell:
        candidates.append(("SELL", 95, "Trap / failed-break reversal", ["Failed upside break", "Bearish reversal"]))

    if fake_buy:
        candidates.append(("BUY", 92, "Fake breakout", ["Break below support failed", "Price closed back up"]))
    if fake_sell:
        candidates.append(("SELL", 92, "Fake breakout", ["Break above resistance failed", "Price closed back down"]))

    if sr_buy:
        candidates.append(("BUY", 90, "Support + rejection", ["Near support", "Bullish rejection"]))
    if sr_sell:
        candidates.append(("SELL", 90, "Resistance + rejection", ["Near resistance", "Bearish rejection"]))

    if candle_buy:
        setup = "Bullish candle structure"
        why = ["Bullish engulfing"] if bullish_engulf else ["Bullish pin-bar structure"]
        candidates.append(("BUY", 88, setup, why))
    if candle_sell:
        setup = "Bearish candle structure"
        why = ["Bearish engulfing"] if bearish_engulf else ["Bearish pin-bar structure"]
        candidates.append(("SELL", 88, setup, why))

    if pa_buy:
        candidates.append(("BUY", 86, "Price action momentum", ["3-candle bullish momentum", "Strong bullish candle"]))
    if pa_sell:
        candidates.append(("SELL", 86, "Price action momentum", ["3-candle bearish momentum", "Strong bearish candle"]))

    # If opposite directions appear on the same candle, do not force a trade.
    directions = {x[0] for x in candidates}
    if len(directions) > 1:
        return "NO TRADE", 0, "Conflict", ["Opposite setups detected", "Waiting for cleaner confirmation"]

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
    changes = [closes[i] - closes[i-1] for i in range(len(closes)-14, len(closes))]
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

    # A doji disables the newly added setup checks for this candle. The
    # original strategy is still evaluated unchanged by analyze().
    last_body_ratio = bodies[-1] / ranges[-1]
    if last_body_ratio < 0.12:
        return None

    closes = cl
    rsi = _rsi14(closes)
    e9 = sma(closes, 9)
    e21 = sma(closes, 21)
    recent_high = max(h[-21:-1])
    recent_low = min(l[-21:-1])
    avg_recent_range = statistics.mean(ranges[-14:])
    tol = max(avg_recent_range * 0.15, 1e-12)

    bullish_trend = e9 > e21
    bearish_trend = e9 < e21
    # Approximate market structure using recent 10-candle halves.
    older_high = max(h[-20:-10]); newer_high = max(h[-10:])
    older_low = min(l[-20:-10]); newer_low = min(l[-10:])
    structure_up = newer_high > older_high and newer_low > older_low
    structure_down = newer_high < older_high and newer_low < older_low

    # Liquidity sweep: wick crosses a recent level, close returns inside.
    sweep_low = l[-1] < recent_low and cl[-1] > recent_low and cl[-1] > o[-1]
    sweep_high = h[-1] > recent_high and cl[-1] < recent_high and cl[-1] < o[-1]

    # Breakout then retest: breakout happened on previous candle; latest candle
    # retested the broken level and closed back in breakout direction.
    prior_resistance = max(h[-22:-2])
    prior_support = min(l[-22:-2])
    breakout_up_prev = cl[-2] > prior_resistance and cl[-2] > o[-2]
    breakout_down_prev = cl[-2] < prior_support and cl[-2] < o[-2]
    retest_buy = breakout_up_prev and l[-1] <= prior_resistance + tol and cl[-1] > prior_resistance and cl[-1] > o[-1]
    retest_sell = breakout_down_prev and h[-1] >= prior_support - tol and cl[-1] < prior_support and cl[-1] < o[-1]

    extra = []
    # Liquidity sweep + directional candle confirmation + structure/momentum.
    if sweep_low and (structure_up or bullish_trend) and (rsi is None or rsi >= 45):
        extra.append(("BUY", 94, "Liquidity sweep + bullish structure", ["Recent low swept and price closed back above it", "Bullish structure/trend confirmation", f"RSI(14): {rsi:.1f}" if rsi is not None else "RSI unavailable"]))
    if sweep_high and (structure_down or bearish_trend) and (rsi is None or rsi <= 55):
        extra.append(("SELL", 94, "Liquidity sweep + bearish structure", ["Recent high swept and price closed back below it", "Bearish structure/trend confirmation", f"RSI(14): {rsi:.1f}" if rsi is not None else "RSI unavailable"]))
    if retest_buy and (rsi is None or rsi > 50):
        extra.append(("BUY", 93, "Breakout + retest", ["Previous candle broke resistance", "Latest candle retested and held the broken level", f"RSI(14): {rsi:.1f}" if rsi is not None else "RSI unavailable"]))
    if retest_sell and (rsi is None or rsi < 50):
        extra.append(("SELL", 93, "Breakout + retest", ["Previous candle broke support", "Latest candle retested and held below the broken level", f"RSI(14): {rsi:.1f}" if rsi is not None else "RSI unavailable"]))

    # User's added five-candle pattern. Big/small are judged RELATIVELY to
    # recent candle bodies (not a fixed multiplier): first body must rank in
    # the upper quartile of recent bodies; second body is smaller and below
    # the recent median. All five pattern candles must be non-doji.
    if len(c) >= 25:
        # Pattern sequence ends at the latest closed candle:
        # big candle, smaller opposite candle, immediate breakout candle,
        # then two opposite-color candles; entry is next candle.
        i = len(c) - 5
        pattern_bodies = bodies[-5:]
        recent_body_window = sorted(bodies[-25:-5])
        median_body = statistics.median(recent_body_window) if recent_body_window else 0
        upper_quartile_rank = sorted(recent_body_window)[int(0.75 * (len(recent_body_window)-1))] if recent_body_window else 0
        no_doji = all(pattern_bodies[j] / ranges[-5+j] >= 0.12 for j in range(5))
        big_enough = pattern_bodies[0] >= upper_quartile_rank and pattern_bodies[0] > 0
        small_enough = pattern_bodies[1] < pattern_bodies[0] and pattern_bodies[1] <= median_body
        # BUY: big green, small red, immediate break above combined high,
        # then exactly two red candles. SELL is the mirror image.
        buy_pattern = (
            no_doji and big_enough and small_enough and
            cl[i] > o[i] and cl[i+1] < o[i+1] and
            h[i+1] <= h[i] and l[i+1] >= l[i] and
            h[i+2] > max(h[i], h[i+1]) and cl[i+2] > max(h[i], h[i+1]) and
            cl[i+3] < o[i+3] and cl[i+4] < o[i+4]
        )
        sell_pattern = (
            no_doji and big_enough and small_enough and
            cl[i] < o[i] and cl[i+1] > o[i+1] and
            h[i+1] <= h[i] and l[i+1] >= l[i] and
            l[i+2] < min(l[i], l[i+1]) and cl[i+2] < min(l[i], l[i+1]) and
            cl[i+3] > o[i+3] and cl[i+4] > o[i+4]
        )
        if buy_pattern:
            extra.append(("BUY", 96, "Big candle â small candle â immediate breakout pattern", ["Big green candle followed by smaller red candle", "Immediate breakout above combined high", "Next two candles were red", "No doji in the five-candle pattern"]))
        if sell_pattern:
            extra.append(("SELL", 96, "Big candle â small candle â immediate breakdown pattern", ["Big red candle followed by smaller green candle", "Immediate breakdown below combined low", "Next two candles were green", "No doji in the five-candle pattern"]))

    if not extra:
        return None
    directions = {x[0] for x in extra}
    if len(directions) > 1:
        return ("NO TRADE", 0, "Added setups conflict", ["New confirmation methods disagree; risk filter says wait"])
    return max(extra, key=lambda x: x[1])


def analyze(c):
    """Keep original logic; add extra confirmations and setup types on top."""
    old_signal, old_score, old_setup, old_reasons = analyze_existing(c)
    added = analyze_added_setups(c)
    if added is None:
        return old_signal, old_score, old_setup, old_reasons
    new_signal, new_score, new_setup, new_reasons = added
    if new_signal == "NO TRADE":
        # Doji/conflicting extra checks should prevent a forced new signal;
        # existing directional setup is still retained unless it is unclear.
        if old_signal in ("BUY", "SELL"):
            return "NO TRADE", 0, new_setup, new_reasons + [f"Original setup ({old_signal}) not taken because added risk filter disagreed"]
        return new_signal, new_score, new_setup, new_reasons
    if old_signal in ("BUY", "SELL") and old_signal != new_signal:
        return "NO TRADE", 0, "Signal conflict / risk check", [f"Original setup suggested {old_signal}", f"Added confirmations suggested {new_signal}", "Conflicting directions: no trade"]
    if old_signal == new_signal:
        return old_signal, max(old_score, new_score), f"{old_setup} + {new_setup}", old_reasons + new_reasons
    return new_signal, new_score, new_setup, new_reasons


FX_TZ = ZoneInfo("America/New_York")
IST = timezone(timedelta(hours=5, minutes=30))

SCAN_PAIRS = [
    "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF",
    "AUD/USD", "USD/CAD", "NZD/USD"
]

def fx_market_open(now_utc=None):
    """Approximate global spot-FX open window: Sun 17:00 ET through Fri 17:00 ET."""
    now_utc = now_utc or datetime.now(timezone.utc)
    et = now_utc.astimezone(FX_TZ)
    wd = et.weekday()  # Mon=0 ... Sun=6

    if wd == 5:  # Saturday
        return False
    if wd == 6:  # Sunday: opens 17:00 ET
        return et.time() >= datetime.strptime("17:00", "%H:%M").time()
    if wd == 4:  # Friday: closes 17:00 ET
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
    """Keep only fully closed UTC candles and reject stale feeds."""
    sec = interval_seconds(interval)
    now = datetime.now(timezone.utc)

    completed = []
    for x in values:
        try:
            dt = datetime.fromisoformat(
                x["datetime"].replace("Z", "+00:00")
            )
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt + timedelta(seconds=sec) <= now:
                completed.append({
                    "open": f(x["open"]),
                    "high": f(x["high"]),
                    "low": f(x["low"]),
                    "close": f(x["close"]),
                    "_dt": dt
                })
        except Exception:
            continue

    completed.sort(key=lambda z: z["_dt"])

    if not completed:
        return []

    # Do not generate a signal from an old/stale market feed.
    age = (now - completed[-1]["_dt"]).total_seconds()
    max_age = max(sec * 3, 15 * 60)
    if age > max_age:
        return []

    return [{k: v for k, v in x.items() if k != "_dt"} for x in completed]


def fetch_market_candles(symbol, interval, force_refresh=False):
    """Return fresh-enough closed candles while protecting the daily API quota."""
    global DAILY_CALLS

    _budget_reset_if_needed()

    cached, age = _cached_candles(symbol, interval)
    if cached is not None and not force_refresh:
        return cached

    if not _refresh_allowed():
        # A stale cache is better than spending beyond the free-plan budget, but
        # only return it if it still has enough candles to analyze.
        item = CANDLE_CACHE.get(_cache_key(symbol, interval))
        if item and len(item["candles"]) >= 40:
            return item["candles"]
        raise RuntimeError(_credit_safe_error())

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": 100,
        "timezone": "UTC",
        "apikey": KEY
    }

    response = requests.get(url, params=params, timeout=10)
    data = response.json()

    # Count attempted data calls locally so repeated automatic scans cannot
    # burn through the free daily allowance.
    DAILY_CALLS += 1

    if "values" not in data:
        raise RuntimeError(data.get("message", "Data unavailable"))

    candles = completed_candles(data["values"], interval)
    if len(candles) < 40:
        raise RuntimeError("Waiting for enough fresh, closed candles")

    CANDLE_CACHE[_cache_key(symbol, interval)] = {
        "candles": candles,
        "fetched_at": time.time()
    }

    return candles


def choose_scan_pairs(interval):
    """Automatically rotate market pairs for each newly closed candle.

    1-minute mode: one fresh pair per closed candle, rotating through all
    configured pairs. 5-minute mode: two fresh pairs per closed candle.
    The result is cached for that candle, so repeated browser requests do not
    create duplicate scans.
    """
    _budget_reset_if_needed()
    now_bucket = int(time.time() // interval_seconds(interval))

    slots = 2 if interval == "5min" else 1
    start = now_bucket % len(SCAN_PAIRS)
    return [SCAN_PAIRS[(start + i) % len(SCAN_PAIRS)] for i in range(slots)]



@app.get("/")
def home():
    return render_template("index.html")


@app.get("/api/live")
def live():
    global latest

    symbol = request.args.get("symbol", "EUR/USD").upper()
    interval = request.args.get("interval", "5min")

    if not KEY:
        return jsonify({"error": "API key missing. Add TWELVE_DATA_API_KEY to Render Environment."})

    if not fx_market_open():
        latest = {
            "signal": "NO TRADE",
            "score": 0,
            "symbol": symbol,
            "timeframe": interval,
            "setup": "REAL MARKET CLOSED",
            "reasons": ["Forex real market is closed. OTC is not scanned."],
            "updated": time.time(),
            "signal_time_ist": india_time_string(),
            "timezone": "UTC+05:30"
        }
        return jsonify(latest)

    try:
        candles = fetch_market_candles(symbol, interval)
        signal, score, setup, reasons = analyze(candles)
        price = candles[-1]["close"]

        latest = {
            "signal": signal,
            "score": score,
            "symbol": symbol,
            "timeframe": interval,
            "setup": setup,
            "reasons": reasons + ["Signal based on the latest CLOSED candle"],
            "updated": time.time(),
            "price": price,
            "signal_time_ist": india_time_string(),
            "timezone": "UTC+05:30"
        }

        return jsonify(latest)

    except Exception as e:
        return jsonify({"error": str(e)})


@app.get("/api/scan")
def scan():
    """One scan per newly closed candle; return at most ONE signal."""
    interval = request.args.get("interval", "5min")

    # Only the two supported trading timeframes are exposed in the UI.
    if interval not in ("1min", "5min"):
        interval = "5min"

    if not KEY:
        return jsonify({"error": "API key missing. Add TWELVE_DATA_API_KEY to Render Environment."})

    if not fx_market_open():
        return jsonify({
            "market": "QUOTEX_LIVE_SIGNAL_ASSISTANT",
            "market_open": False,
            "otc": False,
            "interval": interval,
            "scanned_pairs": SCAN_PAIRS,
            "signals": [],
            "pair_status": [],
            "errors": [],
            "count": 0,
            "message": "REAL MARKET CLOSED â NO SIGNALS",
            "execution": "MANUAL_QUOTEX_ONLY",
            "current_time_ist": india_time_string(),
            "timezone": "UTC+05:30"
        })

    # Current candle bucket identifies the newly forming candle.
    # The latest fully closed candle is the previous bucket.
    candle_bucket = int(time.time() // interval_seconds(interval))
    cache_key = (interval, candle_bucket)
    cached_result = SCAN_RESULT_CACHE.get(cache_key)
    if cached_result is not None:
        return jsonify(cached_result)

    active_pairs = choose_scan_pairs(interval)
    results = []
    pair_status = []
    errors = []

    # IMPORTANT: only the active pairs get fresh data. We do not use older
    # cached candles as a new signal because the user requested one signal
    # based on each newly closed 5-minute candle.
    for symbol in active_pairs:
        try:
            candles = fetch_market_candles(symbol, interval, force_refresh=True)
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
                    "reasons": reasons + [f"Based on the latest fully CLOSED {interval} candle"],
                    "price": price,
                    "updated": time.time(),
                    "signal_time_ist": india_time_string(),
                    "entry": f"NEXT {interval.upper()} CANDLE",
                    "expiry": "1 MINUTE" if interval == "1min" else "5 MINUTES",
                    "execution": "MANUAL_QUOTEX_ONLY",
                    "timezone": "UTC+05:30"
                }
                results.append(result)
                pair_status.append({
                    "symbol": symbol, "signal": signal, "score": score,
                    "setup": setup, "reasons": result["reasons"]
                })
            else:
                pair_status.append({
                    "symbol": symbol, "signal": "NO TRADE", "score": 0,
                    "setup": setup, "reasons": reasons
                })

        except Exception as e:
            msg = str(e)
            errors.append({"symbol": symbol, "error": msg})
            pair_status.append({
                "symbol": symbol, "signal": "DATA ERROR", "score": 0,
                "setup": "Unavailable", "reasons": [msg]
            })

    # One signal maximum for each newly closed candle. If two active pairs
    # qualify, choose the stronger setup.
    results.sort(key=lambda x: x.get("score", 0), reverse=True)
    top_result = results[:1]

    if not top_result and not errors:
        message = f"No qualifying setup on this newly closed {interval} candle."
    elif not top_result and errors:
        message = "Fresh data was unavailable for this candle; no signal generated."
    else:
        message = f"One strongest signal selected for the NEXT {interval} candle."

    response = {
        "market": "QUOTEX_LIVE_SIGNAL_ASSISTANT",
        "market_open": True,
        "otc": False,
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
        "signal_basis": f"LATEST FULLY CLOSED {interval} CANDLE",
        "entry": f"NEXT {interval.upper()} CANDLE",
        "expiry": "1 MINUTE" if interval == "1min" else "5 MINUTES",
        "one_signal_per_closed_candle": True,
        "candle_bucket": candle_bucket,
        "api_budget": {"local_calls_today": DAILY_CALLS, "local_daily_budget": DAILY_BUDGET},
        "note": "Only fresh pairs are used for the new candle; repeated requests for the same candle are served from cache.",
        "message": message
    }

    SCAN_RESULT_CACHE[cache_key] = response

    # Keep memory bounded.
    if len(SCAN_RESULT_CACHE) > 100:
        oldest = sorted(SCAN_RESULT_CACHE, key=lambda k: k[1])[:-50]
        for k in oldest:
            SCAN_RESULT_CACHE.pop(k, None)

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
        return jsonify({"ok": False, "error": "OTCHARTS_API_KEY missing"}), 500

    try:
        r = requests.get(
            "https://otcharts.com/v1/symbols",
            params={"venue": "quotex"},
            headers={
                "Authorization": f"Bearer {key}",
                "Accept": "application/json"
            },
            timeout=15
        )

        try:
            data = r.json()
        except ValueError:
            data = {"error": "Non-JSON response"}

        return jsonify({
            "ok": r.ok,
            "status": r.status_code,
            "data": data
        }), r.status_code

    except requests.RequestException:
        return jsonify({
            "ok": False,
            "error": "OTCharts connection failed"
        }), 502
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
