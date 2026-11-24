import os, time, json, base64, math, requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from statistics import median

VERSION = "V13.2"
API_KEY = os.getenv("TWELVE_DATA_API_KEY") or os.getenv("TWELVEDATA_API_KEY")
BOT_TOKEN, CHAT_ID = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
GH_TOKEN, REPO = os.getenv("GH_ALERT_TOKEN"), os.getenv("GITHUB_REPOSITORY")
STATE_FILE = ".odj_alert_state.json"
BASE_URL = "https://api.twelvedata.com/time_series"
SYMBOLS = ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "USD/CAD", "XAU/USD"]
FRAMES = {"H1": ("1h", 60, 180), "M15": ("15min", 15, 45)}
OUTPUTSIZE, REQUEST_DELAY = 120, 8
MAX_ZONE_AGE, MAX_ZONE_DISTANCE_ATR, MIN_ZONE_QUALITY = 32, 3.0, 35
MAX_SWEEP_AGE, LIQUIDITY_ATR = 4, 3.0
STOP_BUFFER_ATR, MIN_STOP_ATR, MAX_STOP_ATR = .25, .15, 2.5
ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "5000"))
ACCOUNT_CURRENCY = os.getenv("ACCOUNT_CURRENCY", "USD").strip().upper()
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "0.75"))
MAX_DAILY_LOSS_PERCENT = 0.5
MAX_SIGNALS_PER_DAY = 5
MANUAL_DAILY_LOSS_USD = float(os.getenv("MANUAL_DAILY_LOSS_USD", "0"))
LAGOS = ZoneInfo("Africa/Lagos")
TP_R = (1.0, 2.0, 3.0)
BROKER = {
 "EUR/USD": (os.getenv("CONTRACT_SIZE_EURUSD"), os.getenv("MIN_LOT_EURUSD"), os.getenv("LOT_STEP_EURUSD"), os.getenv("MAX_LOT_EURUSD")),
 "GBP/USD": (os.getenv("CONTRACT_SIZE_GBPUSD"), os.getenv("MIN_LOT_GBPUSD"), os.getenv("LOT_STEP_GBPUSD"), os.getenv("MAX_LOT_GBPUSD")),
 "USD/JPY": (os.getenv("CONTRACT_SIZE_USDJPY"), os.getenv("MIN_LOT_USDJPY"), os.getenv("LOT_STEP_USDJPY"), os.getenv("MAX_LOT_USDJPY")),
 "AUD/USD": (os.getenv("CONTRACT_SIZE_AUDUSD"), os.getenv("MIN_LOT_AUDUSD"), os.getenv("LOT_STEP_AUDUSD"), os.getenv("MAX_LOT_AUDUSD")),
 "USD/CAD": (os.getenv("CONTRACT_SIZE_USDCAD"), os.getenv("MIN_LOT_USDCAD"), os.getenv("LOT_STEP_USDCAD"), os.getenv("MAX_LOT_USDCAD")),
 "XAU/USD": (os.getenv("CONTRACT_SIZE_XAUUSD"), os.getenv("MIN_LOT_XAUUSD"), os.getenv("LOT_STEP_XAUUSD"), os.getenv("MAX_LOT_XAUUSD")),
}

def parse_time(value):
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except Exception:
        return None

def fetch_candles(symbol, frame):
    if not API_KEY: raise RuntimeError("Missing Twelve Data API key")
    interval, minutes, stale_limit = FRAMES[frame]
    r = requests.get(BASE_URL, params={"symbol":symbol,"interval":interval,"outputsize":OUTPUTSIZE,"timezone":"UTC","apikey":API_KEY}, timeout=25)
    r.raise_for_status(); data = r.json()
    if "values" not in data: raise RuntimeError(data.get("message", str(data)))
    now, duration, candles = datetime.now(timezone.utc), timedelta(minutes=minutes), []
    for item in data["values"]:
        stamp = parse_time(item.get("datetime"))
        if stamp is None or stamp + duration > now: continue
        try:
            c = {"time":stamp,"open":float(item["open"]),"high":float(item["high"]),"low":float(item["low"]),"close":float(item["close"])}
            if c["high"] >= max(c["open"],c["close"]) and c["low"] <= min(c["open"],c["close"]): candles.append(c)
        except (ValueError, TypeError, KeyError): pass
    candles.sort(key=lambda x:x["time"])
    if len(candles)<40: raise RuntimeError(f"Insufficient completed candles: {symbol} {frame}")
    ended=candles[-1]["time"]+duration; age=(now-ended).total_seconds()/60
    if age>stale_limit: raise RuntimeError(f"STALE DATA: {symbol} {frame}, age {age:.1f} minutes")
    return candles, {"latest_candle_close":ended,"data_age_minutes":age,"candles_count":len(candles)}

