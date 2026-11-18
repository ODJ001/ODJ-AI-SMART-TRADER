
import os
import requests

API_KEY = os.getenv("TWELVEDATA_API_KEY")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOLS = [
    "EUR/USD", "GBP/USD", "USD/JPY",
    "AUD/USD", "USD/CAD", "XAU/USD"
]

def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = requests.post(
        url,
        data={"chat_id": CHAT_ID, "text": message},
        timeout=20
    )
    response.raise_for_status()

def main():
    if not API_KEY or not BOT_TOKEN or not CHAT_ID:
        print("ERROR: One or more secrets are missing.")
        return

    results = ["ODJ AI SMART TRADER - MARKET DATA TEST"]

    for symbol in SYMBOLS:
        try:
            response = requests.get(
                "https://api.twelvedata.com/time_series",
                params={
                    "symbol": symbol,
                    "interval": "1h",
                    "outputsize": 2,
                    "apikey": API_KEY
                },
                timeout=20
            )

            data = response.json()

            if "values" in data and data["values"]:
                candle = data["values"][0]
                results.append(
                    f"\n{symbol}: DATA RECEIVED"
                    f"\nTime: {candle['datetime']}"
                    f"\nOpen: {candle['open']}"
                    f"\nHigh: {candle['high']}"
                    f"\nLow: {candle['low']}"
                    f"\nClose: {candle['close']}"
                )
            else:
                results.append(
                    f"\n{symbol}: DATA ERROR - "
                    f"{data.get('message', data.get('code', 'Unknown error'))}"
                )

        except Exception as error:
            results.append(f"\n{symbol}: REQUEST FAILED - {error}")

    send_telegram("\n".join(results))

if __name__ == "__main__":
    main()
