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
    "updated": 0,
}

def to_float(value):
    return float(value)

def sma(values, n):
    if len(values) < n:
        return None
    window = [to_float(v) for v in values[-n:]]
    return sum(window) / float(n)

def average_range(candles):
    ranges = [
        to_float(x["high"]) - to_float(x["low"])
        for x in candles
    ]
    return statistics.mean(ranges) if ranges else 0.0

def analyze(c):
    if len(c) < 35:
        return "NO TRADE", 0, ["Not enough candles"]

    cl = [to_float(x["close"]) for x in c]
    hi = [to_float(x["high"]) for x in c[-25:-1]]
    lo = [to_float(x["low"]) for x in c[-25:-1]]

    e9 = sma(cl, 9)
    e21 = sma(cl, 21)
    if e9 is None or e21 is None:
        return "NO TRADE", 0, ["Not enough data for trend calculation"]

    last = c[-1]
    last_high = to_float(last["high"])
    last_low = to_float(last["low"])
    last_close = to_float(last["close"])
    last_open = to_float(last["open"])

    rng = max(last_high - last_low, 1e-9)
    body = abs(last_close - last_open)

    score = 0
    reasons = []

    direction = "BUY" if e9 > e21 else "SELL"
    score += 20
    reasons.append("Trend aligned")

    if not hi or not lo:
        return "NO TRADE", score, reasons + ["Not enough level data"]

    sup = min(lo)
    res = max(hi)

    avg_rng = average_range(c[-14:])
    level_distance = 0.35 * avg_rng

    nearS = abs(last_close - sup) < level_distance
    nearR = abs(last_close - res) < level_distance

    if direction == "BUY" and nearS:
        score += 25
        reasons.append("Near support")
    elif direction == "SELL" and nearR:
        score += 25
        reasons.append("Near resistance")
    else:
        reasons.append("No clean level reaction")

    # rng is explicitly a single float, preventing float/list division errors.
    if body / rng >= 0.55:
        score += 20
        reasons.append("Strong price-action candle")
    else:
        reasons.append("Weak candle")

    if ((direction == "BUY" and cl[-1] > cl[-2]) or
            (direction == "SELL" and cl[-1] < cl[-2])):
        score += 20
        reasons.append("Momentum agrees")
    else:
        reasons.append("Momentum disagrees")

    if score >= 80:
        return direction, score, reasons
    return "NO TRADE", score, reasons

@app.get("/")
def home():
    return render_template("index.html")

@app.get("/api/live")
def live():
    global latest

    symbol = request.args.get("symbol", "EUR/USD")
    interval = request.args.get("interval", "5min")

    if not KEY:
        return jsonify({
            "error": "API key missing. Add TWELVE_DATA_API_KEY to Render Environment."
        })

    try:
        url = "https://api.twelvedata.com/time_series"
        response = requests.get(
            url,
            params={
                "symbol": symbol,
                "interval": interval,
                "outputsize": 100,
                "apikey": KEY,
            },
            timeout=8,
        )
        data = response.json()

        if "values" not in data:
            return jsonify({
                "error": data.get("message", "Data unavailable")
            })

        values = list(reversed(data["values"]))
        candles = []

        for x in values:
            candles.append({
                "open": to_float(x["open"]),
                "high": to_float(x["high"]),
                "low": to_float(x["low"]),
                "close": to_float(x["close"]),
            })

        signal, score, reasons = analyze(candles)

        latest = {
            "signal": signal,
            "score": score,
            "symbol": symbol,
            "timeframe": interval,
            "reasons": reasons,
            "updated": time.time(),
            "price": candles[-1]["close"],
        }

        return jsonify(latest)

    except Exception as e:
        return jsonify({"error": str(e)})

@app.post("/api/tradingview")
def tv():
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