def atr(candles, period=14):
    tr=[]
    for i in range(1,len(candles)):
        c,p=candles[i],candles[i-1]
        tr.append(max(c["high"]-c["low"],abs(c["high"]-p["close"]),abs(c["low"]-p["close"])))
    return sum(tr[-period:])/period if len(tr)>=period else 0.0

def swings(candles,left=2,right=2):
    hi,lo=[],[]
    for i in range(left,len(candles)-right):
        h,l=candles[i]["high"],candles[i]["low"]
        if all(h>candles[j]["high"] for j in range(i-left,i+right+1) if j!=i): hi.append({"index":i,"price":h})
        if all(l<candles[j]["low"] for j in range(i-left,i+right+1) if j!=i): lo.append({"index":i,"price":l})
    return hi,lo

def trend(hi,lo):
    if len(hi)<2 or len(lo)<2:return "RANGING"
    if hi[-1]["price"]>hi[-2]["price"] and lo[-1]["price"]>lo[-2]["price"]:return "BULLISH"
    if hi[-1]["price"]<hi[-2]["price"] and lo[-1]["price"]<lo[-2]["price"]:return "BEARISH"
    return "RANGING"

def structure(candles):
    hi,lo=swings(candles)
    out={"direction":trend(hi,lo),"bos":"NONE","choch":"NONE","range_break":"NONE","event_index":None,"event_level":None,"event_age":None,"highs":hi,"lows":lo}
    # At each historical bar, use only pivots that were confirmed before that bar.
    for i in range(max(2,len(candles)-34),len(candles)):
        ph=[x for x in hi if x["index"]+2 < i]
        pl=[x for x in lo if x["index"]+2 < i]
        if not ph or not pl:continue
        upper,lower=ph[-1]["price"],pl[-1]["price"]
        prior_hi=[x for x in hi if x["index"]+2 < i]
        prior_lo=[x for x in lo if x["index"]+2 < i]
        local_trend=trend(prior_hi,prior_lo)
        prev,close=candles[i-1]["close"],candles[i]["close"]
        if prev<=upper and close>upper:
            out.update(event_index=i,event_level=upper,event_age=len(candles)-1-i,bos="NONE",choch="NONE",range_break="NONE")
            if local_trend=="BEARISH":out["choch"]="BULLISH"
            elif local_trend=="RANGING":out["range_break"]="BULLISH"
            else:out["bos"]="BULLISH"
        elif prev>=lower and close<lower:
            out.update(event_index=i,event_level=lower,event_age=len(candles)-1-i,bos="NONE",choch="NONE",range_break="NONE")
            if local_trend=="BULLISH":out["choch"]="BEARISH"
            elif local_trend=="RANGING":out["range_break"]="BEARISH"
            else:out["bos"]="BEARISH"
    return out

def structure_event(s):
    return s["bos"] if s["bos"]!="NONE" else s["choch"] if s["choch"]!="NONE" else None

