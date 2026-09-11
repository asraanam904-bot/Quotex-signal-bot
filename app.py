
from flask import Flask, jsonify, request, render_template
import os, time, statistics, requests
from dotenv import load_dotenv
load_dotenv()
app=Flask(__name__)
KEY=os.getenv("TWELVE_DATA_API_KEY","")
TV_TOKEN=os.getenv("TV_WEBHOOK_TOKEN","CHANGE_ME")
latest={"signal":"NO TRADE","score":0,"symbol":"EUR/USD","timeframe":"5min","reasons":["Waiting for live market data"],"updated":0}

def sma(a,n): return sum(a[-n:])/n if len(a)>=n else None
def analyze(c):
    if len(c)<35:return "NO TRADE",0,["Not enough candles"]
    cl=[x["close"] for x in c]; hi=[x["high"] for x in c[-25:-1]]; lo=[x["low"] for x in c[-25:-1]]
    e9=sma(cl,9); e21=sma(cl,21); last=c[-1]; rng=max(last["high"]-last["low"],1e-9)
    body=abs(last["close"]-last["open"]); score=0; r=[]
    direction="BUY" if e9>e21 else "SELL"; score+=20;r.append("Trend aligned")
    sup=min(lo);res=max(hi)
    nearS=abs(last["close"]-sup)<0.35*statistics.mean([x["high"]-x["low"] for x in c[-14:]])
    nearR=abs(last["close"]-res)<0.35*statistics.mean([x["high"]-x["low"] for x in c[-14:]])
    if direction=="BUY" and nearS:score+=25;r.append("Near support")
    elif direction=="SELL" and nearR:score+=25;r.append("Near resistance")
    else:r.append("No clean level reaction")
    if body/r>=.55:score+=20;r.append("Strong price-action candle")
    else:r.append("Weak candle")
    if (direction=="BUY" and cl[-1]>cl[-2]) or (direction=="SELL" and cl[-1]<cl[-2]):score+=20;r.append("Momentum agrees")
    else:r.append("Momentum disagrees")
    if score>=80:return direction,score,r
    return "NO TRADE",score,r

@app.get("/")
def home():return render_template("index.html")

@app.get("/api/live")
def live():
    global latest
    symbol=request.args.get("symbol","EUR/USD"); interval=request.args.get("interval","5min")
    if not KEY:return jsonify({"error":"API key missing. Add TWELVE_DATA_API_KEY to .env"})
    try:
        u="https://api.twelvedata.com/time_series"
        d=requests.get(u,params={"symbol":symbol,"interval":interval,"outputsize":100,"apikey":KEY},timeout=8).json()
        if "values" not in d:return jsonify({"error":d.get("message","Data unavailable")})
        vals=list(reversed(d["values"]))
        c=[{"open":float(x["open"]),"high":float(x["high"]),"low":float(x["low"]),"close":float(x["close"])} for x in vals]
        sig,score,reasons=analyze(c)
        latest={"signal":sig,"score":score,"symbol":symbol,"timeframe":interval,"reasons":reasons,"updated":time.time(),"price":c[-1]["close"]}
        return jsonify(latest)
    except Exception as e:return jsonify({"error":str(e)})

@app.post("/api/tradingview")
def tv():
    if request.headers.get("X-Bot-Token")!=TV_TOKEN:return jsonify({"error":"unauthorized"}),401
    global latest; p=request.get_json(silent=True) or {}
    latest.update(p);latest["updated"]=time.time();return jsonify({"ok":True})

@app.get("/api/latest")
def get_latest():return jsonify(latest)

if __name__=="__main__":app.run(host="0.0.0.0",port=5000)
