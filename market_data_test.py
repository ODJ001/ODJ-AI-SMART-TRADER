
import os
import time
import requests

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

API_KEY = os.getenv("TWELVEDATA_API_KEY")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

LAGOS = ZoneInfo("Africa/Lagos")

SYMBOLS = [
    "EUR/USD", "GBP/USD", "USD/JPY",
    "AUD/USD", "USD/CAD", "XAU/USD"
]

INTERVALS = {
    "1h": timedelta(hours=1),
    "15min": timedelta(minutes=15)
}

CANDLE_COUNT = 100


def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    response = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": message
        },
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
            "timezone": "Africa/Lagos",
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
        "CANDLE TIME VERIFICATION",
        "Timezone: Africa/Lagos (WAT)",
        f"Script time: {datetime.now(LAGOS).strftime('%Y-%m-%d %H:%M:%S')}",
        "Mode: DEMO"
    ]

    successful = 0
    failed = 0

    for symbol in SYMBOLS:
        for interval, duration in INTERVALS.items():

            try:
                data = fetch_candles(symbol, interval)

                if "values" not in data or not data["values"]:
                    error = data.get(
                        "message",
                        "No candle data returned"
                    )

                    results.append(
                        f"\n{symbol} | {interval}: FAILED"
                        f"\nReason: {error}"
                    )

                    failed += 1

                else:
                    candles = data["values"]
                    latest = candles[0]

                    candle_start = datetime.strptime(
                        latest["datetime"],
                        "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=LAGOS)

                    expected_close = candle_start + duration

                    current_time = datetime.now(LAGOS)

                    if expected_close <= current_time:
                        status = "TIME-ELAPSED: LIKELY CLOSED"
                    else:
                        status = "POSSIBLY STILL FORMING"

                    meta = data.get("meta", {})

                    results.append(
                        f"\n{symbol} | {interval}: SUCCESS"
                        f"\nAPI symbol: {meta.get('symbol', 'Not provided')}"
                        f"\nAPI interval: {meta.get('interval', 'Not provided')}"
                        f"\nCandles: {len(candles)}"
                        f"\nCandle start: {candle_start.strftime('%Y-%m-%d %H:%M:%S')}"
                        f"\nExpected candle end: {expected_close.strftime('%Y-%m-%d %H:%M:%S')}"
                        f"\nStatus: {status}"
                        f"\nOpen: {latest['open']}"
                        f"\nHigh: {latest['high']}"
                        f"\nLow: {latest['low']}"
                        f"\nClose: {latest['close']}"
                    )

                    successful += 1

            except requests.RequestException as error:
                results.append(
                    f"\n{symbol} | {interval}: REQUEST FAILED"
                    f"\nError: {type(error).__name__}"
                )
                failed += 1

            except Exception as error:
                results.append(
                    f"\n{symbol} | {interval}: ERROR"
                    f"\nError: {type(error).__name__}"
                )
                failed += 1

            time.sleep(10)

    results.append(
        f"\n\nSUMMARY"
        f"\nSuccessful: {successful}/12"
        f"\nFailed: {failed}/12"
        f"\nNo trades were placed."
        f"\nNote: Time-elapsed is not independent proof of candle completion."
    )

    send_telegram("\n".join(results))

    print(
        f"Candle verification finished: "
        f"{successful}/12 successful."
    )


if __name__ == "__main__":
    main()