def fvg_zones(candles,a):
    out=[]
    if a<=0:return out
    price=candles[-1]["close"]
    for i in range(2,len(candles)-1):
        age=len(candles)-1-i
        if not 1<=age<=MAX_ZONE_AGE:continue
        x,z=candles[i-2],candles[i]
        if z["low"]>x["high"]: low,high,d=x["high"],z["low"],"BULLISH"
        elif z["high"]<x["low"]:low,high,d=z["high"],x["low"],"BEARISH"
        else:continue
        mid=(low+high)/2; dist=abs(price-mid)/a; gap=(high-low)/a
        if dist>MAX_ZONE_DISTANCE_ATR or gap<.05:continue
        later=candles[i+1:-1]
        full=any(c["low"]<=low for c in later) if d=="BULLISH" else any(c["high"]>=high for c in later)
        partial=any(c["low"]<high for c in later) if d=="BULLISH" else any(c["high"]>low for c in later)
        if full:continue
        q=int(max(0,min(100,38+min(25,gap*30)+max(0,30-age)-dist*10-(10 if partial else 0))))
        if q>=MIN_ZONE_QUALITY:out.append({"type":"FVG","direction":d,"low":low,"high":high,"midpoint":mid,"index":i,"age":age,"distance":dist,"quality":q,"partially_mitigated":partial})
    return out

def order_block(candles,direction,a):
    if a<=0 or direction not in ("BULLISH","BEARISH"):return None
    price=candles[-1]["close"]
    for i in range(len(candles)-2,max(0,len(candles)-MAX_ZONE_AGE-2),-1):
        c=candles[i]; opposing=(direction=="BULLISH" and c["close"]<c["open"]) or (direction=="BEARISH" and c["close"]>c["open"])
        if not opposing:continue
        low,high=c["low"],c["high"]; mid=(low+high)/2; age=len(candles)-1-i; dist=abs(price-mid)/a
        if age<1 or dist>MAX_ZONE_DISTANCE_ATR:continue
        later=candles[i+1:-1]
        invalid=any(x["low"]<low for x in later) if direction=="BULLISH" else any(x["high"]>high for x in later)
        if invalid:continue
        partial=any(x["low"]<high for x in later) if direction=="BULLISH" else any(x["high"]>low for x in later)
        q=int(max(0,78-dist*12-age-(8 if partial else 0)))
        if q>=MIN_ZONE_QUALITY:return {"type":"OB","direction":direction,"low":low,"high":high,"midpoint":mid,"index":i,"age":age,"distance":dist,"quality":q,"partially_mitigated":partial}
    return None

def choose_zone(candles,a,direction):
    if direction not in ("BULLISH","BEARISH"):return None
    zones=[z for z in fvg_zones(candles,a) if z["direction"]==direction]
    ob=order_block(candles,direction,a)
    if ob:zones.append(ob)
    if not zones:return None
    return sorted(zones,key=lambda z:(z["quality"],-z["distance"],-z["age"]),reverse=True)[0]

def retest_rejection(candles,z):
    if not z:return {"retest":False,"rejection":False}
    c=candles[-1]; touched=c["low"]<=z["high"] and c["high"]>=z["low"]
    if not touched:return {"retest":False,"rejection":False}
    span=c["high"]-c["low"]
    if span<=0:return {"retest":True,"rejection":False}
    body=abs(c["close"]-c["open"])/span; pos=(c["close"]-c["low"])/span
    if z["direction"]=="BULLISH":
        wick=min(c["open"],c["close"])-c["low"]
        ok=c["close"]>c["open"] and pos>=.60 and body>=.20 and wick>span*.15 and c["close"]>z["midpoint"]
    else:
        wick=c["high"]-max(c["open"],c["close"])
        ok=c["close"]<c["open"] and pos<=.40 and body>=.20 and wick>span*.15 and c["close"]<z["midpoint"]
    return {"retest":True,"rejection":bool(ok)}

def sweep_signal(candles,s):
    hi,lo=s["highs"],s["lows"]; last=len(candles)-1
    for i in range(last-1,max(2,last-MAX_SWEEP_AGE-1),-1):
        c,f=candles[i],candles[i+1]; ph=[x["price"] for x in hi if x["index"]<=i-2]; pl=[x["price"] for x in lo if x["index"]<=i-2]
        if ph:
            level=ph[-1]
            if c["high"]>level and c["close"]<level and f["close"]<level and f["close"]<f["open"]:
                return {"type":"BUY-SIDE SWEEP","direction":"BEARISH","level":level,"age":last-i}
        if pl:
            level=pl[-1]
            if c["low"]<level and c["close"]>level and f["close"]>level and f["close"]>f["open"]:
                return {"type":"SELL-SIDE SWEEP","direction":"BULLISH","level":level,"age":last-i}
    return None

