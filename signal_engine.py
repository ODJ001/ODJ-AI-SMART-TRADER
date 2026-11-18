
import os
import time
import requests
from datetime import datetime, timezone

# ==========================================
# ODJ AI SMART TRADER CONFIGURATION
# ==========================================

API_KEY = os.getenv("TWELVE_DATA_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BASE_URL = "https://api.twelvedata.com"

SYMBOLS = [
    "EUR/USD",
    "GBP/USD",
    "USD/JPY",
    "XAU/USD"
]

CANDLE_LIMIT = 100
REQUEST_DELAY = 9
SWING_LEFT = 2
SWING_RIGHT = 2


# ==========================================
# MARKET DATA
# ==========================================

session = requests.Session()


def get_candles(symbol, interval):

    if not API_KEY:
        raise RuntimeError("Missing Twelve Data API key")

    response = session.get(
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

    if data.get("status") == "error":
        raise RuntimeError(data.get("message", "Market data error"))

    if "values" not in data:
        raise RuntimeError("No candle data returned")

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

    # Exclude potentially incomplete newest candle.
    return candles[:-1]


# ==========================================
# SWING DETECTION
# ==========================================

def find_swings(
    candles,
    left=SWING_LEFT,
    right=SWING_RIGHT
):

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


# ==========================================
# MARKET STRUCTURE ANALYSIS
# ==========================================

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

    # Determine preceding structural trend.

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
        previous_trend
        if previous_trend != "UNCLEAR"
        else "RANGING"
    )

    # Structural break detection.

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

    # Liquidity sweep detection.

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


# ==========================================
# SIGNAL CLASSIFICATION
# ==========================================

def classify_signal(h1_result, m15_result):

    direction = m15_result["direction"]
    higher_direction = h1_result["direction"]

    aligned = (
        direction in ("BULLISH", "BEARISH")
        and direction == higher_direction
    )

    structural_trigger = (
        m15_result["bos"] != "NONE"
        or m15_result["choch"] != "NONE"
        or m15_result["sweep"] != "NONE"
    )

    if aligned and structural_trigger:

        return {
            "status": "CONFIRMED STRUCTURAL SETUP",
            "signal_type": "CONFIRMED",
            "action": "WAIT FOR ENTRY CONFIRMATION",
            "direction": direction
        }

    if aligned:

        return {
            "status": "EARLY WARNING",
            "signal_type": "EARLY WARNING",
            "action": "WATCH FOR STRUCTURAL CONFIRMATION",
            "direction": direction
        }

    return {
        "status": "NO VALID SETUP",
        "signal_type": "NONE",
        "action": "WAIT",
        "direction": direction
    }


# ==========================================
# INDIVIDUAL PAIR ANALYSIS
# ==========================================

def analyse_pair(symbol):

    print(f"Fetching H1 data: {symbol}", flush=True)

    h1 = get_candles(symbol, "1h")

    time.sleep(REQUEST_DELAY)

    print(f"Fetching M15 data: {symbol}", flush=True)

    m15 = get_candles(symbol, "15min")

    h1_result = analyse_structure(h1)
    m15_result = analyse_structure(m15)

    signal = classify_signal(
        h1_result,
        m15_result
    )

    return (
        f"INSTRUMENT: {symbol}\n"
        f"STATUS: {signal['status']}\n"
        f"SIGNAL TYPE: {signal['signal_type']}\n"
        f"DIRECTION: {signal['direction']}\n"
        f"ACTION: {signal['action']}\n"
        f"H1 DIRECTION: {h1_result['direction']}\n"
        f"M15 DIRECTION: {m15_result['direction']}\n"
        f"BOS: {m15_result['bos']}\n"
        f"CHOCH: {m15_result['choch']}\n"
        f"LIQUIDITY: {m15_result['sweep']}\n"
        f"SWING HIGH: {m15_result['swing_high']}\n"
        f"SWING LOW: {m15_result['swing_low']}\n"
        f"LAST CLOSED PRICE: {m15[-1]['close']}\n"
        f"LAST CLOSED CANDLE: {m15[-1]['datetime']}"
    )


# ==========================================
# TELEGRAM NOTIFICATION
# ==========================================

def send_telegram(message):

    if not TELEGRAM_TOKEN:
        raise RuntimeError("Missing Telegram bot token")

    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing Telegram chat ID")

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
    )

    # Telegram message limit is 4096 characters.
    chunks = [
        message[i:i+4000]
        for i in range(0, len(message), 4000)
    ]

    for chunk in chunks:

        response = session.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk
            },
            timeout=30
        )

        response.raise_for_status()

        result = response.json()

        if not result.get("ok"):
            raise RuntimeError(
                f"Telegram error: {result}"
            )

    print("TELEGRAM DELIVERY SUCCESSFUL", flush=True)


# ==========================================
# MAIN EXECUTION
# ==========================================

def main():

    print(
        "ODJ AI SMART TRADER STARTED",
        flush=True
    )

    report = [
        "ODJ AI SMART TRADER",
        "MARKET STRUCTURE REPORT",
        "SIGNAL POLICY: EARLY WARNINGS + CONFIRMED",
        "--------------------------------"
    ]

    for symbol in SYMBOLS:

        try:

            print(
                f"Analysing {symbol}",
                flush=True
            )

            result = analyse_pair(symbol)

            report.append(result)

            print(
                f"Analysis completed: {symbol}",
                flush=True
            )

        except Exception as error:

            error_message = (
                f"INSTRUMENT: {symbol}\n"
                f"STATUS: ANALYSIS ERROR\n"
                f"ERROR: {str(error)}"
            )

            report.append(error_message)

            print(
                error_message,
                flush=True
            )

        # Pace all API requests, including between pairs.
        time.sleep(REQUEST_DELAY)

    final_report = (
        "\n\n".join(report)
        + "\n\n"
        + "REPORT TIME: "
        + datetime.now(timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    )

    print(final_report, flush=True)

    send_telegram(final_report)

    print(
        "ODJ AI SMART TRADER FINISHED",
        flush=True
    )


if __name__ == "__main__":
    main()
