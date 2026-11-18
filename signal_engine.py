
import os
import time
import requests
from datetime import datetime, timezone

API_KEY = os.getenv("TWELVE_DATA_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BASE_URL = "https://api.twelvedata.com"

# Start with four instruments to control API usage.
SYMBOLS = ["EUR/USD", "GBP/USD", "USD/JPY", "XAU/USD"]

CANDLE_LIMIT = 100
REQUEST_DELAY = 8


def get_candles(symbol, interval):
    if not API_KEY:
        raise RuntimeError("TWELVE_DATA_API_KEY is missing")

    response = requests.get(
        f"{BASE_URL}/time_series",
        params={
            "symbol": symbol,
            "interval": interval,
            "outputsize": CANDLE_LIMIT,
            "apikey": API_KEY,
            "timezone": "UTC"
        },
        timeout=30
    )

    response.raise_for_status()
    data = response.json()

    if data.get("status") == "error" or "values" not in data:
        raise RuntimeError(data.get("message", "Invalid market data"))

    candles = []

    for item in reversed(data["values"]):
        candles.append({
            "datetime": item["datetime"],
            "open": float(item["open"]),
            "high": float(item["high"]),
            "low": float(item["low"]),
            "close": float(item["close"])
        })

    if len(candles) < 20:
        raise RuntimeError("Insufficient candle history")

    # Exclude the newest candle because it may still be forming.
    return candles[:-1]


def find_swings(candles, left=2, right=2):
    highs = []
    lows = []

    for i in range(left, len(candles) - right):
        current = candles[i]

        left_candles = candles[i-left:i]
        right_candles = candles[i+1:i+right+1]

        if all(
            current["high"] > c["high"]
            for c in left_candles + right_candles
        ):
            highs.append({
                "index": i,
                "price": current["high"]
            })

        if all(
            current["low"] < c["low"]
            for c in left_candles + right_candles
        ):
            lows.append({
                "index": i,
                "price": current["low"]
            })

    return highs, lows


def analyse_structure(candles):
    highs, lows = find_swings(candles)

    result = {
        "direction": "UNCLEAR",
        "bos": "NONE",
        "choch": "NONE",
        "sweep": "NONE",
        "swing_high": None,
        "swing_low": None
    }

    if len(candles) < 10 or not highs or not lows:
        return result

    latest = candles[-1]
    previous = candles[-2]

    swing_high = highs[-1]["price"]
    swing_low = lows[-1]["price"]

    result["swing_high"] = swing_high
    result["swing_low"] = swing_low

    previous_trend = "UNCLEAR"

    if len(highs) >= 2 and len(lows) >= 2:
        higher_high = highs[-1]["price"] > highs[-2]["price"]
        higher_low = lows[-1]["price"] > lows[-2]["price"]

        lower_high = highs[-1]["price"] < highs[-2]["price"]
        lower_low = lows[-1]["price"] < lows[-2]["price"]

        if higher_high and higher_low:
            previous_trend = "BULLISH"

        elif lower_high and lower_low:
            previous_trend = "BEARISH"

    result["direction"] = (
        previous_trend if previous_trend != "UNCLEAR"
        else "RANGING"
    )

    bullish_break = (
        previous["close"] <= swing_high
        and latest["close"] > swing_high
    )

    bearish_break = (
        previous["close"] >= swing_low
        and latest["close"] < swing_low
    )

    if bullish_break:
        result["direction"] = "BULLISH"

        if previous_trend == "BEARISH":
            result["choch"] = "BULLISH"
        else:
            result["bos"] = "BULLISH"

    elif bearish_break:
        result["direction"] = "BEARISH"

        if previous_trend == "BULLISH":
            result["choch"] = "BEARISH"
        else:
            result["bos"] = "BEARISH"

    if (
        latest["low"] < swing_low
        and latest["close"] > swing_low
    ):
        result["sweep"] = "SELL-SIDE LIQUIDITY"

    elif (
        latest["high"] > swing_high
        and latest["close"] < swing_high
    ):
        result["sweep"] = "BUY-SIDE LIQUIDITY"

    return result


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Telegram credentials are missing")

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
    )

    response = requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message
        },
        timeout=30
    )

    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError("Telegram rejected the message")


def analyse_pair(symbol):
    # Higher timeframe confirmation.
    h1 = get_candles(symbol, "1h")
    time.sleep(REQUEST_DELAY)

    # Entry timeframe analysis.
    m15 = get_candles(symbol, "15min")

    h1_result = analyse_structure(h1)
    m15_result = analyse_structure(m15)

    direction = m15_result["direction"]
    higher_direction = h1_result["direction"]

    confirmed = (
        direction in ("BULLISH", "BEARISH")
        and direction == higher_direction
        and (
            m15_result["bos"] != "NONE"
            or m15_result["choch"] != "NONE"
            or m15_result["sweep"] != "NONE"
        )
    )

    if confirmed:
        status = "POTENTIAL SETUP"
    else:
        status = "NO CONFIRMED SETUP"

    return (
        f"INSTRUMENT: {symbol}\n"
        f"STATUS: {status}\n"
        f"H1 DIRECTION: {higher_direction}\n"
        f"M15 DIRECTION: {direction}\n"
        f"BOS: {m15_result['bos']}\n"
        f"CHOCH: {m15_result['choch']}\n"
        f"LIQUIDITY: {m15_result['sweep']}\n"
        f"SWING HIGH: {m15_result['swing_high']}\n"
        f"SWING LOW: {m15_result['swing_low']}\n"
        f"LAST CLOSED PRICE: {m15[-1]['close']}\n"
        f"TIME: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )


def main():
    print("ODJ AI SMART TRADER STARTED", flush=True)

    report = [
        "ODJ AI SMART TRADER",
        "MARKET STRUCTURE REPORT",
        "------------------------"
    ]

    for symbol in SYMBOLS:
        try:
            print(f"Analysing {symbol}...", flush=True)
            result = analyse_pair(symbol)
            report.append(result)
            print(f"Completed {symbol}", flush=True)

        except Exception as error:
            message = f"{symbol}: ANALYSIS ERROR - {error}"
            report.append(message)
            print(message, flush=True)

    final_report = "\n\n".join(report)

    print(final_report, flush=True)

    send_telegram(final_report)

    print("TELEGRAM MESSAGE SENT SUCCESSFULLY", flush=True)


if __name__ == "__main__":
    main()