def cluster(levels,tol):
    groups=[]
    for x in sorted(levels,key=lambda y:y["price"]):
        if not groups or abs(x["price"]-median([y["price"] for y in groups[-1]]))>tol:groups.append([x])
        else:groups[-1].append(x)
    return [{"price":median([x["price"] for x in g]),"touches":len(g)} for g in groups if len(g)>=2]

def liquidity(candles,a):
    hi,lo=swings(candles); price=candles[-1]["close"]; tol=max(a*.12,1e-8)
    def split(items):
        near,far=[],[]
        for x in items:
            d=abs(x["price"]-price)/a if a else float("inf"); y=dict(x,distance_atr=d)
            (near if d<=LIQUIDITY_ATR else far).append(y)
        near.sort(key=lambda q:q["distance_atr"]);far.sort(key=lambda q:q["distance_atr"])
        return near[:3],far[:3]
    bn,bf=split(cluster(hi,tol));sn,sf=split(cluster(lo,tol))
    return bn,bf,sn,sf

def position_size(symbol,entry,stop):
    daily_limit=ACCOUNT_BALANCE*MAX_DAILY_LOSS_PERCENT/100
    remaining_daily_budget=max(0.0,daily_limit-MANUAL_DAILY_LOSS_USD)
    requested_trade_risk=ACCOUNT_BALANCE*RISK_PERCENT/100
    budget=min(requested_trade_risk,remaining_daily_budget)
    def no(reason):return {"available":False,"lots":None,"reason":reason,"risk_amount":budget}
    if ACCOUNT_CURRENCY!="USD":return no("Only USD accounts are supported by this calculator.")
    specs=BROKER.get(symbol,())
    if len(specs)<3 or any(not x for x in specs[:3]):return no("Missing exact Exness contract size, minimum lot or lot step.")
    try:
        contract,minlot,step=map(float,specs[:3]);maxlot=float(specs[3]) if specs[3] else None
    except ValueError:return no("Broker specifications must be numeric.")
    vals=[contract,minlot,step]+([maxlot] if maxlot else [])
    if not all(math.isfinite(x) and x>0 for x in vals):return no("Broker specifications must be positive.")
    conversion=1/entry if symbol=="USD/JPY" else 1.0
    per_lot=abs(entry-stop)*contract*conversion
    if per_lot<=0 or not math.isfinite(per_lot):return no("Invalid monetary risk calculation.")
    lots=math.floor((budget/per_lot+1e-12)/step)*step
    if maxlot is not None:lots=min(lots,math.floor((maxlot+1e-12)/step)*step)
    lots=round(lots,8)
    if lots<minlot:return no(f"Risk-sized volume is below broker minimum ({minlot}).")
    risk=lots*per_lot
    if risk>budget+1e-6:return no("Rounded lot size exceeds risk budget.")
    return {"available":True,"lots":lots,"estimated_risk":risk,"risk_amount":budget,"conversion_note":"USD/JPY conversion estimated using entry price." if symbol=="USD/JPY" else "USD quote currency; 1:1 conversion."}

def levels(symbol,direction,entry,z,a,candles):
    if not z or a<=0 or entry<=0:return None
    stop=min(z["low"],candles[-1]["low"])-a*STOP_BUFFER_ATR if direction=="BUY" else max(z["high"],candles[-1]["high"])+a*STOP_BUFFER_ATR
    risk=entry-stop if direction=="BUY" else stop-entry; rat=risk/a
    if risk<=0 or not MIN_STOP_ATR<=rat<=MAX_STOP_ATR:return None
    sign=1 if direction=="BUY" else -1
    return {"direction":direction,"entry":entry,"stop":stop,"risk":risk,"risk_atr":rat,
            "tp1":entry+sign*risk*TP_R[0],"tp2":entry+sign*risk*TP_R[1],"tp3":entry+sign*risk*TP_R[2],
            "position_size":position_size(symbol,entry,stop)}

