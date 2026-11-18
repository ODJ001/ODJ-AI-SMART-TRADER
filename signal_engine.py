
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
        raise RuntimeError("Unsupported candle interval")

    duration = durations[interval]
    now = datetime.now(LAGOS)
    grace = timedelta(seconds=CANDLE_GRACE_SECONDS)

    completed = []

    for candle in raw:
        candle_time = datetime.strptime(
            candle["datetime"],
            "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=LAGOS)

        candle_close_time = candle_time + duration

        if candle_close_time + grace > now:
            continue

        completed.append({
            "time": candle_time,
            "open": float(candle["open"]),
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "close": float(candle["close"])
        })

    completed.sort(key=lambda item: item["time"])

    if len(completed) < 20:
        raise RuntimeError(
            "Insufficient completed candle history"
        )

    return completed


def find_swings(candles):
    highs = []
    lows = []

    for i in range(2, len(candles) - 2):
        current = candles[i]

        if (
            current["high"] > candles[i - 1]["high"]
            and current["high"] > candles[i - 2]["high"]
            and current["high"] > candles[i + 1]["high"]
            and current["high"] > candles[i + 2]["high"]
        ):
            highs.append((i, current["high"]))

        if (
            current["low"] < candles[i - 1]["low"]
            and current["low"] < candles[i - 2]["low"]
            and current["low"] < candles[i + 1]["low"]
            and current["low"] < candles[i + 2]["low"]
        ):
            lows.append((i, current["low"]))

    return highs, lows


def analyse_structure(candles):
    highs, lows = find_swings(candles)

    if not highs or not lows:
        return {
            "direction": "UNCLEAR",
            "bos": "NONE",
            "choch": "NONE",
            "sweep": "NONE"
        }

    latest = candles[-1]
    previous = candles[-2]

    last_high = highs[-1][1]
    last_low = lows[-1][1]

    direction = "RANGING"
    bos = "NONE"
    choch = "NONE"
    sweep = "NONE"

    if latest["close"] > last_high:
        bos = "BULLISH"
        direction = "BULLISH"

        if previous["close"] < last_low:
            choch = "BULLISH"

    elif latest["close"] < last_low:
        bos = "BEARISH"
        direction = "BEARISH"

        if previous["close"] > last_high:
            choch = "BEARISH"

    if (
        latest["low"] < last_low
        and latest["close"] > last_low
    ):
        sweep = "SELL-SIDE LIQUIDITY"

    elif (
        latest["high"] > last_high
        and latest["close"] < last_high
    ):
        sweep = "BUY-SIDE LIQUIDITY"

    return {
        "direction": direction,
        "bos": bos,
        "choch": choch,
        "sweep": sweep,
        "swing_high": last_high,
        "swing_low": last_low
    }


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


def find_order_block(candles):
    if len(candles) < 4:
        return "NONE"

    previous = candles[-2]
    latest = candles[-1]

    if (
        previous["close"] < previous["open"]
        and latest["close"] > previous["high"]
    ):
        return "POSSIBLE BULLISH ORDER BLOCK"

    if (
        previous["close"] > previous["open"]
        and latest["close"] < previous["low"]
    ):
        return "POSSIBLE BEARISH ORDER BLOCK"

    return "NONE"


def analyse_market(symbol, interval):
    raw = fetch_candles(symbol, interval)
    candles = prepare_candles(raw, interval)

    structure = analyse_structure(candles)

    return {
        "symbol": symbol,
        "interval": interval,
        "time": candles[-1]["time"].strftime(
            "%Y-%m-%d %H:%M"
        ),
        "close": candles[-1]["close"],
        "direction": structure["direction"],
        "bos": structure["bos"],
        "choch": structure["choch"],
        "sweep": structure["sweep"],
        "fvg": find_fvg(candles),
        "order_block": find_order_block(candles)
    }


def main():
    if not API_KEY or not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "A required GitHub secret is missing."
        )

    now = datetime.now(LAGOS)

    results = [
        "ODJ AI SMART TRADER",
        "MARKET STRUCTURE ANALYSIS",
        f"Time: {now.strftime('%Y-%m-%d %H:%M:%S')} WAT",
        f"Mode: {MODE}",
        "Completed candles only.",
        "No trades will be placed."
    ]

    successful = 0
    failed = 0

    intervals = [
        interval.strip()
        for interval in os.getenv(
            "SCAN_INTERVALS", "15min"
        ).split(",")
        if interval.strip() in ("15min", "1h")
    ]

    if not intervals:
        raise RuntimeError("No valid scan interval configured.")

    results.append(
        f"Timeframes scanned: {', '.join(intervals)}"
    )

    for symbol in SYMBOLS:
        for interval in intervals:
            try:
                report = analyse_market(symbol, interval)

                results.append(
                    f"\n{symbol} | {interval}"
                    f"\nCompleted candle: {report['time']}"
                    f"\nClose: {report['close']}"
                    f"\nDirection: {report['direction']}"
                    f"\nBOS: {report['bos']}"
                    f"\nCHOCH: {report['choch']}"
                    f"\nLiquidity sweep: {report['sweep']}"
                    f"\nFVG: {report['fvg']}"
                    f"\nOrder Block: {report['order_block']}"
                )

                successful += 1

            except Exception as error:
                results.append(
                    f"\n{symbol} | {interval}: ERROR"
                    f"\n{type(error).__name__}: {str(error)[:120]}"
                )
                failed += 1

            time.sleep(10)

    results.append(
        f"\n\nSUMMARY"
        f"\nSuccessful analyses: {successful}"
        f"\nFailed analyses: {failed}"
        f"\nMode: DEMO"
        f"\nNo trades placed."
    )

    send_telegram("\n".join(results))

    print(
        f"Analysis complete. "
        f"Successful: {successful}, Failed: {failed}"
    )


if __name__ == "__main__":
    main()
