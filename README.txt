IPHONE LIVE SIGNAL BOT V2

This is an iPhone-friendly web app. It does not place trades.

Required:
- Python 3.10+
- Twelve Data API key for live FX candles
- A public HTTPS host if you want to access it from Safari outside your own computer

Run locally:
pip install -r requirements.txt
copy .env.example .env
python app.py
Open http://127.0.0.1:5000

For a real iPhone-accessible deployment, put this Flask app behind HTTPS on a hosting provider.
TradingView webhooks require a public HTTPS endpoint; TradingView says only ports 80/443 are accepted and requests can be cancelled after 3 seconds.

The engine is intentionally strict and can output NO TRADE frequently.
No 99.999% accuracy guarantee is possible.