def evaluate(symbol,h1,m15,hmeta,mmeta):
    hs,ms=structure(h1),structure(m15); a=atr(m15); price=m15[-1]["close"]
    hd,md=hs["direction"],ms["direction"]; aligned=hd in ("BULLISH","BEARISH") and hd==md
    event=structure_event(ms)
    # Discover directional candidates before all confirmation gates are met.
    direction=hd if aligned else md if md in ("BULLISH","BEARISH") else event or (ms["range_break"] if ms["range_break"]!="NONE" else None)
    z=choose_zone(m15,a,direction); ret=retest_rejection(m15,z); sw=sweep_signal(m15,ms)
    structure_ok=bool(event and event==direction); zone_ok=bool(z and z["direction"]==direction)
    sweep_ok=bool(sw and sw["direction"]==direction)
    candidate=None
    if zone_ok:candidate=levels(symbol,"BUY" if direction=="BULLISH" else "SELL",z["midpoint"],z,a,m15)
    confirmed=all([aligned,structure_ok,zone_ok,ret["retest"],ret["rejection"],sweep_ok])
    confirmed_plan=levels(symbol,"BUY" if direction=="BULLISH" else "SELL",price,z,a,m15) if confirmed else None
    if confirmed_plan:status="CONFIRMED "+confirmed_plan["direction"];plan=confirmed_plan;is_confirmed=True
    elif candidate:status="PROPOSED "+candidate["direction"];plan=candidate;is_confirmed=False
    else:status="WATCHLIST" if direction else "NO SETUP";plan=None;is_confirmed=False
    missing=[]
    for ok,label in [(aligned,"H1/M15 trend alignment"),(structure_ok,"M15 BOS/CHOCH in candidate direction"),(zone_ok,"fresh aligned FVG/order block"),(ret["retest"],"zone retest"),(ret["rejection"],"rejection candle"),(sweep_ok,"fresh matching liquidity sweep")]:
        if not ok:missing.append(label)
    if zone_ok and plan is None:missing.append("valid stop distance / trade levels")
    score=(20 if aligned else 0)+(20 if structure_ok else 0)+(15 if zone_ok else 0)+(10 if ret["retest"] else 0)+(15 if ret["rejection"] else 0)+(20 if sweep_ok else 0)
    watch=(15 if hd!="RANGING" else 0)+(15 if md!="RANGING" else 0)+(min(30,int(z["quality"]*.3)) if z else 0)+(20 if structure_ok else 0)+(20 if aligned else 0)+(10 if sweep_ok else 0)
    bn,bf,sn,sf=liquidity(m15,a)
    return {"symbol":symbol,"price":price,"atr":a,"status":status,"h1":hs,"m15":ms,"zone":z,"retest":ret,"sweep":sw,
            "aligned":aligned,"watch_score":min(100,watch),"entry_score":score,"entry_valid":is_confirmed,
            "proposed":status.startswith("PROPOSED"),"levels":plan,"missing":missing,"buy_near":bn,"buy_far":bf,
            "sell_near":sn,"sell_far":sf,"h1_meta":hmeta,"m15_meta":mmeta}

def fmt(x,s):
    if x is None:return "N/A"
    return f"{x:.{3 if 'JPY' in s else 2 if 'XAU' in s else 5}f}"

def liq_text(xs,s):
    return ", ".join(f"{fmt(x['price'],s)} ({x['touches']} touches)" for x in xs) if xs else "None"

