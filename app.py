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


def analyze(c):
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
    """Rotate a small subset of pairs so the free plan is not exhausted."""
    _budget_reset_if_needed()
    now_bucket = int(time.time() // interval_seconds(interval))

    # Number of symbols per auto scan. 5min -> 2 symbols = about 576 requests/day.
    # 1min -> 1 symbol = about 1440 requests/day, so cap it to every other bucket.
    if interval == "1min":
        if now_bucket % 2:
            slots = 0
        else:
            slots = 1
    elif interval == "5min":
        slots = 2
    elif interval == "15min":
        slots = 2
    else:
        slots = 2

    if slots <= 0:
        return []

    start = (now_bucket * slots) % len(SCAN_PAIRS)
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
    """Scan all 7 real-market FX pairs; return max 2 signals plus reasons for every pair."""
    interval = request.args.get("interval", "5min")

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

    results = []
    pair_status = []
    errors = []

    active_pairs = choose_scan_pairs(interval)

    for symbol in SCAN_PAIRS:
        try:
            if symbol not in active_pairs:
                cached, age = _cached_candles(symbol, interval)
                if cached is not None:
                    candles = cached
                    cache_note = f"Using cached candles ({int(age)}s old) to save API credits"
                else:
                    pair_status.append({
                        "symbol": symbol, "signal": "WAIT", "score": 0,
                        "setup": "Waiting for refresh slot",
                        "reasons": ["Pair is rotated into the scanner to protect the free API quota", "No fresh request was made for this pair on this scan"]
                    })
                    continue
            else:
                candles = fetch_market_candles(symbol, interval)
                cache_note = "Fresh market candles fetched"
            signal, score, setup, reasons = analyze(candles)

            if signal in ("BUY", "SELL"):
                price = candles[-1]["close"]
                result = {
                    "signal": signal,
                    "score": score,
                    "symbol": symbol,
                    "timeframe": interval,
                    "interval": interval,
                    "setup": setup,
                    "reasons": reasons + ["Based on the latest CLOSED candle", cache_note],
                    "price": price,
                    "updated": time.time(),
                    "signal_time_ist": india_time_string(),
                    "entry": "NEXT CANDLE",
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
                    "setup": setup, "reasons": reasons + [cache_note]
                })

        except Exception as e:
            msg = str(e)
            errors.append({"symbol": symbol, "error": msg})
            pair_status.append({
                "symbol": symbol, "signal": "DATA ERROR", "score": 0,
                "setup": "Unavailable", "reasons": [msg]
            })

    results.sort(key=lambda x: x.get("score", 0), reverse=True)
    top_results = results[:2]

    return jsonify({
        "market": "QUOTEX_LIVE_SIGNAL_ASSISTANT",
        "market_open": True,
        "otc": False,
        "interval": interval,
        "scanned_pairs": SCAN_PAIRS,
        "active_pairs_this_scan": active_pairs,
        "signals": top_results,
        "all_signal_count": len(results),
        "pair_status": pair_status,
        "errors": errors,
        "count": len(top_results),
        "current_time_ist": india_time_string(),
        "timezone": "UTC+05:30",
        "display_limit": 2,
        "signal_basis": "LATEST FULLY CLOSED CANDLE",
        "entry": "NEXT CANDLE",
        "api_budget": {"local_calls_today": DAILY_CALLS, "local_daily_budget": DAILY_BUDGET},
        "note": "Pairs are rotated/cached to protect the Twelve Data free-plan daily quota."
    })


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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
