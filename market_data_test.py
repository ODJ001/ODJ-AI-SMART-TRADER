
import os
import requests

API_KEY = os.getenv("TWELVEDATA_API_KEY")

SYMBOLS = ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "USD/CAD", "XAU/USD"]

def main():
    if not API_KEY:
        print("ERROR: Twelve Data API key is missing.")
        return

    for symbol in SYMBOLS:
        print(f"\nChecking {symbol}...")

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

        if "values" in data:
            candle = data["values"][0]
            print("Latest candle:", candle)
        else:
            print("Data response:", data)

if __name__ == "__main__":
    main()