def plan_message(r,confirmed):
    p,z,s=r["levels"],r["zone"],r["symbol"]; sizing=p["position_size"]
    lines=[f"ð¤ ODJ AI SMART TRADER {VERSION}",f"{'ð¢' if p['direction']=='BUY' else 'ð´'} {'CONFIRMED' if confirmed else 'PROPOSED â WAIT FOR CONFIRMATION'} {p['direction']} â {s}",
           f"Entry reference: {fmt(p['entry'],s)}",f"Stop Loss: {fmt(p['stop'],s)}",
           f"TP1: {fmt(p['tp1'],s)} (1R)",f"TP2: {fmt(p['tp2'],s)} (2R)",f"TP3: {fmt(p['tp3'],s)} (3R)",
           f"Stop distance: {fmt(p['risk'],s)} | {p['risk_atr']:.2f} ATR",
           f"Account: {ACCOUNT_CURRENCY} {ACCOUNT_BALANCE:,.2f} | requested risk: {RISK_PERCENT:.2f}% ({ACCOUNT_CURRENCY} {ACCOUNT_BALANCE*RISK_PERCENT/100:.2f}) | capped trade budget: {ACCOUNT_CURRENCY} {sizing['risk_amount']:.2f}"]
    if sizing["available"]:lines.append(f"Risk-sized lot: {sizing['lots']:.8f} | estimated risk: {ACCOUNT_CURRENCY} {sizing['estimated_risk']:.2f} | {sizing['conversion_note']}")
    else:lines.append("LOT SIZE NOT AVAILABLE: "+sizing["reason"])
    lines += [f"H1 {r['h1']['direction']} | M15 {r['m15']['direction']}",f"Zone: {z['type']} {z['direction']} {fmt(z['low'],s)}â{fmt(z['high'],s)} | quality {z['quality']}/100",
              f"Retest: {'YES' if r['retest']['retest'] else 'NO'} | rejection: {'YES' if r['retest']['rejection'] else 'NO'}",
              f"Sweep: {r['sweep']['type'] if r['sweep'] else 'NOT CONFIRMED'}"]
    if not confirmed:lines += ["Still required:"]+["- "+x for x in r["missing"]]+["Candidate reference only; NOT a confirmed entry or filled order."]
    else:lines.append("Entry reference uses completed M15 close. Check live price, spread and slippage; do not chase.")
    lines.append("Alert only â no automatic trading. Prices are references, not guaranteed fills.")
    return "\n".join(lines)

def format_result(r):
    s=r["symbol"];h,m,z=r["h1"],r["m15"],r["zone"]
    lines=[f"{s} â {r['status']}",f"H1 {h['direction']} | M15 {m['direction']}",
           f"BOS {m['bos']} | CHOCH {m['choch']} | RANGE BREAK {m['range_break']}",
           f"Price {fmt(r['price'],s)} | Watch {r['watch_score']}/100 | Entry conditions {r['entry_score']}/100",
           f"H1 ended {r['h1_meta']['latest_candle_close'].strftime('%Y-%m-%d %H:%M UTC')} | age {r['h1_meta']['data_age_minutes']:.1f}m",
           f"M15 ended {r['m15_meta']['latest_candle_close'].strftime('%Y-%m-%d %H:%M UTC')} | age {r['m15_meta']['data_age_minutes']:.1f}m"]
    if z:lines += [f"Zone {z['type']} {z['direction']} {fmt(z['low'],s)}â{fmt(z['high'],s)} | quality {z['quality']}/100 | age {z['age']} | distance {z['distance']:.2f} ATR",
                   f"Retest {'YES' if r['retest']['retest'] else 'NO'} | rejection {'YES' if r['retest']['rejection'] else 'NO'}"]
    else:lines.append("No qualifying zone for candidate direction.")
    lines.append("Sweep: "+(f"{r['sweep']['type']} at {fmt(r['sweep']['level'],s)}" if r["sweep"] else "None confirmed"))
    lines += ["Nearby buy-side liquidity: "+liq_text(r["buy_near"],s),"Nearby sell-side liquidity: "+liq_text(r["sell_near"],s)]
    if r["levels"]:
        p=r["levels"];lines.append(f"Entry {fmt(p['entry'],s)} | SL {fmt(p['stop'],s)} | TP1 {fmt(p['tp1'],s)} | TP2 {fmt(p['tp2'],s)} | TP3 {fmt(p['tp3'],s)}")
        lines.append("PROPOSED ONLY â wait for confirmations." if r["proposed"] else "CONFIRMED CONDITIONS â separate alert follows.")
    if r["missing"]:lines.append("Missing: "+", ".join(r["missing"]))
    return "\n".join(lines)

def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:raise RuntimeError("Missing Telegram configuration")
    r=requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",json={"chat_id":CHAT_ID,"text":message,"disable_web_page_preview":True},timeout=25)
    r.raise_for_status();data=r.json()
    if not data.get("ok"):raise RuntimeError(f"Telegram rejected message: {data}")

