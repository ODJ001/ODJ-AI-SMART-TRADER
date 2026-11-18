
import os
import time
import requests

API_KEY = os.getenv("TWELVEDATA_API_KEY")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOLS = [
    "EUR/USD", "GBP/USD", "USD/JPY",
    "AUD/USD", "USD/CAD", "XAU/USD"
]

INTERVALS = ["1h", "15min"]
CANDLE_COUNT = 100

def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = requests.post(
        url,
        data={"chat_id": CHAT_ID, "text": message},
        timeout=20
    )
    response.raise_for_status()

def fetch_candles(symbol, interval):
    response = requests.get(
        "https://api.twelvedata.com/time_series",
        params={
            "symbol": symbol,
            "interval": interval,
            "outputsize": CANDLE_COUNT,
            "apikey": API_KEY
        },
        timeout=30
    )

    response.raise_for_status()
    return response.json()

def main():
    if not API_KEY or not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("A required GitHub secret is missing.")

    results = [
        "ODJ AI SMART TRADER",
        "HISTORICAL MARKET DATA TEST",
        "Mode: DEMO"
    ]

    successful = 0
    failed = 0

    for symbol in SYMBOLS:
        for interval in INTERVALS:
            try:
                data = fetch_candles(symbol, interval)

                if "values" not in data or not data["values"]:
                    error = data.get("message", "No candle data returned")
                    results.append(
                        f"\n{symbol} | {interval}: FAILED - {error}"
                    )
                    failed += 1
                else:
                    candles = data["values"]
                    latest = candles[0]

                    results.append(
                        f"\n{symbol} | {interval}: SUCCESS"
                        f"\nCandles: {len(candles)}"
                        f"\nTime: {latest['datetime']}"
                        f"\nOpen: {latest['open']}"
                        f"\nHigh: {latest['high']}"
                        f"\nLow: {latest['low']}"
                        f"\nClose: {latest['close']}"
                    )
                    successful += 1

            except requests.RequestException as error:
                results.append(
                    f"\n{symbol} | {interval}: REQUEST FAILED"
                    f"\nHTTP/network error: {type(error).__name__}"
                )
                failed += 1

            except Exception as error:
                results.append(
                    f"\n{symbol} | {interval}: ERROR"
                    f"\n{type(error).__name__}"
                )
                failed += 1

            time.sleep(10)

    results.append(
        f"\n\nSUMMARY"
        f"\nSuccessful: {successful}/12"
        f"\nFailed: {failed}/12"
        f"\nNo trades were placed."
    )

    send_telegram("\n".join(results))

    print(f"Market-data test finished: {successful}/12 successful.")

if __name__ == "__main__":
    main()
