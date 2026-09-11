from flask import Flask, jsonify, request, render_template
import os, time, statistics, requests
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
    "reasons": ["Waiting for live market data"],
    "updated": 0
}

def f(v):
    return float(v)

def sma(values, n):
    if len(values) < n:
        return None
    return sum(f(v) for v in values[-n:]) / float(n)

def average_range(candles):
    vals = [f(x["high"]) - f(x["low"]) for x in candles]
    return statistics.mean(vals) if vals else 0.0

def analyze(c):
    if len(c) < 50:
        return "NO TRADE", 0, ["Not enough candles"]

    close = [f(x["close"]) for x in c]
    e9 = sma(close, 9)
    e21 = sma(close, 21)
    if e9 is None or e21 is None:
        return "NO TRADE", 0, ["Not enough candles"]

    last = c[-1]
    prev = c[-2]

    o = f(last["open"])
    h = f(last["high"])
    l = f(last["low"])
    cl = f(last["close"])

    prev_close = f(prev["close"])
    rng = max(h - l, 1e-9)
    body = abs(cl - o)
    body_ratio = body / rng

    recent = c[-31:-1]
    support = min(f(x["low"]) for x in recent)
    resistance = max(f(x["high"]) for x in recent)

    ar = average_range(c[-14:])
    level_distance = max(ar * 0.50, 1e-9)

    near_support = abs(cl - support) <= level_distance
    near_resistance = abs(cl - resistance) <= level_distance

    upper_wick = h - max(o, cl)
    lower_wick = min(o, cl) - l

    bullish_rejection = (
        cl > o and
        lower_wick >= max(body * 1.25, rng * 0.28)
    )
    bearish_rejection = (
        cl < o and
        upper_wick >= max(body * 1.25, rng * 0.28)
    )

    # Momentum must agree with the intended direction.
    c1 = f(c[-3]["close"])
    c2 = f(c[-2]["close"])
    c3 = f(c[-1]["close"])
    bullish_momentum = c3 > c2 > c1
    bearish_momentum = c3 < c2 < c1

    bullish_trend = e9 > e21
    bearish_trend = e9 < e21

    # A reversal at a level is allowed only with rejection.
    # A continuation setup needs trend + momentum + good candle.
    buy_score = 0
    sell_score = 0
    buy_reasons = []
    sell_reasons = []

    if bullish_trend:
        buy_score += 30
        buy_reasons.append("Bullish trend")
    if bearish_trend:
        sell_score += 30
        sell_reasons.append("Bearish trend")

    if near_support:
        buy_score += 25
        buy_reasons.append("Near support")
    if near_resistance:
        sell_score += 25
        sell_reasons.append("Near resistance")

    if bullish_rejection:
        buy_score += 25
        buy_reasons.append("Bullish rejection")
    if bearish_rejection:
        sell_score += 25
        sell_reasons.append("Bearish rejection")

    if bullish_momentum:
        buy_score += 20
        buy_reasons.append("Bullish momentum")
    if bearish_momentum:
        sell_score += 20
        sell_reasons.append("Bearish momentum")

    if body_ratio >= 0.45:
        if cl > o:
            buy_score += 10
            buy_reasons.append("Strong bullish candle")
        elif cl < o:
            sell_score += 10
            sell_reasons.append("Strong bearish candle")

    # High-quality rules:
    # 1) Trend + level + rejection + momentum, OR
    # 2) Trend + momentum + strong candle away from a conflicting level.
    buy_level_setup = near_support and bullish_rejection
    sell_level_setup = near_resistance and bearish_rejection

    buy_continuation = (
        bullish_trend and bullish_momentum and
        body_ratio >= 0.45 and not near_resistance
    )
    sell_continuation = (
        bearish_trend and bearish_momentum and
        body_ratio >= 0.45 and not near_support
    )

    buy_valid = (
        bullish_trend and bullish_momentum and
        (buy_level_setup or buy_continuation) and buy_score >= 70
    )
    sell_valid = (
        bearish_trend and bearish_momentum and
        (sell_level_setup or sell_continuation) and sell_score >= 70
    )

    if buy_valid and buy_score > sell_score:
        return "BUY", buy_score, buy_reasons

    if sell_valid and sell_score > buy_score:
        return "SELL", sell_score, sell_reasons

    reasons = []
    if bullish_trend:
        reasons.append("Bullish trend")
    elif bearish_trend:
        reasons.append("Bearish trend")
    else:
        reasons.append("Trend unclear")

    if near_support:
        reasons.append("Near support")
    elif near_resistance:
        reasons.append("Near resistance")
    else:
        reasons.append("Waiting for key level")

    if bullish_rejection:
        reasons.append("Bullish rejection")
    elif bearish_rejection:
        reasons.append("Bearish rejection")
    else:
        reasons.append("No strong rejection")

    if bullish_momentum:
        reasons.append("Bullish momentum")
    elif bearish_momentum:
        reasons.append("Bearish momentum")
    else:
        reasons.append("Momentum mixed")

    if body_ratio >= 0.45:
        reasons.append("Strong candle")
    else:
        reasons.append("Weak candle")

    return "NO TRADE", max(buy_score, sell_score), reasons

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

    try:
        url = "https://api.twelvedata.com/time_series"
        params = {
            "symbol": symbol,
            "interval": interval,
            "outputsize": 100,
            "apikey": KEY
        }
        data = requests.get(url, params=params, timeout=8).json()

        if "values" not in data:
            return jsonify({"error": data.get("message", "Data unavailable")})

        values = list(reversed(data["values"]))
        candles = [{
            "open": f(x["open"]),
            "high": f(x["high"]),
            "low": f(x["low"]),
            "close": f(x["close"])
        } for x in values]

        signal, score, reasons = analyze(candles)

        latest = {
            "signal": signal,
            "score": score,
            "symbol": symbol,
            "timeframe": interval,
            "reasons": reasons,
            "updated": time.time(),
            "price": candles[-1]["close"]
        }
        return jsonify(latest)

    except Exception as e:
        return jsonify({"error": str(e)})

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