def send_long(message):
    chunks=[];buf=""
    for line in message.splitlines():
        if len(buf)+len(line)+1>3900:
            if buf:chunks.append(buf)
            buf=""
        buf+=line+"\n"
    if buf:chunks.append(buf)
    for part in chunks:send_telegram(part)

def gh_headers():
    return {"Authorization":f"Bearer {GH_TOKEN}","Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28"}

def state_url():
    return f"https://api.github.com/repos/{REPO}/contents/{STATE_FILE}" if REPO else None

def load_state():
    if not GH_TOKEN or not REPO:
        print("WARNING: persistent duplicate protection disabled.");return {"alerts":{}},None,False
    try:
        r=requests.get(state_url(),headers=gh_headers(),timeout=20)
        if r.status_code==404:return {"alerts":{}},None,True
        r.raise_for_status();d=r.json();state=json.loads(base64.b64decode(d["content"].replace("\n","")).decode())
        if not isinstance(state,dict):state={}
        if not isinstance(state.get("alerts"),dict):state["alerts"]={}
        return state,d.get("sha"),True
    except Exception as e:
        print("WARNING: cannot load alert state:",e);return {"alerts":{}},None,False

def save_state(state,sha):
    try:
        raw=base64.b64encode(json.dumps(state,indent=2,sort_keys=True).encode()).decode()
        payload={"message":f"Update ODJ Smart Trader {VERSION} alert state","content":raw}
        if sha:payload["sha"]=sha
        r=requests.put(state_url(),headers=gh_headers(),json=payload,timeout=25);r.raise_for_status()
        print("Persistent alert state saved.");return True
    except Exception as e:print("WARNING: cannot save alert state:",e);return False

def alert_signature(r,kind):
    z,s=r["zone"],r["symbol"];digits=3 if "JPY" in s else 2 if "XAU" in s else 5
    return "|".join([kind,s,z["direction"],z["type"],str(round(z["low"],digits)),str(round(z["high"],digits))])

def prepare_alerts(results,state,persist):
    if not persist:
        raise RuntimeError("Persistent GitHub alert state is required for the daily five-signal cap. Configure GH_ALERT_TOKEN with repository Contents read/write access.")
    today=datetime.now(LAGOS).date().isoformat()
    daily=state.get("daily",{}) if isinstance(state.get("daily",{}),dict) else {}
    if daily.get("date") != today:
        daily={"date":today,"signals_sent":0}
    used=int(daily.get("signals_sent",0))
    cutoff=datetime.now(timezone.utc)-timedelta(days=30);stored={}
    for sig,stamp in state.get("alerts",{}).items():
        t=parse_time(stamp)
        if t and t>=cutoff:stored[sig]=stamp
    alerts=[]
    daily_loss_limit=ACCOUNT_BALANCE*MAX_DAILY_LOSS_PERCENT/100
    if MANUAL_DAILY_LOSS_USD < 0 or not math.isfinite(MANUAL_DAILY_LOSS_USD):
        raise RuntimeError("MANUAL_DAILY_LOSS_USD must be a non-negative loss amount in USD.")
    if MANUAL_DAILY_LOSS_USD >= daily_loss_limit:
        state["daily"]={"date":today,"signals_sent":used,"loss_lock":True}
        return [],stored,state,"DAILY LOSS LOCK: manual reported loss has reached the 0.5% daily limit. No signal alerts will be sent."
    for r in results:
        if "error" in r or not r.get("levels"):continue
        kind="CONFIRMED" if r["entry_valid"] else "PROPOSED";sig=alert_signature(r,kind)
        if sig in stored:continue
        if used+len(alerts)>=MAX_SIGNALS_PER_DAY:break
        alerts.append({"signature":sig,"message":plan_message(r,r["entry_valid"])})
    return alerts,stored,state,None

