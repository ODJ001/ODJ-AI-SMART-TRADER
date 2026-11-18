
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

    if len(candles) < 5 or not highs or not lows:
        return result

    latest = candles[-1]
    previous = candles[-2]

    # Only use confirmed swing points.
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

    # Determine the previous market trend.
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

    # Start with the preceding trend.
    result["direction"] = (
        previous_trend
        if previous_trend != "UNCLEAR"
        else "RANGING"
    )

    # Detect a fresh bullish break using candle closes.
    bullish_break = (
        previous["close"] <= swing_high
        and latest["close"] > swing_high
    )

    # Detect a fresh bearish break using candle closes.
    bearish_break = (
        previous["close"] >= swing_low
        and latest["close"] < swing_low
    )

    # Classify structural breaks.
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

    # Detect sell-side liquidity sweep.
    # Price must trade below the swing low and close back above it.
    if (
        latest["low"] < swing_low
        and latest["close"] > swing_low
    ):
        result["sweep"] = "SELL-SIDE LIQUIDITY"

    # Detect buy-side liquidity sweep.
    # Price must trade above the swing high and close back below it.
    elif (
        latest["high"] > swing_high
        and latest["close"] < swing_high
    ):
        result["sweep"] = "BUY-SIDE LIQUIDITY"

    return result
