from flask import Flask, jsonify, request, render_template
import os, time, statistics, requests
from dotenv import load_dotenv
load_dotenv()
app=Flask(__name__)
KEY=os.getenv('TWELVE_DATA_API_KEY','')
TV_TOKEN=os.getenv('TV_WEBHOOK_TOKEN','CHANGE_ME')
latest={'signal':'NO TRADE','score':0,'symbol':'EUR/USD','timeframe':'5min','reasons':['Waiting for live market data'],'updated':0}
def f(v): return float(v)
def sma(values,n):
    if len(values)<n:return None
    return sum(f(v) for v in values[-n:])/float(n)
def avg_range(candles):
    ranges=[f(x['high'])-f(x['low']) for x in candles]
    return statistics.mean(ranges) if ranges else 0.0
def analyze(c):
    if len(c)<40:return 'NO TRADE',0,['Not enough candles']
    closes=[f(x['close']) for x in c]; e9=sma(closes,9); e21=sma(closes,21)
    if e9 is None or e21 is None:return 'NO TRADE',0,['Not enough candles']
    last=c[-1]; op=f(last['open']); hi=f(last['high']); lo=f(last['low']); cl=f(last['close'])
    rng=max(hi-lo,1e-9); body=abs(cl-op)
    recent=c[-31:-1]; support=min(f(x['low']) for x in recent); resistance=max(f(x['high']) for x in recent)
    ar=avg_range(c[-14:]); dist=max(0.50*ar,1e-9)
    near_s=abs(cl-support)<=dist; near_r=abs(cl-resistance)<=dist
    upper=hi-max(op,cl); lower=min(op,cl)-lo
    bull_rej=lower>=max(body*1.2,rng*0.25) and cl>op
    bear_rej=upper>=max(body*1.2,rng*0.25) and cl<op
    a=f(c[-3]['close']); b=f(c[-2]['close']); d=f(c[-1]['close'])
    bull_mom=d>b>a; bear_mom=d<b<a; good=(body/rng)>=0.40
    bull_trend=e9>e21; bear_trend=e9<e21
    buy=sell=0; br=[]; sr=[]
    if bull_trend: buy+=25; br.append('Bullish trend')
    if bear_trend: sell+=25; sr.append('Bearish trend')
    if near_s: buy+=25; br.append('Near support')
    if near_r: sell+=25; sr.append('Near resistance')
    if bull_rej: buy+=20; br.append('Bullish rejection')
    if bear_rej: sell+=20; sr.append('Bearish rejection')
    if bull_mom: buy+=20; br.append('Bullish momentum')
    if bear_mom: sell+=20; sr.append('Bearish momentum')
    if good:
        if cl>op: buy+=10; br.append('Good bullish candle')
        elif cl<op: sell+=10; sr.append('Good bearish candle')
    buy_ok=bull_trend and (near_s or bull_rej) and buy>=50
    sell_ok=bear_trend and (near_r or bear_rej) and sell>=50
    if buy_ok and buy>=sell:return 'BUY',buy,br
    if sell_ok and sell>buy:return 'SELL',sell,sr
    reasons=[]
    reasons.append('Bullish trend' if bull_trend else 'Bearish trend' if bear_trend else 'Trend unclear')
    reasons.append('Near support' if near_s else 'Near resistance' if near_r else 'Waiting for key level')
    reasons.append('Bullish rejection' if bull_rej else 'Bearish rejection' if bear_rej else 'No strong rejection')
    reasons.append('Bullish momentum' if bull_mom else 'Bearish momentum' if bear_mom else 'Momentum mixed')
    return 'NO TRADE',max(buy,sell),reasons
@app.get('/')
def home(): return render_template('index.html')
@app.get('/api/live')
def live():
    global latest
    symbol=request.args.get('symbol','EUR/USD').upper(); interval=request.args.get('interval','5min')
    if not KEY:return jsonify({'error':'API key missing. Add TWELVE_DATA_API_KEY to Render Environment.'})
    try:
        d=requests.get('https://api.twelvedata.com/time_series',params={'symbol':symbol,'interval':interval,'outputsize':100,'apikey':KEY},timeout=8).json()
        if 'values' not in d:return jsonify({'error':d.get('message','Data unavailable')})
        vals=list(reversed(d['values']))
        c=[{'open':f(x['open']),'high':f(x['high']),'low':f(x['low']),'close':f(x['close'])} for x in vals]
        sig,score,reasons=analyze(c)
        latest={'signal':sig,'score':score,'symbol':symbol,'timeframe':interval,'reasons':reasons,'updated':time.time(),'price':c[-1]['close']}
        return jsonify(latest)
    except Exception as e:return jsonify({'error':str(e)})
@app.post('/api/tradingview')
def tradingview():
    if request.headers.get('X-Bot-Token')!=TV_TOKEN:return jsonify({'error':'unauthorized'}),401
    global latest; payload=request.get_json(silent=True) or {}; latest.update(payload); latest['updated']=time.time(); return jsonify({'ok':True})
@app.get('/api/latest')
def get_latest(): return jsonify(latest)
if __name__=='__main__': app.run(host='0.0.0.0',port=5000)