def main():
    if not API_KEY:raise RuntimeError("Twelve Data API key missing")
    if not BOT_TOKEN or not CHAT_ID:raise RuntimeError("Telegram credentials missing")
    if not math.isfinite(ACCOUNT_BALANCE) or ACCOUNT_BALANCE<=0 or RISK_PERCENT != 0.75:raise RuntimeError("Account balance must be positive and per-trade risk must remain 0.75%.")
    if not math.isfinite(MANUAL_DAILY_LOSS_USD) or MANUAL_DAILY_LOSS_USD<0:raise RuntimeError("MANUAL_DAILY_LOSS_USD must be non-negative.")
    scan=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC");results=[]
    for symbol in SYMBOLS:
        try:
            print("Scanning",symbol);h1,hm=fetch_candles(symbol,"H1");time.sleep(REQUEST_DELAY)
            m15,mm=fetch_candles(symbol,"M15");time.sleep(REQUEST_DELAY)
            r=evaluate(symbol,h1,m15,hm,mm);results.append(r);print(symbol,r["status"],"score",r["entry_score"])
        except Exception as e:print("ERROR",symbol,e);results.append({"symbol":symbol,"error":str(e)})
    report=[f"ð¤ ODJ AI SMART TRADER {VERSION}",f"Scan: {scan}","ALERT ONLY â NO AUTOMATIC TRADING",
            "Daily loss lock depends on MANUAL_DAILY_LOSS_USD being updated with your Exness realised daily loss; it cannot read manual trades.",
            "PROPOSED BUY/SELL candidates are separate from CONFIRMED BUY/SELL entries.",
            "Proposed entry reference = zone midpoint; confirmed entry reference = completed M15 close.",
            f"Account {ACCOUNT_CURRENCY} {ACCOUNT_BALANCE:,.2f} | risk {RISK_PERCENT:.2f}% | budget {ACCOUNT_CURRENCY} {ACCOUNT_BALANCE*RISK_PERCENT/100:.2f}",
            f"Daily loss ceiling: USD {ACCOUNT_BALANCE*MAX_DAILY_LOSS_PERCENT/100:.2f} (0.5%). Manual realised loss input: USD {MANUAL_DAILY_LOSS_USD:.2f}.",
            f"Per-trade risk target: {RISK_PERCENT:.2f}% (USD {ACCOUNT_BALANCE*RISK_PERCENT/100:.2f}); actual position risk is capped by remaining daily loss budget (USD {max(0.0,ACCOUNT_BALANCE*MAX_DAILY_LOSS_PERCENT/100-MANUAL_DAILY_LOSS_USD):.2f}).",
            f"Maximum {MAX_SIGNALS_PER_DAY} proposed/confirmed alerts per Lagos calendar day. Minimum target: 2R (TP2).",
            "Minimum target is TP2=2R (TP1=1R is optional partial target); exact broker specs required for lot sizing.",""]
    for r in results:
        report.append(f"{r['symbol']} â SCAN ERROR\n{r['error']}" if "error" in r else format_result(r));report.append("------------------------")
    report.append("Confirmation requires H1/M15 alignment, matching M15 BOS/CHOCH, zone retest and rejection, plus a matching fresh sweep. Proposed ideas are not confirmed trades. No automatic orders are placed.")
    send_long("\n".join(report))
    state,sha,persist=load_state();alerts,stored,state,lock_message=prepare_alerts(results,state,persist);sent=[]
    if lock_message:send_long(lock_message)
    for item in alerts:
        try:send_long(item["message"]);sent.append(item["signature"]);print("Alert sent:",item["signature"])
        except Exception as e:print("Alert failed:",e)
    if persist:
        stamp=datetime.now(timezone.utc).isoformat()
        for sig in sent:stored[sig]=stamp
        today=datetime.now(LAGOS).date().isoformat()
        daily=state.get("daily",{}) if isinstance(state.get("daily",{}),dict) else {}
        if daily.get("date") != today:daily={"date":today,"signals_sent":0}
        daily["signals_sent"]=int(daily.get("signals_sent",0))+len(sent)
        daily["loss_lock"]=bool(lock_message)
        state["daily"]=daily
        state["alerts"]=dict(sorted(stored.items(),key=lambda x:x[1],reverse=True)[:200]);save_state(state,sha)
    print(VERSION,"scan completed.")

if __name__=="__main__":main()
