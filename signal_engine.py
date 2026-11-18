
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

CANDLE_COUNT = 100
CANDLE_GRACE_SECONDS = 60
REQUEST_DELAY = 10

MODE = "DEMO - ANALYSIS ONLY"


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
            "timezone": "Africa/Lagos",
            "apikey": API_KEY
        },
        timeout=30
    )

    response.raise_for_status()
    data = response.json()

    if "values" not in data:
        raise RuntimeError(
            data.get("message", "No candle data returned")
        )

    return data["values"]


def prepare_candles(raw, interval):
    durations = {
        "15min": timedelta(minutes=15),
        "1h": timedelta(hours=1)
    }

    if interval not in durations:
        raise RuntimeError("Unsupported interval")

    now = datetime.now(LAGOS)
    duration = durations[interval]
    grace = timedelta(seconds=CANDLE_GRACE_SECONDS)

    candles = []

    for item in raw:
        candle_time = datetime.strptime(
            item["datetime"],
            "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=LAGOS)

        if candle_time + duration + grace > now:
            continue

        candles.append({
            "time": candle_time,
            "open": float(item["open"]),
            "high": float(item["high"]),
            "low": float(item["low"]),
            "close": float(item["close"])
        })

    candles.sort(key=lambda x: x["time"])

    if len(candles) < 20:
        raise RuntimeError("Insufficient completed candles")

    return candles


def find_swings(candles):
    highs = []
    lows = []

    # A swing is confirmed only after two candles on its right.
    for i in range(2, len(candles) - 2):
        current = candles[i]

        if all(
            current["high"] > candles[j]["high"]
            for j in (i - 2, i - 1, i + 1, i + 2)
        ):
            highs.append({
                "index": i,
                "price": current["high"]
            })

        if all(
            current["low"] < candles[j]["low"]
            for j in (i - 2, i - 1, i + 1, i + 2)
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

    if not highs or not lows:
        return result

    latest = candles[-1]
    previous = candles[-2]

    # Use only swing levels formed before the latest candle.
    prior_highs = [
        item for item in highs
        if item["index"] < len(candles) - 1
    ]

    prior_lows = [
        item for item in lows
        if item["index"] < len(candles) - 1
    ]

    if not prior_highs or not prior_lows:
        return result

    swing_high = prior_highs[-1]["price"]
    swing_low = prior_lows[-1]["price"]

    result["swing_high"] = swing_high
    result["swing_low"] = swing_low

    # Break of structure and possible change of character.
    if latest["close"] > swing_high:
        result["bos"] = "BULLISH"
        result["direction"] = "BULLISH"

        if previous["close"] <= swing_high:
            result["choch"] = "BULLISH"

    elif latest["close"] < swing_low:
        result["bos"] = "BEARISH"
        result["direction"] = "BEARISH"

        if previous["close"] >= swing_low:
            result["choch"] = "BEARISH"

    else:
        # Use recent confirmed swing progression as a trend estimate.
        if len(highs) >= 2 and len(lows) >= 2:
            higher_high = highs[-1]["price"] > highs[-2]["price"]
            higher_low = lows[-1]["price"] > lows[-2]["price"]
            lower_high = highs[-1]["price"] < highs[-2]["price"]
            lower_low = lows[-1]["price"] < lows[-2]["price"]

            if higher_high and higher_low:
                result["direction"] = "BULLISH"
            elif lower_high and lower_low:
                result["direction"] = "BEARISH"
            else:
                result["direction"] = "RANGING"

    # Liquidity sweep: price crosses a prior swing and closes back inside.
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


def find_fvg(candles):
    if len(candles) < 3:
        return "NONE"

    first = candles[-3]
    third = candles[-1]

    if first["high"] < third["low"]:
        return "BULLISH FVG"

    if first["low"] > third["high"]:
        return "BEARISH FVG"

    return "NONE"


def find_order_block(candles, structure):
    direction = structure["direction"]

    if direction == "BULLISH":
        for i in range(len(candles) - 2, max(-1, len(candles) - 7), -1):
            candle = candles[i]

            if candle["close"] < candle["open"]:
                return "POSSIBLE BULLISH OB"

    elif direction == "BEARISH":
        for i in range(len(candles) - 2, max(-1, len(candles) - 7), -1):
            candle = candles[i]

            if candle["close"] > candle["open"]:
                return "POSSIBLE BEARISH OB"

    return "NONE"


def analyse_market(symbol, interval):
    raw = fetch_candles(symbol, interval)
    candles = prepare_candles(raw, interval)

    structure = analyse_structure(candles)

    return {
        "symbol": symbol,
        "interval": interval,
        "time": candles[-1]["time"].strftime("%Y-%m-%d %H:%M"),
        "close": candles[-1]["close"],
        "direction": structure["direction"],
        "bos": structure["bos"],
        "choch": structure["choch"],
        "sweep": structure["sweep"],
        "fvg": find_fvg(candles),
        "order_block": find_order_block(candles, structure)
    }


def confirmation_check(h1, m15):
    if h1["direction"] not in ("BULLISH", "BEARISH"):
        return "NO SETUP - H1 DIRECTION UNCLEAR"

    if m15["direction"] != h1["direction"]:
        return "NO SETUP - M15 DOES NOT AGREE WITH H1"

    direction = h1["direction"]

    if direction == "BULLISH":
        correct_sweep = m15["sweep"] == "SELL-SIDE LIQUIDITY"
        correct_choch = m15["choch"] == "BULLISH"
        correct_zone = (
            m15["fvg"] == "BULLISH FVG"
            or m15["order_block"] == "POSSIBLE BULLISH OB"
        )
    else:
        correct_sweep = m15["sweep"] == "BUY-SIDE LIQUIDITY"
        correct_choch = m15["choch"] == "BEARISH"
        correct_zone = (
            m15["fvg"] == "BEARISH FVG"
            or m15["order_block"] == "POSSIBLE BEARISH OB"
        )

    if not correct_sweep:
        return "WAIT - LIQUIDITY SWEEP NOT CONFIRMED"

    if not correct_choch:
        return "WAIT - M15 CHOCH NOT CONFIRMED"

    if not correct_zone:
        return "WAIT - NO MATCHING FVG OR ORDER BLOCK"

    return "POTENTIAL SETUP - REQUIRES FURTHER VALIDATION"


def format_report(symbol, h1, m15):
    status = confirmation_check(h1, m15)

    return (
        f"\n{symbol}"
        f"\nH1 direction: {h1['direction']}"
        f"\nH1 BOS: {h1['bos']}"
        f"\nM15 direction: {m15['direction']}"
        f"\nM15 BOS: {m15['bos']}"
        f"\nM15 CHOCH: {m15['choch']}"
        f"\nM15 liquidity sweep: {m15['sweep']}"
        f"\nM15 FVG: {m15['fvg']}"
        f"\nM15 Order Block: {m15['order_block']}"
        f"\nAssessment: {status}"
        f"\nM15 completed candle: {m15['time']}"
    )


def main():
    if not API_KEY or not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("A required GitHub secret is missing.")

    intervals = [
        item.strip()
        for item in os.getenv("SCAN_INTERVALS", "15min").split(",")
        if item.strip() in ("15min", "1h")
    ]

    if not intervals:
        raise RuntimeError("No valid scan interval configured.")

    results = [
        "ODJ AI SMART TRADER",
        "H1 - M15 CONFIRMATION ANALYSIS",
        f"Time: {datetime.now(LAGOS).strftime('%Y-%m-%d %H:%M:%S')} WAT",
        f"Mode: {MODE}",
        "Completed candles only.",
        "No trades will be placed.",
        f"Workflow intervals: {', '.join(intervals)}"
    ]

    successful = 0
    failed = 0

    for symbol in SYMBOLS:
        try:
            # The M15 workflow checks H1 first, then M15.
            if "15min" in intervals:
                h1 = analyse_market(symbol, "1h")
                time.sleep(REQUEST_DELAY)

                m15 = analyse_market(symbol, "15min")
                results.append(format_report(symbol, h1, m15))

            else:
                h1 = analyse_market(symbol, "1h")
                results.append(
                    f"\n{symbol} | H1"
                    f"\nDirection: {h1['direction']}"
                    f"\nBOS: {h1['bos']}"
                    f"\nCHOCH: {h1['choch']}"
                    f"\nLiquidity sweep: {h1['sweep']}"
                    f"\nFVG: {h1['fvg']}"
                    f"\nOrder Block: {h1['order_block']}"
                    f"\nCompleted candle: {h1['time']}"
                )

            successful += 1

        except Exception as error:
            results.append(
                f"\n{symbol}: ERROR"
                f"\n{type(error).__name__}: {str(error)[:120]}"
            )
            failed += 1

        time.sleep(REQUEST_DELAY)

    results.extend([
        "\nSUMMARY",
        f"Successful symbols: {successful}",
        f"Failed symbols: {failed}",
        "DEMO MODE - NO TRADES PLACED",
        "Assessments are not validated trading signals."
    ])

    send_telegram("\n".join(results))

    print(
        f"Analysis complete. Successful: {successful}, "
        f"Failed: {failed}"
    )


if __name__ == "__main__":
    main()
