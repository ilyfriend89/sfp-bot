# -*- coding: utf-8 -*-
# =====================================================================
#  SFP-бот v5 CLEAN — Bybit / BingX — оболочка Flet
#  ЧАСТЬ 1/5: конфигурация, индикаторы, утилиты
#  ⚠️ ТОЛЬКО ИЗОЛИРОВАННАЯ МАРЖА (ISOLATED), ПЛЕЧО 5x В ПРИЛОЖЕНИИ БИРЖИ.
# =====================================================================
import os
import csv
import json
import time
import hmac
import hashlib
import urllib.parse
import threading
import traceback
import datetime as dt
from decimal import Decimal, ROUND_DOWN
import requests
import flet as ft

WARNING_ISOLATED = ("⚠️ ТОРГОВАТЬ ТОЛЬКО ИЗОЛИРОВАННОЙ МАРЖОЙ (ISOLATED) С ПЛЕЧОМ 5x. "
                    "Размер позиции считает бот из риска — плечо задаёт только залог.")

BYBIT_REAL = "https://api.bybit.com"
BYBIT_DEMO = "https://api-demo.bybit.com"
BINGX_REAL = "https://open-api.bingx.com"
BINGX_DEMO = "https://open-api-vst.bingx.com"

# ------------------- ПАПКА ДАННЫХ (Download/SFP_Bot) -------------------
LOG_MAX = 200_000
_LOG_PENDING = []   # записи, сделанные до выбора LOG_FILE — сбрасываются ниже

def log_to_file(text):
    """До инициализации LOG_FILE копит в буфер, после — пишет в файл."""
    try:
        path = globals().get("LOG_FILE")
        if not path:
            _LOG_PENDING.append(f"{dt.datetime.now():%d.%m %H:%M:%S} | {text}")
            return
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX:
            with open(path, "r", encoding="utf-8") as f:
                data = f.read()
            with open(path, "w", encoding="utf-8") as f:
                f.write(data[-LOG_MAX // 2:])
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now():%d.%m %H:%M:%S} | {text}\n")
    except Exception:
        pass

def _dir_writable(p):
    """Фактическая проба записи — os.access на Android врёт (scoped storage)."""
    try:
        probe = os.path.join(p, ".sfp_write_test")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except Exception:
        return False

def _get_data_dir():
    """Папка для логов, сделок, состояния. Выбор — по фактической записи."""
    for p in ("/storage/emulated/0/Download", "/sdcard/Download",
              os.path.expanduser("~/Downloads"), os.path.expanduser("~/Загрузки")):
        if os.path.isdir(p) and _dir_writable(p):
            sub = os.path.join(p, "SFP_Bot")
            try:
                os.makedirs(sub, exist_ok=True)
                if _dir_writable(sub):
                    log_to_file(f"[DIR] выбран путь: {sub}")
                    return sub
            except Exception:
                continue
    base = os.getenv("FLET_APP_STORAGE_DATA") or os.getcwd()
    sub = os.path.join(base, "SFP_Bot")
    os.makedirs(sub, exist_ok=True)
    log_to_file(f"[DIR] Download недоступен, запасной путь: {sub}")
    return sub

_DATA_DIR = _get_data_dir()
STATE_FILE = os.path.join(_DATA_DIR, "sfp_state.json")
LOG_FILE = os.path.join(_DATA_DIR, "sfp_log.txt")
TRADES_FILE = os.path.join(_DATA_DIR, "sfp_trades.csv")
log_to_file(f"[DIR] итог: {_DATA_DIR}")
LOG_MAX = 200_000
DEBUG_API = True
DRY_RUN = False
DRY_RUN_BALANCE = 1000.0

# ------------------- ТАЙМФРЕЙМЫ -------------------
TIMEFRAMES = {
    "1 минута":  {"interval_bybit": "1",    "interval_bingx": "1m",   "vol_mult": 3.0},
    "5 минут":   {"interval_bybit": "5",    "interval_bingx": "5m",   "vol_mult": 2.8},
    "15 минут":  {"interval_bybit": "15",   "interval_bingx": "15m",  "vol_mult": 1.5},
    "30 минут":  {"interval_bybit": "30",   "interval_bingx": "30m",  "vol_mult": 2.2},
    "1 час":     {"interval_bybit": "60",   "interval_bingx": "1h",   "vol_mult": 2.0},
    "4 часа":    {"interval_bybit": "240",  "interval_bingx": "4h",   "vol_mult": 1.5},
    "1 день":    {"interval_bybit": "D",    "interval_bingx": "1d",   "vol_mult": 1.2},
}
TF_MINUTES = {"1 минута": 1, "5 минут": 5, "15 минут": 15, "30 минут": 30,
              "1 час": 60, "4 часа": 240, "1 день": 1440}
TF_ORDER = ["1 минута", "5 минут", "15 минут", "30 минут", "1 час", "4 часа", "1 день"]

BYBIT_ALIASES = {
    "PEPEUSDT": "1000PEPEUSDT", "BONKUSDT": "1000BONKUSDT",
    "FLOKIUSDT": "1000FLOKIUSDT", "SHIBUSDT": "1000SHIBUSDT",
    "PUMPUSDT": "1000PUMPUSDT", "WLFIUSDT": "1000WLFIUSDT",
}
BINGX_ALIASES = {
    "PEPEUSDT": "1000PEPEUSDT", "PUMPUSDT": "1000PUMPUSDT",
}

# ------------------- ИНДИКАТОРЫ / РЕЖИМ -------------------
EMA_PERIOD = 50
EMA_FAST = 20
EMA_OFF = "Выключен"
ATR_PERIOD = 14
REGIME_LOOKBACK = 10
REGIME_THRESHOLD = 1.5
HTF_BARS = 160
WORK_BARS = 320

# ------------------- ИЗДЕРЖКИ (восстановлено по MIXED5, 18.11.2026) -------------------
FEE_MAKER = 0.0002
FEE_TAKER = 0.0005
COST_STOP_MULT = 10.0          
MAX_SPREAD_PCT = 0.30          

# ------------------- СХЕМЫ ВЫХОДОВ (восстановлено по MIXED5, 18.11.2026; форвард-чек пройден) -------------------
TP_TREND = [(1.5, 0.50), (3.0, 0.20)]  
TREND_TAIL_SHARE = 0.30
CHANDELIER_LOOKBACK = 22
CHANDELIER_ATR = 3.0                   
TREND_BE_AFTER_TP1 = 0.0               # после ТП1 стоп в безубыток (0.0R)

RANGE_TP1_SHARE = 0.50
RANGE_TP2_SHARE = 0.50
RANGE_BE_AFTER_TP1 = -0.30
RANGE_MIN_TP1_R = 1.0                  # БЫЛО 1.2 — мягче фильтр по середине канала
RANGE_MIN_TP2_R = 2.0                  # БЫЛО 2.5 — мягче фильтр по дальней границе
RANGE_TARGET_FRAC = 0.90
RANGE_MIN_WIDTH_R = 3.5
RANGE_STOP_ATR = 0.6
RANGE_ENTRY_RETRACE = 0.20             # БЫЛО 0.38 — ключевой фикс! Вход ближе к границе
RANGE_DAYS = 3
RANGE_TOUCH_TOL = 0.0015
RANGE_VOL_MULT = 1.3
RANGE_MIN_TOUCHES = 2

SFP_SWING = 20
SFP_STOP_ATR = 0.5
CHOCH_SWING = 60
CHOCH_MINOR = 10
CHOCH_STOP_ATR = 0.6
CHOCH_COUNTER_SIZE = 0.70

# ------------------- РИСК / ЛИМИТЫ (восстановлено по MIXED5, 18.11.2026) -------------------
RISK_PCT_DEFAULT = 0.015               # 1.5% на сделку
RISK_CAP_LEVERAGE = 5.0
ENTRY_WAIT = 3
FAST_PERIOD = 5
CANCEL_RETRY_SEC = 20
MAX_STOPS_PER_PAIR = 3
MAX_STOPS_BASKET = 5                   # - попыток в день
DAILY_R_LIMIT = -4.0
PAIR_COOLDOWN_HOURS = 4
PAIR_COOLDOWN_STOPS = 2
MAX_TOTAL_RISK = 0.030                 # 3.0% суммарный риск
MAX_SAME_DIR_RISK = 0.030              # 3.0% в одну сторону

# ------------------- СКОРИНГ / ВЫТЕСНЕНИЕ -------------------
SCORE_W_VOL = 0.30
SCORE_W_DEPTH = 0.20
SCORE_W_ROOM = 0.25
SCORE_W_ALIGN = 0.15
SCORE_W_COST = 0.25                    
SCORE_CONFLUENCE = 0.10
PREEMPT_ENABLED = True
PREEMPT_SCORE_RATIO = 1.25
PREEMPT_MIN_AGE_BARS = 1
CONFLUENCE_SIZE_MULT = 1.00

# ------------------- КЛАСТЕРЫ КОРРЕЛЯЦИИ -------------------
CLUSTERS = {
    "MAJORS": ("BTCUSDT", "ETHUSDT"),
    "LARGE": ("SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT",
              "LINKUSDT", "LTCUSDT", "BCHUSDT", "ETCUSDT", "ATOMUSDT", "XLMUSDT",
              "HBARUSDT", "ALGOUSDT", "VETUSDT", "ICPUSDT", "FILUSDT", "NEARUSDT",
              "APTUSDT", "SUIUSDT", "TRXUSDT", "POLUSDT", "UNIUSDT", "AAVEUSDT",
              "KASUSDT"),
    "MEME": ("DOGEUSDT", "PEPEUSDT", "WIFUSDT", "BONKUSDT", "FLOKIUSDT",
             "SHIBUSDT", "JUPUSDT", "TIAUSDT", "SEIUSDT", "INJUSDT", "ENAUSDT",
             "PUMPUSDT", "WLFIUSDT", "SUSDT", "ASTERUSDT", "NIGHTUSDT",
             "FARTCOINUSDT"),
    "AI": ("FETUSDT", "RENDERUSDT", "TAOUSDT", "GRTUSDT", "WLDUSDT",
           "VIRTUALUSDT", "ARKMUSDT"),
    "DEFI": ("CRVUSDT", "LDOUSDT", "ARBUSDT", "OPUSDT", "PENDLEUSDT",
             "DYDXUSDT", "ONDOUSDT"),
    "OTHER": ("ZECUSDT", "HYPEUSDT", "XPLUSDT"),
}
_CLUSTER_MAP = {s: c for c, arr in CLUSTERS.items() for s in arr}
def cluster_of(symbol):
    return _CLUSTER_MAP.get(symbol, "OTHER")

# ------------------- УТИЛИТЫ -------------------

def dbg(text):
    if DEBUG_API:
        try:
            log_to_file("[DEBUG] " + str(text)[:700])
        except Exception:
            pass

def state_load():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as ex:
        log_to_file(f"[LOAD FAILED] {ex} — файл битый? path={STATE_FILE}")
        return {}

def state_save(data):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)   # атомарно: kill посреди записи не оставит битый файл
        return True
    except Exception as ex:
        log_to_file(f"[SAVE FAILED] {ex} path={STATE_FILE}")
        return False

def round_step(value, step):
    try:
        if not step or step <= 0:
            return value
        s = Decimal(str(step))
        n = (Decimal(str(value)) / s).to_integral_value(rounding=ROUND_DOWN)
        return float(n * s)
    except Exception:
        return value

def qstr(x):
    s = f"{float(x):.10f}".rstrip("0").rstrip(".")
    return s if s else "0"

def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))

def _sgn(side):
    return 1 if side == "LONG" else -1

def new_client_id(prefix="sfp"):
    return f"{prefix}{int(time.time() * 1000)}"

def extract_order_id(res):
    """orderId из любого формата ответа: dict / вложенность / список / скаляр."""
    if res is None:
        return None
    if isinstance(res, (str, int)):
        s = str(res).strip()
        return s or None
    if isinstance(res, (list, tuple)):
        for it in res:
            v = extract_order_id(it)
            if v:
                return v
        return None
    if isinstance(res, dict):
        for k in ("orderId", "order_id", "orderID", "id"):
            v = res.get(k)
            if isinstance(v, (str, int)) and str(v).strip():
                return str(v).strip()
        for k in ("order", "data", "result"):
            v = extract_order_id(res.get(k))
            if v:
                return v
    return None

def is_reduce(o):
    return o.get("reduceOnly") in (True, "true", "True", 1, "1")

def order_ids(o):
    oid = o.get("orderId") or o.get("id") or ""
    cid = o.get("clientOrderId") or o.get("orderLinkId") or ""
    return str(oid), str(cid)

DEAD_STATUSES = ("filled", "cancelled", "canceled", "rejected",
                 "expired", "deactivated", "failed")
def order_alive(o):
    st = str(o.get("status") or o.get("orderStatus") or "").lower()
    return st not in DEAD_STATUSES

def tf_ms(label):
    return TF_MINUTES[label] * 60 * 1000

# ------------------- ИНДИКАТОРЫ -------------------
def true_ranges(bars):
    out = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i][2], bars[i][3], bars[i - 1][4]
        out.append(max(h - l, abs(h - pc), abs(l - pc)))
    return out

def atr_last(bars, period=ATR_PERIOD):
    tr = true_ranges(bars)
    if len(tr) < period:
        return None
    a = sum(tr[:period]) / period
    for x in tr[period:]:
        a = (a * (period - 1) + x) / period
    return a

def ema(values, period):
    if len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e

def ema_series(values, n):
    if len(values) < n:
        return []
    k = 2.0 / (n + 1)
    out, e = [], values[0]
    for v in values:
        e = v * k + e * (1 - k)
        out.append(e)
    return out

def calc_tps(entry, risk, side, offsets):
    if side == "LONG":
        return [entry + o * risk for o in offsets]
    return [entry - o * risk for o in offsets]

def cost_floor_frac(spread):
    sp = spread if (spread and spread > 0) else 0.0002
    return COST_STOP_MULT * (FEE_MAKER + FEE_TAKER + sp)

def cost_ratio(stop_frac, spread):
    if stop_frac <= 0:
        return 1.0
    sp = spread if (spread and spread > 0) else 0.0002
    return (FEE_MAKER + FEE_TAKER + sp) / stop_frac

def score_signal(f):
    s = (SCORE_W_VOL * clamp(f.get("vol_z", 0.0))
         + SCORE_W_DEPTH * clamp(f.get("depth", 0.0))
         + SCORE_W_ROOM * clamp(f.get("room", 0.0))
         + SCORE_W_ALIGN * clamp(f.get("align", 0.0))
         - SCORE_W_COST * clamp(f.get("cost", 0.0)))
    if f.get("confluence"):
        s += SCORE_CONFLUENCE
    return s

MOSCOW_HINT = ("⏰ v5 CLEAN: торговля 24/7. Лимит входа ждёт 3 свечи. "
               "Сигналы отбираются по скорингу всей корзины. Логи в Download/SFP_Bot/")
# =====================================================================
#  ЧАСТЬ 2/5: API-слой. Единый интерфейс для Bybit и BingX.
# =====================================================================
PUBLIC_OK_PATHS = ("/v5/market/", "/openApi/swap/v2/quote/", "/openApi/swap/v3/quote/")
RATE_HINTS = ("too many", "rate", "429", "稍后", "retry", "frequent", "limit")

def _is_public(path):
    return any(path.startswith(p) for p in PUBLIC_OK_PATHS)

def _f(x, default=0.0):
    try:
        if x is None or x == "":
            return default
        return float(x)
    except Exception:
        return default


class BybitAPI:
    def __init__(self, api_key, secret, demo, interval):
        self.api_key, self.secret = api_key, secret
        self.base = BYBIT_DEMO if demo else BYBIT_REAL
        self.interval = interval
        self.name = "Bybit"
        self.aliases = BYBIT_ALIASES
        self.reverse = {v: k for k, v in BYBIT_ALIASES.items()}
        self._lots = None
        self._kcache = {}
        self._tick = (0.0, {})
        self.last_error = None

    def sym(self, s):
        return self.aliases.get(s, s)
    def to_universal(self, s):
        return self.reverse.get(s, s.replace("-", ""))

    def _headers(self, payload):
        ts = str(int(time.time() * 1000))
        sign = hmac.new(self.secret.encode(),
                        (ts + self.api_key + "5000" + payload).encode(),
                        hashlib.sha256).hexdigest()
        return {"X-BAPI-API-KEY": self.api_key,
                "X-BAPI-TIMESTAMP": ts,
                "X-BAPI-RECV-WINDOW": "5000",
                "X-BAPI-SIGN": sign,
                "Content-Type": "application/json"}

    def req(self, method, path, params=None):
        params = params or {}
        auth = bool(self.api_key and self.secret) or not _is_public(path)
        last_ex = None
        for attempt in range(4):
            try:
                if method == "GET":
                    qs = urllib.parse.urlencode(params)
                    h = self._headers(qs) if auth else {}
                    r = requests.get(f"{self.base}{path}?{qs}", headers=h, timeout=15)
                else:
                    body = json.dumps(params)
                    h = self._headers(body) if auth else {"Content-Type": "application/json"}
                    r = requests.post(f"{self.base}{path}", data=body, headers=h, timeout=15)
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as ex:
                last_ex = ex
                time.sleep(1.5 * (attempt + 1))
                continue
            if not r.text.strip():
                raise RuntimeError("пустой ответ — проверьте ключ и права")
            d = r.json()
            code = d.get("retCode")
            if code != 0:
                msg = str(d.get("retMsg") or "")
                if any(k in msg.lower() for k in RATE_HINTS) and attempt < 3:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"Bybit: {msg} ({code})")
            self.last_error = None
            return d.get("result")
        if last_ex is not None:
            raise RuntimeError(f"сеть: {last_ex}")
        raise RuntimeError("Bybit: лимит запросов — операция пропущена")

    def _load_lots(self):
        if self._lots is not None:
            return self._lots
        try:
            d = self.req("GET", "/v5/market/instruments-info", {"category": "linear"})
            lots = {}
            for it in (d or {}).get("list", []):
                f = it.get("lotSizeFilter", {})
                pf = it.get("priceFilter", {})
                lots[it["symbol"]] = {
                    "min": _f(f.get("minOrderQty") or f.get("minQty")),
                    "qstep": _f(f.get("qtyStep")),
                    "tick": _f(pf.get("tickSize")),
                    "max": _f(f.get("maxMktOrderQty")),
                    "min_usdt": 0.0,
                }
            self._lots = lots
        except Exception as ex:
            self.last_error = str(ex)
            self._lots = None
        return self._lots or {}

    def lot_info(self, symbol):
        info = self._load_lots().get(self.sym(symbol))
        if info is not None:
            return info
        try:
            d = self.req("GET", "/v5/market/instruments-info",
                         {"category": "linear", "symbol": self.sym(symbol)})
            for it in (d or {}).get("list", []):
                f = it.get("lotSizeFilter", {})
                pf = it.get("priceFilter", {})
                info = {"min": _f(f.get("minOrderQty")), "qstep": _f(f.get("qtyStep")),
                        "tick": _f(pf.get("tickSize")), "max": _f(f.get("maxMktOrderQty")),
                        "min_usdt": 0.0}
                if self._lots is None:
                    self._lots = {}
                self._lots[it["symbol"]] = info
        except Exception:
            return None
        return info

    def qty_ok(self, symbol, qty, price=None):
        info = self.lot_info(symbol)
        if not info:
            return round(qty, 3)
        qty = round_step(qty, info["qstep"])
        if info.get("max") and qty > info["max"]:
            qty = round_step(info["max"], info["qstep"])
        return qty if qty >= info["min"] else 0.0

    def price_ok(self, symbol, price):
        if not isinstance(price, (int, float)):
            return None
        info = self.lot_info(symbol)
        if not info or not info.get("tick"):
            return round(price, 8)
        return round_step(price, info["tick"])

    def _parse_klines(self, rows):
        out = []
        for k in rows or []:
            if isinstance(k, (list, tuple)) and len(k) >= 6:
                out.append((_f(k[0]), _f(k[1]), _f(k[2]), _f(k[3]), _f(k[4]), _f(k[5])))
            elif isinstance(k, dict):
                t = _f(k.get("time", k.get("t", k.get("startTime", 0))))
                out.append((t, _f(k.get("open", k.get("o"))), _f(k.get("high", k.get("h"))),
                            _f(k.get("low", k.get("l"))), _f(k.get("close", k.get("c"))),
                            _f(k.get("volume", k.get("v")))))
        out = [r for r in out if r[0] > 0 and r[4] > 0]
        out.sort(key=lambda x: x[0])
        ded = []
        for r in out:
            if not ded or r[0] != ded[-1][0]:
                ded.append(r)
        return ded

    def klines(self, symbol, limit=WORK_BARS, interval=None, max_age_ms=0):
        iv = interval or self.interval
        key = (self.sym(symbol), str(iv), int(limit))
        now_ms = int(time.time() * 1000)
        hit = self._kcache.get(key)
        if hit and (now_ms - hit[0]) < max_age_ms:
            return hit[1]
        d = self.req("GET", "/v5/market/kline",
                     {"category": "linear", "symbol": self.sym(symbol),
                      "interval": str(iv), "limit": int(limit)})
        bars = self._parse_klines((d or {}).get("list", []))
        self._kcache[key] = (now_ms, bars)
        return bars

    def tickers(self, symbols, max_age_ms=10000):
        now = time.time()
        if self._tick[1] and (now - self._tick[0]) * 1000 < max_age_ms:
            return self._tick[1]
        out = {}
        try:
            d = self.req("GET", "/v5/market/tickers", {"category": "linear"})
            for t in (d or {}).get("list", []):
                last = _f(t.get("lastPrice"))
                bid = _f(t.get("bid1Price"))
                ask = _f(t.get("ask1Price"))
                sp = (ask - bid) / ((ask + bid) / 2) if (bid > 0 and ask > bid) else 0.0
                out[self.to_universal(t.get("symbol", ""))] = {
                    "last": last, "bid": bid, "ask": ask, "spread": sp}
        except Exception as ex:
            self.last_error = str(ex)
            return self._tick[1] or {}
        self._tick = (now, out)
        return out

    def quote(self, symbol):
        t = self.tickers([symbol]).get(symbol)
        if t and t.get("last"):
            return t
        d = self.req("GET", "/v5/market/tickers",
                     {"category": "linear", "symbol": self.sym(symbol)})
        arr = (d or {}).get("list", [])
        if not arr:
            return None
        t = arr[0]
        bid, ask = _f(t.get("bid1Price")), _f(t.get("ask1Price"))
        return {"last": _f(t.get("lastPrice")), "bid": bid, "ask": ask,
                "spread": (ask - bid) / ((ask + bid) / 2) if (bid > 0 and ask > bid) else 0.0}

    def price(self, symbol):
        q = self.quote(symbol)
        return q["last"] if q and q.get("last") else None

    def balance(self):
        for acct in ("UNIFIED", "CONTRACT"):
            try:
                d = self.req("GET", "/v5/account/wallet-balance", {"accountType": acct})
                for coin in (d or {}).get("list", [{}])[0].get("coin", []):
                    if coin.get("coin") == "USDT":
                        v = _f(coin.get("walletBalance"))
                        if v > 0:
                            return v
            except Exception:
                continue
        return 0.0

    def open_positions(self):
        try:
            d = self.req("GET", "/v5/position/list",
                         {"category": "linear", "settleCoin": "USDT"})
        except Exception as ex:
            self.last_error = str(ex)
            return None
        out = {}
        for p in (d or {}).get("list", []):
            size = _f(p.get("size"))
            if size == 0:
                continue
            out[self.to_universal(p["symbol"])] = {
                "side": "LONG" if p.get("side") == "Buy" else "SHORT",
                "qty": size, "entry": _f(p.get("avgPrice"))}
        return out

    def all_open_orders(self, symbol):
        out, ok = [], False
        want = self.sym(symbol).upper()
        for filt in ("Order", "StopOrder"):
            try:
                d = self.req("GET", "/v5/order/realtime",
                             {"category": "linear", "symbol": self.sym(symbol),
                              "openOnly": 0, "orderFilter": filt})
                ok = True
            except Exception as ex:
                self.last_error = str(ex)
                continue
            for o in (d or {}).get("list", []):
                if isinstance(o, dict) and str(o.get("symbol", "")).upper() == want:
                    out.append(o)
        return out if ok else None

    def open_reduce_orders(self, symbol):
        res = []
        for o in self.all_open_orders(symbol) or []:
            if is_reduce(o):
                oid, cid = order_ids(o)
                if oid or cid:
                    res.append((oid, cid))
        return res

    def open_entry_orders(self, symbol):
        orders = self.all_open_orders(symbol)
        if orders is None:
            return None
        res = []
        for o in orders:
            if is_reduce(o) or not order_alive(o):
                continue
            oid, cid = order_ids(o)
            if oid or cid:
                res.append((oid, cid))
        return res

    def pending_exit_prices(self, symbol):
        stop = tp = None
        for o in self.all_open_orders(symbol) or []:
            if not is_reduce(o):
                continue
            if o.get("triggerPrice") and stop is None:
                stop = _f(o.get("triggerPrice")) or None
            elif o.get("orderType") == "Limit" and o.get("price") and tp is None:
                tp = _f(o.get("price")) or None
        return stop, tp

    def cancel(self, symbol, order_id=None, client_id=None):
        p = {"category": "linear", "symbol": self.sym(symbol)}
        if order_id:
            p["orderId"] = str(order_id)
        elif client_id:
            p["orderLinkId"] = str(client_id)
        else:
            self.last_error = "нет orderId/orderLinkId"
            return False
        try:
            res = self.req("POST", "/v5/order/cancel", p)
            dbg(f"Bybit cancel {symbol} {p} -> {res}")
            self.last_error = None
            return True
        except Exception as ex:
            self.last_error = f"{type(ex).__name__}: {ex}"
            dbg(f"Bybit cancel FAIL {symbol} {p} -> {self.last_error}")
            return False

    def cancel_all(self, symbol):
        try:
            res = self.req("POST", "/v5/order/cancel-all",
                           {"category": "linear", "symbol": self.sym(symbol),
                            "orderFilter": "Order"})
            dbg(f"Bybit cancel-all {symbol} -> {res}")
            return True
        except Exception as ex:
            self.last_error = f"{type(ex).__name__}: {ex}"
            return False

    def order(self, symbol, side, qty, stop=None, tp=None, reduce_only=False,
              client_id=None):
        p = {"category": "linear", "symbol": self.sym(symbol),
             "side": side, "qty": qstr(qty), "positionIdx": 0}
        if client_id:
            p["orderLinkId"] = str(client_id)
        if stop:
            p["orderType"] = "Market"
            p["triggerPrice"] = qstr(stop)
            p["triggerBy"] = "LastPrice"
            p["triggerDirection"] = "2" if side == "Sell" else "1"
        elif tp:
            p["orderType"] = "Limit"
            p["price"] = qstr(tp)
            p["timeInForce"] = "GTC"
        else:
            p["orderType"] = "Market"
        if reduce_only:
            p["reduceOnly"] = True
        res = self.req("POST", "/v5/order/create", p)
        dbg(f"Bybit order {symbol} {p} -> {res}")
        return res


class BingXAPI:
    def __init__(self, api_key, secret, demo, interval):
        self.api_key, self.secret = api_key, secret
        self.base = BINGX_DEMO if demo else BINGX_REAL
        self.interval = interval
        self.name = "BingX"
        self.aliases = BINGX_ALIASES
        self.reverse = {v: k for k, v in BINGX_ALIASES.items()}
        self._lots = None
        self._kcache = {}
        self._tick = (0.0, {})
        self.last_error = None

    def sym(self, s):
        return self.aliases.get(s, s)
    def to_universal(self, s):
        return self.reverse.get(s, s.replace("-", ""))
    def fmt(self, symbol):
        s = self.sym(symbol)
        for q in ("USDT", "USDC", "BTC"):
            if s.endswith(q):
                return s[:-len(q)] + "-" + q
        return s

    def _query(self, params):
        p = dict(params)
        p["timestamp"] = str(int(time.time() * 1000))
        p["recvWindow"] = "5000"
        qs = urllib.parse.urlencode(sorted(p.items()))
        sign = hmac.new(self.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        return qs, sign

    def req(self, method, path, params=None):
        params = params or {}
        auth = bool(self.api_key and self.secret) or not _is_public(path)
        last_ex = None
        for attempt in range(4):
            try:
                if auth:
                    qs, sign = self._query(params)
                    url = f"{self.base}{path}?{qs}&signature={sign}"
                    headers = {"X-BX-APIKEY": self.api_key}
                else:
                    url = f"{self.base}{path}?{urllib.parse.urlencode(sorted(params.items()))}"
                    headers = {}
                r = requests.post(url, headers=headers, timeout=15) if method == "POST" \
                    else requests.delete(url, headers=headers, timeout=15) if method == "DELETE" \
                    else requests.get(url, headers=headers, timeout=15)
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as ex:
                last_ex = ex
                time.sleep(1.5 * (attempt + 1))
                continue
            if not r.text.strip():
                raise RuntimeError("пустой ответ — проверьте ключ и права")
            d = r.json()
            if d.get("code") != 0:
                msg = str(d.get("msg") or "")
                if any(k in msg.lower() for k in RATE_HINTS) and attempt < 3:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"BingX: {msg} ({d.get('code')})")
            self.last_error = None
            return d.get("data")
        if last_ex is not None:
            raise RuntimeError(f"сеть: {last_ex}")
        raise RuntimeError("BingX: лимит запросов — операция пропущена")

    @staticmethod
    def _unwrap(d, keys=("orders", "data", "list", "orderList")):
        for _ in range(3):
            if isinstance(d, dict):
                nxt = None
                for k in keys:
                    if k in d:
                        nxt = d[k]
                        break
                d = nxt if nxt is not None else []
            else:
                break
        return d if isinstance(d, (list, tuple)) else []

    def _load_lots(self):
        if self._lots is not None:
            return self._lots
        try:
            d = self.req("GET", "/openApi/swap/v2/quote/contracts", {})
            lots = {}
            for it in d or []:
                s = (it.get("symbol") or "").replace("-", "")
                pq = int(_f(it.get("quantityPrecision"), 8))
                pp = int(_f(it.get("pricePrecision"), 8))
                lots[s] = {"min": _f(it.get("tradeMinQuantity")),
                           "qstep": 10 ** (-pq) if pq > 0 else 1.0,
                           "tick": 10 ** (-pp) if pp > 0 else 0.0,
                           "max": 0.0,
                           "min_usdt": _f(it.get("tradeMinUSDT"))}
            self._lots = lots
        except Exception as ex:
            self.last_error = str(ex)
            self._lots = None
        return self._lots or {}

    def qty_ok(self, symbol, qty, price=None):
        info = self._load_lots().get(self.sym(symbol))
        if info is None:
            self._lots = None
            info = self._load_lots().get(self.sym(symbol))
        if not info:
            return round(qty, 3)
        qty = round_step(qty, info["qstep"])
        if qty < info["min"]:
            return 0.0
        if price and info.get("min_usdt") and qty * price < info["min_usdt"]:
            return 0.0
        return qty

    def price_ok(self, symbol, price):
        if not isinstance(price, (int, float)):
            return None
        info = self._load_lots().get(self.sym(symbol))
        if info is None:
            self._lots = None
            info = self._load_lots().get(self.sym(symbol))
        if not info or not info.get("tick"):
            return round(price, 8)
        return round_step(price, info["tick"])

    def klines(self, symbol, limit=WORK_BARS, interval=None, max_age_ms=0):
        iv = interval or self.interval
        key = (self.sym(symbol), str(iv), int(limit))
        now_ms = int(time.time() * 1000)
        hit = self._kcache.get(key)
        if hit and (now_ms - hit[0]) < max_age_ms:
            return hit[1]
        d = self.req("GET", "/openApi/swap/v3/quote/klines",
                     {"symbol": self.fmt(symbol), "interval": str(iv),
                      "limit": int(limit)})
        rows = self._unwrap(d, ("data", "list", "o"))
        if not rows and isinstance(d, list):
            rows = d
        out = []
        for k in rows:
            if isinstance(k, (list, tuple)) and len(k) >= 6:
                out.append((_f(k[0]), _f(k[1]), _f(k[2]), _f(k[3]), _f(k[4]), _f(k[5])))
            elif isinstance(k, dict):
                t = _f(k.get("time", k.get("t", k.get("startTime", 0))))
                out.append((t, _f(k.get("open", k.get("o"))), _f(k.get("high", k.get("h"))),
                            _f(k.get("low", k.get("l"))), _f(k.get("close", k.get("c"))),
                            _f(k.get("volume", k.get("v")))))
        out = [r for r in out if r[0] > 0 and r[4] > 0]
        out.sort(key=lambda x: x[0])
        ded = []
        for r in out:
            if not ded or r[0] != ded[-1][0]:
                ded.append(r)
        self._kcache[key] = (now_ms, ded)
        return ded

    def tickers(self, symbols, max_age_ms=10000):
        now = time.time()
        if self._tick[1] and (now - self._tick[0]) * 1000 < max_age_ms:
            return self._tick[1]
        out = {}
        try:
            d = self.req("GET", "/openApi/swap/v2/quote/ticker", {})
            for t in self._unwrap(d, ("data", "list")):
                if not isinstance(t, dict):
                    continue
                uni = self.to_universal(t.get("symbol", ""))
                last = _f(t.get("lastPrice", t.get("price")))
                bid = _f(t.get("bidPrice", t.get("bid1Price")))
                ask = _f(t.get("askPrice", t.get("ask1Price")))
                sp = (ask - bid) / ((ask + bid) / 2) if (bid > 0 and ask > bid) else 0.0
                if last > 0:
                    out[uni] = {"last": last, "bid": bid, "ask": ask, "spread": sp}
        except Exception as ex:
            self.last_error = str(ex)
        if not out:
            for s in symbols:
                try:
                    q = self._quote_one(s)
                    if q:
                        out[s] = q
                except Exception:
                    continue
                time.sleep(0.12)
        if out:
            self._tick = (now, out)
        return out or self._tick[1]

    def _quote_one(self, symbol):
        d = self.req("GET", "/openApi/swap/v2/quote/ticker", {"symbol": self.fmt(symbol)})
        arr = self._unwrap(d, ("data", "list"))
        if not arr and isinstance(d, dict):
            arr = [d]
        if not arr:
            return None
        t = arr[0]
        last = _f(t.get("lastPrice", t.get("price")))
        bid = _f(t.get("bidPrice", t.get("bid1Price")))
        ask = _f(t.get("askPrice", t.get("ask1Price")))
        if last <= 0:
            return None
        return {"last": last, "bid": bid, "ask": ask,
                "spread": (ask - bid) / ((ask + bid) / 2) if (bid > 0 and ask > bid) else 0.0}

    def quote(self, symbol):
        t = self.tickers([symbol]).get(symbol)
        if t and t.get("last"):
            return t
        return self._quote_one(symbol)

    def price(self, symbol):
        q = self.quote(symbol)
        return q["last"] if q and q.get("last") else None

    def balance(self):
        d = self.req("GET", "/openApi/swap/v2/user/balance", {})
        dbg(f"BingX balance raw: {str(d)[:300]}")
        if isinstance(d, dict):
            inner = d.get("balance")
            src = inner if isinstance(inner, dict) else d
            for key in ("balance", "equity", "availableMargin", "available"):
                v = _f(src.get(key))
                if v > 0:
                    return v
        if isinstance(d, list):
            for coin in d:
                if str(coin.get("asset") or coin.get("coin")) == "USDT":
                    for key in ("balance", "equity", "availableMargin", "available"):
                        v = _f(coin.get(key))
                        if v > 0:
                            return v
        return 0.0

    def open_positions(self):
        try:
            d = self.req("GET", "/openApi/swap/v2/user/positions", {})
        except Exception as ex:
            self.last_error = str(ex)
            return None
        out = {}
        for p in d or []:
            if not isinstance(p, dict):
                continue
            raw_amt = _f(p.get("positionAmt"))
            amt = abs(raw_amt)
            if amt == 0:
                continue
            ps = str(p.get("positionSide") or "").upper()
            if ps in ("LONG", "SHORT"):
                side = ps
            elif raw_amt < 0:
                side = "SHORT"
            else:
                side = str(p.get("side") or "LONG").upper()
            if side in ("BUY", "SELL"):
                side = "LONG" if side == "BUY" else "SHORT"
            out[self.to_universal(p.get("symbol", ""))] = {
                "side": side, "qty": amt, "entry": _f(p.get("avgPrice"))}
        return out

    def all_open_orders(self, symbol):
        try:
            d = self.req("GET", "/openApi/swap/v2/trade/openOrders",
                         {"symbol": self.fmt(symbol)})
        except Exception as ex:
            self.last_error = str(ex)
            return None
        want = self.fmt(symbol).upper()
        return [o for o in self._unwrap(d) if isinstance(o, dict)
                and str(o.get("symbol", "")).upper() == want]

    def open_reduce_orders(self, symbol):
        res = []
        for o in self.all_open_orders(symbol) or []:
            if is_reduce(o):
                oid, cid = order_ids(o)
                if oid or cid:
                    res.append((oid, cid))
        return res

    def open_entry_orders(self, symbol):
        orders = self.all_open_orders(symbol)
        if orders is None:
            return None
        res = []
        for o in orders:
            if is_reduce(o) or not order_alive(o):
                continue
            oid, cid = order_ids(o)
            if oid or cid:
                res.append((oid, cid))
        return res

    def pending_exit_prices(self, symbol):
        stop = tp = None
        for o in self.all_open_orders(symbol) or []:
            if not is_reduce(o):
                continue
            otype = str(o.get("type") or "").upper()
            sp = o.get("stopPrice") or o.get("triggerPrice")
            if "TRIGGER" in otype and sp:
                stop = _f(sp) or None
            elif otype == "LIMIT" and o.get("price"):
                tp = _f(o.get("price")) or None
        return stop, tp

    def cancel(self, symbol, order_id=None, client_id=None):
        p = {"symbol": self.fmt(symbol)}
        if order_id:
            p["orderId"] = str(order_id)
        elif client_id:
            p["clientOrderId"] = str(client_id)
        else:
            self.last_error = "нет orderId/clientOrderId"
            return False
        try:
            res = self.req("DELETE", "/openApi/swap/v2/trade/order", p)
            dbg(f"BingX cancel {symbol} {p} -> {res}")
            self.last_error = None
            return True
        except Exception as ex:
            self.last_error = f"{type(ex).__name__}: {ex}"
            dbg(f"BingX cancel FAIL {symbol} {p} -> {self.last_error}")
            return False

    def cancel_all(self, symbol):
        try:
            res = self.req("DELETE", "/openApi/swap/v2/trade/allOpenOrders",
                           {"symbol": self.fmt(symbol)})
            dbg(f"BingX cancel-all {symbol} -> {res}")
            return True
        except Exception as ex:
            self.last_error = f"{type(ex).__name__}: {ex}"
            return False

    def order(self, symbol, side, qty, stop=None, tp=None, reduce_only=False,
              client_id=None):
        p = {"symbol": self.fmt(symbol),
             "side": "BUY" if side == "Buy" else "SELL",
             "positionSide": "BOTH",
             "quantity": qstr(qty)}
        if client_id:
            p["clientOrderId"] = str(client_id)
        if stop:
            p["type"] = "TRIGGER_MARKET"
            p["stopPrice"] = qstr(stop)
            p["workingType"] = "MARK_PRICE"
        elif tp:
            p["type"] = "LIMIT"
            p["price"] = qstr(tp)
            p["timeInForce"] = "GTC"
        else:
            p["type"] = "MARKET"
        if reduce_only:
            p["reduceOnly"] = "true"
        res = self.req("POST", "/openApi/swap/v2/trade/order", p)
        dbg(f"BingX order {symbol} {p} -> {res}")
        return res
# =====================================================================
#  ЧАСТЬ 3/5: режим рынка и генераторы сигналов.
# =====================================================================
class Signals:
    def __init__(self, api, tf_label, trend_label, ema_prox_pct, log):
        self.api = api
        self.tf_label = tf_label
        self.vol_mult = TIMEFRAMES[tf_label]["vol_mult"]
        self.trend_enabled = (trend_label != EMA_OFF)
        self.trend_label = trend_label
        self.ema_prox = ema_prox_pct
        self.log = log
        key = ("interval_bybit" if api.name == "Bybit" else "interval_bingx")
        self.trend_interval = TIMEFRAMES[trend_label][key] if self.trend_enabled else None
        self.range_bars = max(30, RANGE_DAYS * 1440 // TF_MINUTES[tf_label])
        self._ctx = {}
        self._diag = {}

    def _once_hour(self, key, msg):
        now = time.time()
        if now - self._diag.get(key, 0) > 3600:
            self._diag[key] = now
            self.log(msg)

    def _sw(self, bars):
        return max(b[2] for b in bars), min(b[3] for b in bars)

    def _stop_dist(self, entry, atr, mult, spread):
        return max(atr * mult, entry * cost_floor_frac(spread))

    def _no_chase(self, sig, reg):
        e = reg.get("ema")
        if not e or e <= 0 or self.ema_prox <= 0:
            return True
        dev = (sig["entry"] - e) / e * 100.0
        if sig["kind"] == "TREND_A":
            if sig["side"] == "LONG" and dev <= 0:
                return False
            if sig["side"] == "SHORT" and dev >= 0:
                return False
        return abs(dev) <= self.ema_prox

    def _mk(self, symbol, kind, side, entry, stop, tps, splits, tail, be,
            size_mult, feats, note=""):
        entry = self.api.price_ok(symbol, entry)
        stop = self.api.price_ok(symbol, stop)
        if not isinstance(entry, (int, float)) or not isinstance(stop, (int, float)):
            return None
        risk = abs(entry - stop)
        if risk <= 0 or entry <= 0:
            return None
        sf = risk / entry
        if sf + 1e-9 < cost_floor_frac(feats.get("_spread", 0.0)):
            return None
        tps = [self.api.price_ok(symbol, t) for t in tps]
        if any(t is None for t in tps):
            return None
        sig = {"symbol": symbol, "kind": kind, "side": side,
               "entry": entry, "stop": stop, "risk": risk, "stop_frac": sf,
               "tps": tps, "splits": splits, "tail": tail, "be": be,
               "size_mult": size_mult, "feats": feats, "note": note,
               "confluence": False}
        sig["score"] = score_signal(feats)
        return sig

    def context(self, symbol):
        now = time.time()
        hit = self._ctx.get(symbol)
        if hit and now - hit[0] < 20:
            return hit[1]
        bars = self.api.klines(symbol, limit=WORK_BARS, max_age_ms=20000)
        closed = bars[:-1] if len(bars) > 1 else []
        if len(closed) < max(CHOCH_SWING + 2, ATR_PERIOD + 2):
            self._once_hour(("bars", symbol),
                            f"{symbol}: мало свечей ({len(closed)}) — сигналы не ищем")
            return None
        atr = atr_last(closed)
        if not atr or atr <= 0:
            return None
        q = self.api.quote(symbol) or {}
        spread = q.get("spread") or 0.0
        if spread * 100 > MAX_SPREAD_PCT:
            self._once_hour(("spread", symbol),
                            f"{symbol}: спред {spread * 100:.3f}% > {MAX_SPREAD_PCT}% — пропуск")
            return None
        if self.trend_enabled:
            hb = self.api.klines(symbol, limit=HTF_BARS,
                                 interval=self.trend_interval, max_age_ms=60000)
            hb = hb[:-1] if len(hb) > 1 else []
        else:
            hb = closed
        closes = [b[4] for b in hb]
        ser = ema_series(closes, EMA_PERIOD)
        hatr = atr_last(hb) or atr
        if len(ser) > REGIME_LOOKBACK and hatr > 0:
            slope = ser[-1] - ser[-1 - REGIME_LOOKBACK]
            strength = abs(slope) / hatr
            ema_now = ser[-1]
            direction = (1 if slope > 0 else -1) if strength > REGIME_THRESHOLD else 0
            if direction and ema_now > 0:
                if (direction > 0 and closes[-1] < ema_now) or \
                        (direction < 0 and closes[-1] > ema_now):
                    direction = 0
        else:
            strength, direction, ema_now = 0.0, 0, (ser[-1] if ser else None)
        ctx = {"closed": closed, "last": closed[-1], "atr": atr,
               "spread": spread, "last_price": q.get("last") or closed[-1][4],
               "regime": {"dir": direction, "strength": strength,
                          "strong": direction != 0, "ema": ema_now}}
        self._ctx[symbol] = (now, ctx)
        return ctx

    def range_v5(self, symbol, ctx):
        closed, last, atr = ctx["closed"], ctx["last"], ctx["atr"]
        n = self.range_bars
        if len(closed) < n + 1:
            return None
        hist = closed[-(n + 1):-1]
        upper, lower = self._sw(hist)
        if upper <= lower:
            return None
        avg = sum(b[5] for b in hist) / len(hist)
        tol = RANGE_TOUCH_TOL
        tu = sum(1 for b in hist if b[2] >= upper * (1 - tol))
        td = sum(1 for b in hist if b[3] <= lower * (1 + tol))
        vol_ok = last[5] > RANGE_VOL_MULT * avg
        rev_up = (vol_ok and tu >= RANGE_MIN_TOUCHES and
                  last[2] >= upper * (1 - tol) and last[4] < upper * (1 - tol))
        rev_dn = (vol_ok and td >= RANGE_MIN_TOUCHES and
                  last[3] <= lower * (1 + tol) and last[4] > lower * (1 + tol))
        if not (rev_up or rev_dn):
            return None
        side = "SHORT" if rev_up else "LONG"
        d = ctx["regime"]["dir"]
        if d > 0 and side == "SHORT":
            return None
        if d < 0 and side == "LONG":
            return None
        rng = max(last[2] - last[3], atr * 0.1)
        entry = last[4] + RANGE_ENTRY_RETRACE * rng if side == "SHORT" else \
            last[4] - RANGE_ENTRY_RETRACE * rng
        boundary = upper if side == "SHORT" else lower
        beyond = max(last[2], upper) if side == "SHORT" else min(last[3], lower)
        dist = self._stop_dist(entry, atr, RANGE_STOP_ATR, ctx["spread"])
        stop = beyond + dist if side == "SHORT" else beyond - dist
        risk = abs(entry - stop)
        if risk <= 0:
            return None
        width = upper - lower
        if width / risk < RANGE_MIN_WIDTH_R:
            return None
        sgn = 1 if side == "LONG" else -1
        tp1 = (upper + lower) / 2.0
        far = upper if side == "LONG" else lower        # ДАЛЬНЯЯ граница
        tp2 = entry + sgn * abs(far - entry) * RANGE_TARGET_FRAC
        r1, r2 = abs(tp1 - entry) / risk, abs(tp2 - entry) / risk
        if r1 < RANGE_MIN_TP1_R or r2 < RANGE_MIN_TP2_R:
            return None
        feats = {"vol_z": (last[5] / avg - 1) / 2 if avg > 0 else 0.0,
                 "depth": (abs(beyond - boundary) / atr) / 1.5,
                 "room": r2 / 3.0,
                 "align": 1.0 if (d != 0 and ((d > 0) == (side == "LONG"))) else 0.5,
                 "cost": cost_ratio(risk / entry, ctx["spread"]) / 0.15,
                 "_spread": ctx["spread"]}
        return self._mk(symbol, "RANGE", side, entry, stop, [tp1, tp2],
                        [RANGE_TP1_SHARE, RANGE_TP2_SHARE], 0.0,
                        RANGE_BE_AFTER_TP1, 1.0, feats,
                        f"канал {width / risk:.1f}R, цели {r1:.2f}R/{r2:.2f}R")

    def trend_a(self, symbol, ctx):
        closed, last, atr = ctx["closed"], ctx["last"], ctx["atr"]
        if len(closed) < SFP_SWING + 1:
            return None
        win = closed[-(SFP_SWING + 1):-1]
        sw_hi, sw_lo = self._sw(win)
        avg = sum(b[5] for b in win) / len(win)
        if last[5] <= self.vol_mult * avg:
            return None
        long_ok = last[3] < sw_lo and last[4] > sw_lo
        short_ok = last[2] > sw_hi and last[4] < sw_hi
        d = ctx["regime"]["dir"]
        if d > 0:
            short_ok = False
        elif d < 0:
            long_ok = False
        if not (long_ok or short_ok):
            return None
        side = "LONG" if long_ok else "SHORT"
        entry = sw_lo if side == "LONG" else sw_hi
        beyond = min(last[3], sw_lo) if side == "LONG" else max(last[2], sw_hi)
        dist = self._stop_dist(entry, atr, SFP_STOP_ATR, ctx["spread"])
        stop = beyond - dist if side == "LONG" else beyond + dist
        risk = abs(entry - stop)
        if risk <= 0:
            return None
        offsets = [r for r, _ in TP_TREND]
        splits = [w for _, w in TP_TREND]
        tps = calc_tps(entry, risk, side, offsets)
        feats = {"vol_z": (last[5] / avg - 1) / 2 if avg > 0 else 0.0,
                 "depth": (abs(beyond - entry) / atr) / 1.5,
                 "room": offsets[-1] / 3.0,
                 "align": 1.0 if d != 0 else 0.5,
                 "cost": cost_ratio(risk / entry, ctx["spread"]) / 0.15,
                 "_spread": ctx["spread"]}
        return self._mk(symbol, "TREND_A", side, entry, stop, tps, splits,
                        TREND_TAIL_SHARE, TREND_BE_AFTER_TP1, 1.0, feats,
                        f"свинг {SFP_SWING}, стоп {risk / entry * 100:.2f}%")

    def trend_b(self, symbol, ctx):
        closed, last, atr = ctx["closed"], ctx["last"], ctx["atr"]
        if len(closed) < CHOCH_SWING + 2:
            return None
        major = closed[-(CHOCH_SWING + 1):-1]
        minor = closed[-(CHOCH_MINOR + 1):-1]
        mj_hi, mj_lo = self._sw(major)
        mn_hi, mn_lo = self._sw(minor)
        avg = sum(b[5] for b in major) / len(major)
        sweep_up = last[2] > mj_hi and last[4] < mj_hi
        sweep_dn = last[3] < mj_lo and last[4] > mj_lo
        side = None
        if sweep_up and last[4] < mn_lo:
            side = "SHORT"
        elif sweep_dn and last[4] > mn_hi:
            side = "LONG"
        if side is None:
            return None
        closes = [b[4] for b in closed]
        ef, es = ema_series(closes, EMA_FAST), ema_series(closes, EMA_PERIOD)
        if not ef or not es:
            return None
        slope = es[-1] - es[-2] if len(es) > 1 else 0.0
        conf = (ef[-1] < es[-1] and slope <= 0) if side == "SHORT" else \
            (ef[-1] > es[-1] and slope >= 0)
        if not conf:
            return None
        entry = mn_lo if side == "SHORT" else mn_hi
        beyond = last[2] if side == "SHORT" else last[3]
        dist = self._stop_dist(entry, atr, CHOCH_STOP_ATR, ctx["spread"])
        stop = beyond + dist if side == "SHORT" else beyond - dist
        risk = abs(entry - stop)
        if risk <= 0:
            return None
        d = ctx["regime"]["dir"]
        counter = (d > 0 and side == "SHORT") or (d < 0 and side == "LONG")
        size = CHOCH_COUNTER_SIZE if counter else 1.0
        offsets = [r for r, _ in TP_TREND]
        splits = [w for _, w in TP_TREND]
        tps = calc_tps(entry, risk, side, offsets)
        feats = {"vol_z": (last[5] / avg - 1) / 2 if avg > 0 else 0.0,
                 "depth": (abs(beyond - (mj_hi if side == "SHORT" else mj_lo)) / atr) / 1.5,
                 "room": offsets[-1] / 3.0,
                 "align": 0.5 if counter else 1.0,
                 "cost": cost_ratio(risk / entry, ctx["spread"]) / 0.15,
                 "_spread": ctx["spread"]}
        return self._mk(symbol, "TREND_B", side, entry, stop, tps, splits,
                        TREND_TAIL_SHARE, TREND_BE_AFTER_TP1, size, feats,
                        f"CHoCH {CHOCH_SWING}, против режима={counter}")

    def resolve(self, symbol, ctx):
        r = self.range_v5(symbol, ctx)
        a = self.trend_a(symbol, ctx)
        b = self.trend_b(symbol, ctx)
        d = ctx["regime"]["dir"]
        strong = ctx["regime"]["strong"]
        t = None
        if a and b:
            if a["side"] != b["side"]:
                t = b
            else:
                t = a if (strong and d != 0) else b
        else:
            t = a or b
        out = None
        if r and t:
            if r["side"] != t["side"]:
                return []
            out = r if r["score"] >= t["score"] else t
            out["confluence"] = True
            out["feats"]["confluence"] = True
            out["score"] = score_signal(out["feats"])
        else:
            out = r or t
        if out is None:
            return []
        if not self._no_chase(out, ctx["regime"]):
            return []
        return [out]

    def candidates(self, symbol):
        ctx = self.context(symbol)
        if ctx is None:
            return []
        return self.resolve(symbol, ctx)
# =====================================================================
#  ЧАСТЬ 4/5: ядро бота — состояние, лимиты, отбор сигналов,
#  учёт сделок, исполнение ордеров и сопровождение позиций.
# =====================================================================
class Bot:
    def __init__(self, api, tf_label, trend_label, symbols, risk_pct,
                 max_losses_per_day, max_positions, log,
                 ema_proximity_pct=0.0, load_state=None, save_state=None):
        self.api = api
        self.tf_label = tf_label
        self.tf_sec = TF_MINUTES[tf_label] * 60
        self.symbols = symbols
        self.risk_pct = risk_pct
        self.max_losses = max_losses_per_day
        self.max_positions = max_positions
        self._log_ui = log
        self.log = self._log_both
        self._load_state = load_state
        self._save_state = save_state
        self.stop_event = threading.Event()
        self.sig = Signals(api, tf_label, trend_label, ema_proximity_pct, self.log)
        self.positions = {}
        self.equity = DRY_RUN_BALANCE
        self.dry_balance = DRY_RUN_BALANCE
        self.day = dt.date.today()
        self.losses_today = 0
        self.losses_by_pair = {}
        self.cooldown = {}
        self.day_r = 0.0
        self.day_usdt = 0.0
        self.stat_out = {}
        self.stat_kind = {}
        self.stat_signals = 0
        self.stat_taken = 0
        self.stat_preempt = 0
        self.last_stat_hour = None
        self._acted = {}
        self._rej = {}
        self._live_pos_ts = 0.0
        self._live_pos_cache = None

    def _log_both(self, msg):
        log_to_file(msg)
        self._log_ui(msg)

    def load_day(self):
        if not self._load_state:
            return
        st = self._load_state() or {}
        if st.get("date") != dt.date.today().isoformat():
            return
        self.losses_today = int(st.get("losses", 0))
        self.losses_by_pair = dict(st.get("losses_by_pair") or {})
        self.day_r = float(st.get("day_r", 0.0))
        self.day_usdt = float(st.get("day_usdt", 0.0))
        self.cooldown = {k: float(v) for k, v in (st.get("cooldown") or {}).items()}
        if self.losses_today or self.day_r:
            self.log(f"Восстановлено за сегодня: стопов {self.losses_today}, "
                     f"{self.day_r:+.2f}R ({self.day_usdt:+.2f} USDT)")

    def persist(self):
        if not self._save_state:
            return
        self._save_state({"date": dt.date.today().isoformat(),
                          "losses": self.losses_today,
                          "losses_by_pair": self.losses_by_pair,
                          "day_r": self.day_r, "day_usdt": self.day_usdt,
                          "cooldown": self.cooldown})

    def reset_day(self):
        self.day = dt.date.today()
        self.losses_today = 0
        self.losses_by_pair = {}
        self.cooldown = {}
        self.day_r = 0.0
        self.day_usdt = 0.0
        self.stat_out = {}
        self.stat_kind = {}
        self.stat_signals = 0
        self.stat_taken = 0
        self.stat_preempt = 0
        self.last_stat_hour = None
        self._rej = {}
        self.persist()
        self.log("Новый день — дневные лимиты и статистика сброшены")

    def log_stats(self):
        hour = dt.datetime.now().hour
        if self.last_stat_hour == hour:
            return
        self.last_stat_hour = hour
        kinds = "; ".join(f"{k}: {v[0]} сд., {v[1]:+.2f}R"
                          for k, v in sorted(self.stat_kind.items())) or "сделок нет"
        outs = ", ".join(f"{k} {v}" for k, v in sorted(self.stat_out.items())) or "нет"
        self.log(f"Итоги дня: сигналов {self.stat_signals}, входов {self.stat_taken}, "
                 f"вытеснений {self.stat_preempt}. {kinds}. Исходы: {outs}. "
                 f"День {self.day_r:+.2f}R ({self.day_usdt:+.2f} USDT)")

    def register_stop(self, symbol):
        self.losses_today += 1
        self.losses_by_pair[symbol] = self.losses_by_pair.get(symbol, 0) + 1
        if self.losses_by_pair[symbol] >= PAIR_COOLDOWN_STOPS:
            self.cooldown[symbol] = time.time() + PAIR_COOLDOWN_HOURS * 3600
            self.log(f"{symbol}: {self.losses_by_pair[symbol]} стопа за день — "
                     f"кулдаун {PAIR_COOLDOWN_HOURS} ч")
        self.persist()

    def pair_blocked(self, symbol):
        if self.losses_by_pair.get(symbol, 0) >= MAX_STOPS_PER_PAIR:
            return "лимит стопов на пару"
        if time.time() < self.cooldown.get(symbol, 0):
            return "кулдаун после стопов"
        return None

    def basket_blocked(self):
        if self.losses_today >= self.max_losses:
            return f"лимит стопов корзины {self.losses_today}/{self.max_losses}"
        if self.day_r <= DAILY_R_LIMIT:
            return f"дневной убыток {self.day_r:+.2f}R <= {DAILY_R_LIMIT}R"
        return None

    def open_risk(self):
        eq = self.equity or 1.0
        tot = lr = sr = 0.0
        for p in self.positions.values():
            r = p.get("risk_usdt", 0.0) / eq
            tot += r
            if p["side"] == "LONG":
                lr += r
            else:
                sr += r
        return tot, lr, sr

    def cluster_busy(self, symbol, side):
        c = cluster_of(symbol)
        for s, p in self.positions.items():
            if s != symbol and cluster_of(s) == c and p["side"] == side:
                return True
        return False

    def _portfolio_block(self, sig):
        s, side = sig["symbol"], sig["side"]
        if self.cluster_busy(s, side):
            return f"кластер {cluster_of(s)} уже занят в {side}"
        mult = sig["size_mult"]
        if sig.get("confluence"):
            mult *= CONFLUENCE_SIZE_MULT
        add = self.risk_pct * mult
        tot, lr, sr = self.open_risk()
        if tot + add > MAX_TOTAL_RISK + 1e-9:
            return f"суммарный риск {(tot + add) * 100:.2f}% > {MAX_TOTAL_RISK * 100:.1f}%"
        same = (lr if side == "LONG" else sr) + add
        if same > MAX_SAME_DIR_RISK + 1e-9:
            return f"риск в {side} {same * 100:.2f}% > {MAX_SAME_DIR_RISK * 100:.1f}%"
        return None

    def track_excursion(self, symbol, p, cur):
        if cur is None or p.get("risk", 0) <= 0:
            return
        sgn = 1 if p["side"] == "LONG" else -1
        r = (cur - p["entry"]) / p["risk"] * sgn
        p["mfe"] = max(p.get("mfe", 0.0), r)
        p["mae"] = min(p.get("mae", 0.0), r)

    def close_trade(self, symbol, p, category, cur=None):
        risk = p["risk"]
        qty = p.get("qty_total") or 0.0
        splits = p["splits"]
        filled = sum(p["tp_filled"])
        sgn = 1 if p["side"] == "LONG" else -1
        r_gross = 0.0
        for i in range(filled):
            r_gross += splits[i] * (p["tp_levels"][i] - p["entry"]) / risk * sgn
        remaining = max(0.0, 1.0 - sum(splits[:filled]))
        if cur is not None and remaining > 0:
            r_close = (cur - p["entry"]) / risk * sgn
        else:
            r_close = -1.0 if category == "СТОП" else 0.0
        r_gross += remaining * r_close
        gross = r_gross * risk * qty
        fees = qty * p["entry"] * FEE_MAKER
        for i in range(filled):
            fees += qty * splits[i] * p["tp_levels"][i] * FEE_MAKER
        if remaining > 0 and cur is not None:
            fees += qty * remaining * cur * FEE_TAKER
        net = gross - fees
        r_net = net / (risk * qty) if risk * qty > 0 else r_gross
        self.day_r += r_net
        self.day_usdt += net
        if DRY_RUN:
            self.dry_balance += net
        self.stat_out[category] = self.stat_out.get(category, 0) + 1
        a = self.stat_kind.setdefault(p.get("kind", "?"), [0, 0.0])
        a[0] += 1
        a[1] += r_net
        self.persist()
        self.log(f"Итог {symbol} [{p.get('kind')}/{p.get('subtype', '')}]: {category}, "
                 f"{r_gross:+.2f}R брутто / {r_net:+.2f}R нетто "
                 f"({net:+.2f} USDT, комиссии {fees:.2f}), "
                 f"тейков {filled}/{len(splits)}, MAE {p.get('mae', 0):.2f}R / "
                 f"MFE {p.get('mfe', 0):.2f}R. День {self.day_r:+.2f}R")
        self._csv_trade(symbol, p, category, cur, r_gross, r_net, net, fees)

    def _csv_trade(self, symbol, p, category, cur, r_gross, r_net, usdt, fees):
        try:
            new = not os.path.exists(TRADES_FILE)
            with open(TRADES_FILE, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["ts", "symbol", "kind", "side", "entry", "stop0",
                                "risk0", "qty", "tp1", "tp2", "tps_filled", "exit",
                                "category", "r_gross", "r_net", "usdt_net", "fees",
                                "mae_R", "mfe_R", "score", "confluence",
                                "vol_z", "depth", "room", "align", "cost"])
                ft_ = p.get("feats") or {}
                w.writerow([dt.datetime.now().isoformat(timespec="seconds"),
                            symbol, p.get("kind"), p["side"], qstr(p["entry"]),
                            qstr(p.get("stop0") or 0), qstr(p["risk"]),
                            qstr(p.get("qty_total") or 0), qstr(p["tp_levels"][0]),
                            qstr(p["tp_levels"][-1]), sum(p["tp_filled"]),
                            qstr(cur) if cur is not None else "", category,
                            f"{r_gross:.4f}", f"{r_net:.4f}", f"{usdt:.4f}",
                            f"{fees:.4f}", f"{p.get('mae', 0):.3f}",
                            f"{p.get('mfe', 0):.3f}", f"{p.get('score', 0):.3f}",
                            int(bool(p.get("confluence"))),
                            f"{ft_.get('vol_z', 0):.3f}", f"{ft_.get('depth', 0):.3f}",
                            f"{ft_.get('room', 0):.3f}", f"{ft_.get('align', 0):.3f}",
                            f"{ft_.get('cost', 0):.3f}"])
        except Exception as ex:
            dbg(f"CSV trade write fail: {ex}")

    def _bar_t(self, symbol):
        hit = self.sig._ctx.get(symbol)
        return hit[1]["last"][0] if hit else 0

    def _can_preempt(self, old, sig):
        if old.get("phase") != "WAIT_FILL" or old.get("preempted"):
            return False
        if (time.time() - old.get("placed_ts", 0)) / self.tf_sec < PREEMPT_MIN_AGE_BARS:
            return False
        return sig["score"] > old.get("score", 0.0) * PREEMPT_SCORE_RATIO

    def scan_signals(self):
        block = self.basket_blocked()
        if block:
            self.log(f"Новые входы запрещены: {block}")
            return
        self.equity = self.dry_balance if DRY_RUN else (self.api.balance() or self.equity)
        if self.equity <= 0:
            self.log("Баланс не получен — входы пропущены")
            return
        self.api.tickers(self.symbols)
        fresh, preempt = [], []
        for s in self.symbols:
            if self.stop_event.is_set():
                return
            cur = self.positions.get(s)
            if cur and cur.get("phase") == "ACTIVE":
                continue
            if self.pair_blocked(s):
                continue
            try:
                arr = self.sig.candidates(s)
            except Exception as ex:
                dbg(f"{s}: scan fail {type(ex).__name__}: {ex}")
                continue
            for sig in arr:
                self.stat_signals += 1
                if cur is None:
                    fresh.append(sig)
                elif PREEMPT_ENABLED and self._can_preempt(cur, sig):
                    preempt.append((s, cur, sig))
        for s, old, sig in sorted(preempt, key=lambda x: -x[2]["score"]):
            if self.stop_event.is_set():
                return
            if self.cancel_entry_confirmed(s, old):
                del self.positions[s]
                self.stat_preempt += 1
                sig["_preempted"] = True
                fresh.append(sig)
                self.log(f"{s}: вытеснение — score {old.get('score', 0):.2f} -> "
                         f"{sig['score']:.2f}")
            else:
                self.log(f"{s}: вытеснение отменено — снятие не подтверждено, "
                         f"старый ордер оставлен (fail-safe)")
        if len(self._rej) > 1000:
            self._rej = {}
        fresh.sort(key=lambda x: (-x["score"], x["symbol"]))
        for sig in fresh:
            if self.stop_event.is_set():
                return
            if len(self.positions) >= self.max_positions:
                break
            s = sig["symbol"]
            if s in self.positions:
                continue
            bt = self._bar_t(s)
            if self._acted.get(s) == bt:
                continue
            why = self._portfolio_block(sig)
            if why:
                if self._rej.get((s, bt)) != why:
                    self._rej[(s, bt)] = why
                    self.log(f"{s}: {sig['kind']} {sig['side']} score "
                             f"{sig['score']:.2f} отклонён — {why}")
                continue
            if self.try_enter(sig):
                self._acted[s] = bt
                self.stat_taken += 1
        self.log_stats()

    def _err_kind(self, err):
        e = str(err or "").lower()
        if any(k in e for k in ("order not exist", "orderid not exist", "order_not_exist",
                                "does not exist", "order not found", "no such",
                                "already", "cancelled", "canceled", "filled",
                                "110007", "не существует ордер")):
            return "gone"
        if any(k in e for k in RATE_HINTS):
            return "retry"
        return "fail"

    def _do_cancel(self, symbol, oid=None, cid=None):
        try:
            ok = self.api.cancel(symbol, order_id=oid, client_id=cid)
        except Exception as ex:
            ok = False
            self.api.last_error = str(ex)
        return "gone" if ok else self._err_kind(getattr(self.api, "last_error", ""))

    def _entry_left(self, symbol):
        try:
            return self.api.open_entry_orders(symbol)
        except Exception:
            return None

    def _cancel_pending(self, symbol, p, why):
        p["cancel_fails"] = p.get("cancel_fails", 0) + 1
        n = p["cancel_fails"]
        self.log(f"{symbol}: отмена НЕ подтверждена ({why}) — слот держим, "
                 f"повтор через {CANCEL_RETRY_SEC}с. Попытка {n}")
        if n in (5, 15, 30):
            self.log(f"{symbol}: ⚠️ входной ордер может висеть — проверьте биржу вручную")
        return False

    def _cancel_done(self, symbol, p):
        live = self.api.open_positions()
        if live is None:
            return self._cancel_pending(symbol, p, "позиции недоступны")
        if symbol in live:
            self.log(f"{symbol}: ордер успел исполниться — сопровождаю позицию")
            return False
        p["cancel_fails"] = 0
        return True

    def cancel_entry_confirmed(self, symbol, p):
        if DRY_RUN:
            return True
        oid, cid = p.get("order_id"), p.get("client_id")
        if oid:
            for i in range(1, 4):
                k = self._do_cancel(symbol, oid=oid)
                if k == "gone":
                    break
                if k == "retry" and i < 3:
                    time.sleep(2 * i)
                    continue
                break
        left = self._entry_left(symbol)
        if left is None:
            return self._cancel_pending(symbol, p, "список ордеров недоступен")
        if not left:
            return self._cancel_done(symbol, p)
        if cid:
            self._do_cancel(symbol, cid=cid)
            time.sleep(0.8)
            left = self._entry_left(symbol)
            if left is None:
                return self._cancel_pending(symbol, p, "список ордеров недоступен")
            if not left:
                return self._cancel_done(symbol, p)
        self.log(f"{symbol}: осталось входных {len(left)} — снимаю принудительно")
        for o_id, c_id in left:
            for i in range(1, 3):
                k = self._do_cancel(symbol, oid=o_id or None,
                                    cid=None if o_id else (c_id or None))
                if k == "gone":
                    break
                if k == "retry" and i < 2:
                    time.sleep(2)
                    continue
                break
        time.sleep(1.0)
        left = self._entry_left(symbol)
        if left is None:
            return self._cancel_pending(symbol, p, "список ордеров недоступен")
        if not left:
            return self._cancel_done(symbol, p)
        ours = any((o_id and str(o_id) == str(oid)) or
                   (c_id and cid and str(c_id) == str(cid)) for o_id, c_id in left)
        if ours:
            self.log(f"{symbol}: точечно не снимается — снимаю ВСЕ ордера пары")
            try:
                self.api.cancel_all(symbol)
            except Exception as ex:
                dbg(f"{symbol}: cancel_all fail {ex}")
            time.sleep(1.2)
            left = self._entry_left(symbol)
            if left is None:
                return self._cancel_pending(symbol, p, "список ордеров недоступен")
            if not left:
                return self._cancel_done(symbol, p)
        return self._cancel_pending(symbol, p, f"осталось {len(left)}")

    def cancel_leftover_entry(self, symbol, p):
        if p.get("order_id"):
            self._do_cancel(symbol, oid=p["order_id"])
        if p.get("client_id"):
            self._do_cancel(symbol, cid=p["client_id"])

    def cleanup_orders(self, symbol):
        for oid, cid in (self.api.open_reduce_orders(symbol) or []):
            k = self._do_cancel(symbol, oid=oid or None,
                                cid=None if oid else (cid or None))
            if k != "gone":
                self.log(f"{symbol}: ордер {oid or cid} не снят — "
                         f"{getattr(self.api, 'last_error', '')}")

    def _live_positions_cached(self, ttl=5.0):
        """Живые позиции биржи с коротким кэшем — чтобы не долбить API на каждый сигнал."""
        now = time.time()
        if now - self._live_pos_ts < ttl:
            return self._live_pos_cache
        try:
            out = self.api.open_positions()
        except Exception:
            out = None
        self._live_pos_ts, self._live_pos_cache = now, out
        return out

    def _entry_gone(self, symbol, p):
        """True — входного ордера больше нет на бирже (снят вручную/исключён).
        None-ответ API не считается пропажей: слот держится до подтверждения."""
        left = self._entry_left(symbol)
        if left is None:
            p["_gone_chk"] = p.get("_gone_chk", 0) + 1
            if p["_gone_chk"] % 10 == 3:
                self.log(f"{symbol}: список ордеров недоступен x{p['_gone_chk']} — слот держится")
            return False
        p["_gone_chk"] = 0
        return len(left) == 0

    def orphan_sweep(self):
        """Периодическая чистка входных ордеров на парах без отслеживаемой позиции."""
        if DRY_RUN:
            return
        for s in self.symbols:
            if self.stop_event.is_set():
                return
            if s in self.positions:
                continue
            left = self._entry_left(s)
            if not left:
                continue
            for oid, cid in left:
                if self._do_cancel(s, oid=oid or None,
                                   cid=None if oid else (cid or None)) == "gone":
                    self.log(f"{s}: снят осиротевший входной ордер {oid or cid}")

    def try_enter(self, sig):
        s = sig["symbol"]
        if s in self.positions:
            return False
        entry, risk = sig["entry"], sig["risk"]
        # --- no-dup: на бирже не должно быть позиции, которую бот не ведёт ---
        if not DRY_RUN:
            live_chk = self._live_positions_cached()
            if live_chk is None:
                self.log(f"{s}: список позиций недоступен — вход пропущен (no-dup fail-safe)")
                return False
            if s in live_chk:
                self.log(f"{s}: на бирже уже есть позиция вне бота — вход пропущен (no-dup)")
                return False
        # --- no-chase: не ставить лимит, если цена уже за входом ---
        try:
            q = self.api.quote(s)
        except Exception:
            q = None
        cur = (q or {}).get("last")
        if cur:
            if (cur - entry) * _sgn(sig["side"]) < 0:
                self.log(f"{s}: цена {qstr(cur)} уже за входом {qstr(entry)} — "
                         f"вход отменён (no-chase)")
                return False
        mult = sig["size_mult"]
        if sig.get("confluence"):
            mult *= CONFLUENCE_SIZE_MULT
        risk_usdt = self.equity * self.risk_pct * mult
        raw_qty = risk_usdt / risk
        cap_qty = self.equity * RISK_CAP_LEVERAGE / entry
        qty = self.api.qty_ok(s, min(raw_qty, cap_qty), price=entry)
        if qty <= 0:
            self.log(f"{s}: объём {raw_qty:.6f} ниже минимума биржи — пропуск")
            return False
        cid = new_client_id("sfp")
        oid = None
        if not DRY_RUN:
            try:
                res = self.api.order(s, "Buy" if sig["side"] == "LONG" else "Sell",
                                     qty, tp=entry, client_id=cid)
                oid = extract_order_id(res)
                if not oid:
                    time.sleep(0.8)
                    for o_id, c_id in (self.api.open_entry_orders(s) or []):
                        if c_id == cid:
                            oid = o_id
                            break
                if not oid:
                    self.log(f"{s}: orderId не распознан — отмена пойдёт по clientOrderId {cid}")
            except Exception as ex:
                self.log(f"{s}: лимит-вход не выставлен — {ex}")
                return False
        self.positions[s] = {
            "phase": "WAIT_FILL", "placed_ts": time.time(),
            "order_id": oid, "client_id": cid, "cancel_fails": 0,
            "preempted": bool(sig.get("_preempted")),
            "kind": sig["kind"], "subtype": "конфлюэнс" if sig.get("confluence") else "",
            "side": sig["side"], "entry": entry, "stop0": sig["stop"],
            "stop_price": sig["stop"], "risk": risk, "stop_frac": sig["stop_frac"],
            "tp_levels": list(sig["tps"]), "splits": list(sig["splits"]),
            "tp_filled": [False] * len(sig["splits"]),
            "tail_share": sig["tail"], "be": sig["be"], "tail": False,
            "qty_total": qty, "qty": qty, "risk_usdt": qty * risk,
            "stop_id": None, "stop_cid": None,
            "score": sig["score"], "confluence": bool(sig.get("confluence")),
            "feats": dict(sig["feats"]), "mae": 0.0, "mfe": 0.0,
        }
        tag = "DRY " if DRY_RUN else ""
        self.log(f"{tag}ЛИМИТ-ВХОД {sig['side']} {s} [{sig['kind']}] "
                 f"по {qstr(entry)}, стоп {qstr(sig['stop'])} "
                 f"({sig['stop_frac'] * 100:.2f}%), цели "
                 f"{' / '.join(qstr(t) for t in sig['tps'])}, "
                 f"хвост {sig['tail'] * 100:.0f}%, score {sig['score']:.2f}"
                 f"{' КОНФЛЮЭНС' if sig.get('confluence') else ''} "
                 f"[{len(self.positions)}/{self.max_positions}] — {sig['note']}")
        return True

    def place_stop(self, symbol, p, qty, stop_price):
        if DRY_RUN:
            return None, None
        cid = new_client_id("sl")
        try:
            res = self.api.order(symbol, "Sell" if p["side"] == "LONG" else "Buy",
                                 qty, stop=stop_price, reduce_only=True, client_id=cid)
            return extract_order_id(res), cid
        except Exception as ex:
            self.log(f"{symbol}: стоп не выставлен — {ex}. Резервное закрытие активно")
            return None, None

    def move_stop(self, symbol, p, new_stop, new_qty):
        new_stop = self.api.price_ok(symbol, new_stop)
        if new_stop is None:
            return
        if DRY_RUN:
            p["stop_price"] = new_stop
            return
        q = self.api.qty_ok(symbol, new_qty, price=p["entry"])
        if q <= 0:
            return
        old_id, old_cid = p.get("stop_id"), p.get("stop_cid")
        nid, ncid = self.place_stop(symbol, p, q, new_stop)
        if not nid and not ncid:
            self.log(f"{symbol}: перенос стопа не удался — старый стоп остаётся")
            return
        p["stop_price"], p["stop_id"], p["stop_cid"] = new_stop, nid, ncid
        if old_id:
            self._do_cancel(symbol, oid=old_id)
        if old_cid and old_cid != ncid:
            self._do_cancel(symbol, cid=old_cid)
        self.log(f"{symbol}: стоп перенесён на {qstr(new_stop)} (объём {qstr(q)})")

    def place_tps(self, symbol, p):
        if DRY_RUN:
            return 0
        placed = 0
        for i, (w, tp) in enumerate(zip(p["splits"], p["tp_levels"])):
            q = self.api.qty_ok(symbol, p["qty_total"] * w, price=p["entry"])
            if q <= 0:
                self.log(f"{symbol}: тейк {i + 1} ниже минимума биржи — пропущен")
                continue
            try:
                self.api.order(symbol, "Sell" if p["side"] == "LONG" else "Buy",
                               q, tp=tp, reduce_only=True, client_id=new_client_id("tp"))
                placed += 1
            except Exception as ex:
                self.log(f"{symbol}: тейк {i + 1} не выставлен — {ex}")
        if placed == 0:
            self.log(f"{symbol}: тейки не выставлены — сопровождение по стопу")
        return placed

    def chandelier(self, symbol, p):
        try:
            bars = self.api.klines(symbol, limit=CHANDELIER_LOOKBACK + ATR_PERIOD + 5,
                                   max_age_ms=20000)
        except Exception:
            return None
        closed = bars[:-1] if len(bars) > 1 else bars
        if len(closed) < CHANDELIER_LOOKBACK + ATR_PERIOD:
            return None
        a = atr_last(closed)
        if not a:
            return None
        win = closed[-CHANDELIER_LOOKBACK:]
        if p["side"] == "LONG":
            return max(b[2] for b in win) - CHANDELIER_ATR * a
        return min(b[3] for b in win) + CHANDELIER_ATR * a

    def _activate(self, symbol, p, info):
        qty = info["qty"]
        p["qty_total"], p["qty"] = qty, qty
        p["risk_usdt"] = qty * p["risk"]
        p["phase"] = "ACTIVE"
        sid, scid = self.place_stop(symbol, p, qty, p["stop_price"])
        p["stop_id"], p["stop_cid"] = sid, scid
        self.place_tps(symbol, p)
        ok = bool(sid) or bool(scid)
        self.log(f"ВХОД исполнен {p['side']} {symbol} [{p['kind']}] по "
                 f"{qstr(info.get('entry') or p['entry'])}, стоп "
                 f"{qstr(p['stop_price'])}, "
                 f"{'защита выставлена' if ok else 'СТОП НЕ ВЫСТАВЛЕН — резервное закрытие'}")

    def _detect_tps(self, symbol, p, info):
        if p["qty_total"] <= 0:
            return
        frac = (p["qty_total"] - info["qty"]) / p["qty_total"]
        n, cum, now = len(p["splits"]), 0.0, 0
        for w in p["splits"]:
            cum += w
            if frac + 0.06 >= cum:
                now += 1
        now = min(n, now)
        done = sum(p["tp_filled"])
        if now <= done:
            return
        for i in range(done, now):
            p["tp_filled"][i] = True
            self.log(f"ТЕЙК {i + 1}/{n} по {symbol} ({qstr(p['tp_levels'][i])})")
            if i == 0:
                be = p["entry"] + _sgn(p["side"]) * p["be"] * p["risk"]
                self.move_stop(symbol, p, be, info["qty"])
        if now >= n:
            if p.get("tail_share", 0) > 0:
                p["tail"] = True
                self.log(f"{symbol}: тейки отработаны, хвост "
                         f"{p['tail_share'] * 100:.0f}% трейлится ATR-каналом")
            else:
                self.close_trade(symbol, p, "ТЕЙК-ПОЛНЫЙ", p["tp_levels"][-1])
                self.cleanup_orders(symbol)
                del self.positions[symbol]

    def _settle_closed(self, symbol, p, cur):
        sp, sgn = p["stop_price"], _sgn(p["side"])
        stopped = cur is not None and (cur - sp) * sgn <= 0
        loss = cur is not None and (cur - p["entry"]) * sgn < 0
        if stopped and loss:
            self.register_stop(symbol)
            self.log(f"СТОП по {symbol}. Стопов за день: {self.losses_today}")
            self.close_trade(symbol, p, "СТОП", cur)
        elif stopped:
            self.close_trade(symbol, p, "ТРЕЙЛ" if p.get("tail") else "БЕЗУБЫТОК", cur)
        elif cur is not None and (cur - p["tp_levels"][-1]) * sgn >= 0:
            p["tp_filled"] = [True] * len(p["tp_filled"])
            self.close_trade(symbol, p, "ТЕЙК-ПОЛНЫЙ", cur)
        else:
            self.close_trade(symbol, p, "ПРОЧЕЕ", cur)
        self.cleanup_orders(symbol)
        del self.positions[symbol]

    def _fallback_close(self, symbol, p, cur):
        sp, sgn = p["stop_price"], _sgn(p["side"])
        if cur is None or sp is None or (cur - sp) * sgn > 0:
            return
        try:
            self.api.order(symbol, "Sell" if p["side"] == "LONG" else "Buy",
                           p["qty"], reduce_only=True, client_id=new_client_id("fb"))
        except Exception as ex:
            self.log(f"{symbol}: резервное закрытие не удалось — {ex}")
            return
        if (cur - p["entry"]) * sgn < 0:
            self.register_stop(symbol)
            self.close_trade(symbol, p, "СТОП", cur)
        else:
            self.close_trade(symbol, p, "ТРЕЙЛ" if p.get("tail") else "БЕЗУБЫТОК", cur)
        self.cleanup_orders(symbol)
        del self.positions[symbol]

    def manage_positions(self):
        if self.day != dt.date.today():
            self.reset_day()
        if DRY_RUN:
            self._manage_dry()
            return
        live = self.api.open_positions()
        quotes = self.api.tickers(self.symbols)
        for symbol in list(self.positions):
            p = self.positions[symbol]
            cur = (quotes.get(symbol) or {}).get("last") or self.api.price(symbol)
            self.track_excursion(symbol, p, cur)
            if p["phase"] == "WAIT_FILL":
                info = live.get(symbol) if live is not None else None
                if info:
                    self.cancel_leftover_entry(symbol, p)
                    self._activate(symbol, p, info)
                elif time.time() > p["placed_ts"] + ENTRY_WAIT * self.tf_sec:
                    if self.cancel_entry_confirmed(symbol, p):
                        self.log(f"{symbol}: лимит не исполнен за {ENTRY_WAIT} свечей — сигнал отменён")
                        del self.positions[symbol]
                    else:
                        p["placed_ts"] = (time.time() - ENTRY_WAIT * self.tf_sec
                                          + CANCEL_RETRY_SEC)
                elif self._entry_gone(symbol, p):
                    live2 = self.api.open_positions()
                    if live2 is not None and symbol in live2:
                        # гонка: ордер исполнился между двумя запросами — активирую
                        self.cancel_leftover_entry(symbol, p)
                        self._activate(symbol, p, live2[symbol])
                    else:
                        self.log(f"{symbol}: входной ордер исчез с биржи (снят вручную?) — "
                                 f"слот освобождён")
                        del self.positions[symbol]
                continue
            info = live.get(symbol) if live is not None else None
            if info is None:
                if live is None:
                    continue
                self._settle_closed(symbol, p, cur)
                continue
            p["qty"] = info["qty"]
            self._detect_tps(symbol, p, info)
            if symbol not in self.positions:
                continue
            if p.get("tail") and p["qty"] > 0:
                ch = self.chandelier(symbol, p)
                if ch is not None:
                    ns = max(p["stop_price"], ch) if p["side"] == "LONG" \
                        else min(p["stop_price"], ch)
                    if ns != p["stop_price"]:
                        self.move_stop(symbol, p, ns, p["qty"])
            if not p.get("stop_id") and not p.get("stop_cid"):
                self._fallback_close(symbol, p, cur)

    def _manage_dry(self):
        quotes = self.api.tickers(self.symbols)
        for symbol in list(self.positions):
            p = self.positions[symbol]
            cur = (quotes.get(symbol) or {}).get("last") or self.api.price(symbol)
            if cur is None:
                continue
            self.track_excursion(symbol, p, cur)
            sgn = _sgn(p["side"])
            if p["phase"] == "WAIT_FILL":
                if (cur - p["entry"]) * sgn <= 0:
                    p["phase"] = "ACTIVE"
                    self.log(f"DRY: ВХОД {p['side']} {symbol} [{p['kind']}] по {qstr(p['entry'])}")
                elif time.time() > p["placed_ts"] + ENTRY_WAIT * self.tf_sec:
                    self.log(f"{symbol}: DRY лимит не исполнен за {ENTRY_WAIT} свечей — отменён")
                    del self.positions[symbol]
                    continue
                else:
                    continue
            if p.get("tail"):
                ch = self.chandelier(symbol, p)
                if ch is not None:
                    ns = max(p["stop_price"], ch) if sgn > 0 else min(p["stop_price"], ch)
                    if ns != p["stop_price"]:
                        p["stop_price"] = ns
                        self.log(f"DRY: трейлинг {symbol} — стоп {qstr(ns)}")
            if (cur - p["stop_price"]) * sgn <= 0:
                sp = p["stop_price"]
                if (sp - p["entry"]) * sgn < 0:
                    self.register_stop(symbol)
                    self.close_trade(symbol, p, "СТОП", sp)
                else:
                    self.close_trade(symbol, p, "ТРЕЙЛ" if p.get("tail") else "БЕЗУБЫТОК", sp)
                del self.positions[symbol]
                continue
            n = len(p["splits"])
            for i in range(n):
                if p["tp_filled"][i]:
                    continue
                tp = p["tp_levels"][i]
                if (cur - tp) * sgn < 0:
                    break
                p["tp_filled"][i] = True
                p["qty"] = max(0.0, p["qty"] - p["qty_total"] * p["splits"][i])
                self.log(f"DRY: ТЕЙК {i + 1}/{n} {symbol} ({qstr(tp)}), остаток {qstr(p['qty'])}")
                if i == 0:
                    p["stop_price"] = p["entry"] + sgn * p["be"] * p["risk"]
                    self.log(f"{symbol}: стоп перенесён на {qstr(p['stop_price'])}")
                if sum(p["tp_filled"]) >= n:
                    if p.get("tail_share", 0) > 0 and p["qty"] > 1e-9:
                        p["tail"] = True
                        self.log(f"DRY: {symbol} хвост {p['tail_share'] * 100:.0f}% трейлится ATR-каналом")
                    else:
                        self.close_trade(symbol, p, "ТЕЙК-ПОЛНЫЙ", p["tp_levels"][-1])
                        del self.positions[symbol]
                    break

    def recover(self):
        self.load_day()
        if DRY_RUN:
            return
        live = self.api.open_positions()
        if live is None:
            self.log("Позиции недоступны (сбой API) — восстановление пропущено. "
                     "Проверьте биржу вручную!")
            return
        for sym, info in live.items():
            if sym not in self.symbols or sym in self.positions:
                continue
            side, entry, qty = info["side"], info["entry"], info["qty"]
            if entry <= 0 or qty <= 0:
                continue
            stop, _ = self.api.pending_exit_prices(sym)
            self.cleanup_orders(sym)
            for oid, cid in (self.api.open_entry_orders(sym) or []):
                self._do_cancel(sym, oid=oid or None, cid=None if oid else (cid or None))
            if not stop or stop == entry:
                stop = entry * (1 - 0.01) if side == "LONG" else entry * (1 + 0.01)
                self.log(f"{sym}: биржевой стоп не найден — взят расчётный 1%")
            stop = self.api.price_ok(sym, stop)
            risk = abs(entry - stop)
            if risk <= 0:
                continue
            tps = [self.api.price_ok(sym, t) for t in
                   calc_tps(entry, risk, side, [r for r, _ in TP_TREND])]
            splits = [w for _, w in TP_TREND]
            p = {"phase": "ACTIVE", "placed_ts": time.time(),
                 "order_id": None, "client_id": None, "cancel_fails": 0,
                 "preempted": False, "kind": "RECOVER", "subtype": "",
                 "side": side, "entry": entry, "stop0": stop, "stop_price": stop,
                 "risk": risk, "stop_frac": risk / entry,
                 "tp_levels": tps, "splits": splits,
                 "tp_filled": [False] * len(splits),
                 "tail_share": TREND_TAIL_SHARE, "be": TREND_BE_AFTER_TP1,
                 "tail": False, "qty_total": qty, "qty": qty,
                 "risk_usdt": qty * risk, "stop_id": None, "stop_cid": None,
                 "score": 0.0, "confluence": False, "feats": {},
                 "mae": 0.0, "mfe": 0.0}
            self.positions[sym] = p
            sid, scid = self.place_stop(sym, p, qty, stop)
            p["stop_id"], p["stop_cid"] = sid, scid
            self.place_tps(sym, p)
            self.log(f"Восстановлена {side} {sym} по {qstr(entry)}, объём {qstr(qty)}: "
                     f"стоп {qstr(stop)}, тейки {' / '.join(qstr(t) for t in tps)}")
        for sym in self.symbols:
            if sym in self.positions:
                continue
            try:
                left = self.api.open_entry_orders(sym)
            except Exception:
                left = None
            for oid, cid in (left or []):
                if self._do_cancel(sym, oid=oid or None,
                                   cid=None if oid else (cid or None)) == "gone":
                    self.log(f"{sym}: снят осиротевший входной ордер {oid or cid}")
        if self.positions:
            self.log(f"Восстановлено позиций: {len(self.positions)}")

    def run(self):
        mode = "DRY-RUN (ордера НЕ отправляются)" if DRY_RUN else "РЕАЛЬНЫЙ РЕЖИМ"
        self.log(WARNING_ISOLATED)
        self.log(f"Папка данных: {_DATA_DIR}")
        self.log(f"Журнал: {LOG_FILE}")
        self.log(f"Сделки: {TRADES_FILE}")
        self.log(f"SFP-бот v5 CLEAN: {self.api.name} {self.api.base}. {mode}. ТФ {self.tf_label}")
        trend = (f"EMA{EMA_PERIOD} на {self.sig.trend_label}" if self.sig.trend_enabled
                 else "EMA выключена — режим по рабочему ТФ")
        self.log(f"Режим: |наклон EMA{EMA_PERIOD}| / ATR > {REGIME_THRESHOLD} за "
                 f"{REGIME_LOOKBACK} бар = сильный тренд. {trend}")
        self.log(f"Пол стопа: stop >= {COST_STOP_MULT:.0f} x (maker+taker+спред); "
                 f"спред > {MAX_SPREAD_PCT}% — пара пропускается")
        self.log(f"RANGE v5: канал {RANGE_DAYS} дн ({self.sig.range_bars} бар), "
                 f"касания >={RANGE_MIN_TOUCHES}, объём x{RANGE_VOL_MULT} (работает), "
                 f"вход {RANGE_ENTRY_RETRACE * 100:.0f}% отката, стоп {RANGE_STOP_ATR} ATR, "
                 f"цели середина (>={RANGE_MIN_TP1_R}R) и дальняя граница "
                 f"(>={RANGE_MIN_TP2_R}R), после ТП1 стоп {RANGE_BE_AFTER_TP1}R, "
                 f"против режима не торгуем")
        self.log(f"TREND-A: свинг {SFP_SWING}, объём x{self.sig.vol_mult}, "
                 f"стоп {SFP_STOP_ATR} ATR. TREND-B: CHoCH {CHOCH_SWING} + слом "
                 f"{CHOCH_MINOR}, стоп {CHOCH_STOP_ATR} ATR, против режима "
                 f"размер x{CHOCH_COUNTER_SIZE}")
        self.log(f"Выходы TREND: {TP_TREND[0][0]}R ({TP_TREND[0][1] * 100:.0f}%) + "
                 f"{TP_TREND[1][0]}R ({TP_TREND[1][1] * 100:.0f}%), хвост "
                 f"{TREND_TAIL_SHARE * 100:.0f}% — ATR-канал {CHANDELIER_ATR} ATR / "
                 f"{CHANDELIER_LOOKBACK} бар")
        self.log(f"Риск {self.risk_pct * 100:.2f}% компаунд, кэп x{RISK_CAP_LEVERAGE:.0f}. "
                 f"Вход ждёт {ENTRY_WAIT} свечей. Позиций max {self.max_positions}. "
                 f"Лимиты: {MAX_STOPS_PER_PAIR}/пара, {self.max_losses}/корзина, "
                 f"{DAILY_R_LIMIT}R/день, кулдаун {PAIR_COOLDOWN_HOURS}ч после "
                 f"{PAIR_COOLDOWN_STOPS} стопов. Риск всего <={MAX_TOTAL_RISK * 100:.1f}%, "
                 f"в сторону <={MAX_SAME_DIR_RISK * 100:.1f}%, 1 на кластер/сторону")
        self.log(f"Отбор: скоринг всей корзины ({len(self.symbols)} пар), вытеснение "
                 f"{'вкл' if PREEMPT_ENABLED else 'выкл'} (порог x{PREEMPT_SCORE_RATIO}). "
                 f"Позиции каждые {FAST_PERIOD}с, сигналы на закрытии бара")
        self.log(MOSCOW_HINT)
        if DRY_RUN:
            self.equity = self.dry_balance
            self.log(f"DRY-RUN: расчётный баланс {self.dry_balance:.0f} USDT")
        else:
            try:
                bal = self.api.balance()
                if bal > 0:
                    self.equity = bal
                    self.log(f"Баланс: {bal:.2f} USDT — API-доступ подтверждён")
            except Exception as ex:
                self.log(f"[API FAIL] Баланс не получен: {ex}")
        self.recover()
        bar_ms = tf_ms(self.tf_label)
        last_bar, last_status = None, 0.0
        while not self.stop_event.is_set():
            try:
                self.manage_positions()
            except Exception as ex:
                self.log(f"Ошибка сопровождения: {ex}\n{traceback.format_exc()}")
            now_ms = int(time.time() * 1000)
            bar_id, into = now_ms // bar_ms, now_ms % bar_ms
            if bar_id != last_bar and into >= 5000:
                last_bar = bar_id
                try:
                    self.scan_signals()
                except Exception as ex:
                    self.log(f"Ошибка отбора: {ex}\n{traceback.format_exc()}")
            if time.time() - last_status > 300:
                last_status = time.time()
                try:
                    self.orphan_sweep()
                except Exception as ex:
                    dbg(f"orphan_sweep fail: {ex}")
                tot, lr, sr = self.open_risk()
                self.log(f"Статус: позиций {len(self.positions)}/{self.max_positions}, "
                         f"риск {tot * 100:.2f}% (L {lr * 100:.2f}% / S {sr * 100:.2f}%), "
                         f"день {self.day_r:+.2f}R, стопов {self.losses_today}")
            for _ in range(FAST_PERIOD):
                if self.stop_event.is_set():
                    break
                time.sleep(1)
        self.log("Бот остановлен")
# =====================================================================
#  ЧАСТЬ 5/5: Оболочка Flet — все 13 полей настроек
# =====================================================================
def tg_send(cfg, text):
    if not cfg.get("token") or not cfg.get("chat"):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{cfg['token']}/sendMessage",
                      json={"chat_id": cfg["chat"], "text": text}, timeout=10)
    except Exception:
        pass

def main(page: ft.Page):
    page.title = "SFP-бот v5 CLEAN"
    page.padding = 48

    # миграция: если файл состояния пуст, а в Download он есть — переносим
    if not os.path.exists(STATE_FILE):
        _old = os.path.join("/storage/emulated/0/Download", "SFP_Bot", "sfp_state.json")
        if os.path.exists(_old):
            try:
                with open(_old, "r", encoding="utf-8") as _f:
                    state_save(json.load(_f))
                log_to_file(f"[MIGRATE] состояние перенесено из {_old}")
            except Exception as _ex:
                log_to_file(f"[MIGRATE] не удалось: {_ex}")

    def load_profiles():
        return state_load().get("profiles") or {}
    def save_profiles(p):
        d = state_load()
        d["profiles"] = p
        state_save(d)
    profiles = load_profiles()

    def load_state():
        return state_load().get("losses")
    def save_state(data):
        d = state_load()
        d["losses"] = data
        state_save(d)

    ex = ft.Dropdown(label="Биржа", options=[ft.dropdown.Option("Bybit"),
                                             ft.dropdown.Option("BingX")], value="BingX")
    mode = ft.Dropdown(label="Режим", options=[ft.dropdown.Option("Демо"),
                                               ft.dropdown.Option("Реальный")], value="Демо")
    tf = ft.Dropdown(label="Таймфрейм",
                     options=[ft.dropdown.Option(k) for k in TIMEFRAMES], value="15 минут")
    trend_tf = ft.Dropdown(label="Тренд-ТФ (EMA)",
                           options=[ft.dropdown.Option(EMA_OFF)] +
                                   [ft.dropdown.Option(k) for k in TIMEFRAMES], value="1 час")
    api_key = ft.TextField(label="API-ключ", password=True, can_reveal_password=True)
    secret = ft.TextField(label="Секрет", password=True, can_reveal_password=True)
    symbols = ft.TextField(
        label="Пары через запятую",
        value="BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,BNBUSDT,ZECUSDT,HYPEUSDT,XPLUSDT,"
              "UNIUSDT,NEARUSDT,DOGEUSDT,ADAUSDT,LTCUSDT,LINKUSDT,AVAXUSDT,TRXUSDT,"
              "DOTUSDT,SUIUSDT,APTUSDT,ARBUSDT,OPUSDT,JUPUSDT,FILUSDT,ATOMUSDT,"
              "XLMUSDT,HBARUSDT,INJUSDT,PEPEUSDT,WIFUSDT,TIAUSDT,SEIUSDT,SUSDT,"
              "ICPUSDT,ETCUSDT,BCHUSDT,POLUSDT,ALGOUSDT,VETUSDT,AAVEUSDT,CRVUSDT,"
              "LDOUSDT,GRTUSDT,FETUSDT,RENDERUSDT,TAOUSDT,ASTERUSDT,NIGHTUSDT,ENAUSDT,"
              "PENDLEUSDT,WLDUSDT,VIRTUALUSDT,KASUSDT,FARTCOINUSDT,DYDXUSDT,"
              "ONDOUSDT,ARKMUSDT")
    deposit = ft.TextField(label="Риск на сделку (0.015 = 1.5% компаунд от баланса)",
                           value=str(RISK_PCT_DEFAULT))
    losses = ft.TextField(label="Макс. стопов на корзину в день",
                          value=str(MAX_STOPS_BASKET))
    maxpos = ft.TextField(label="Макс. позиций одновременно", value="3")
    ema_prox = ft.TextField(label="Вход не дальше N% от EMA (0 = без ограничения)",
                            value="3.0")
    tg_token = ft.TextField(label="Токен Telegram-бота", password=True,
                            can_reveal_password=True)
    tg_chat = ft.TextField(label="Ваш chat_id в Telegram")
    profile_name = ft.TextField(label="Имя профиля", hint_text="например: BingX-демо-15m")
    profile_dd = ft.Dropdown(label="Сохранённые профили",
                             options=[ft.dropdown.Option(k) for k in sorted(profiles)],
                             value=sorted(profiles)[0] if profiles else None)
    status = ft.Text("Остановлен", size=18, weight=ft.FontWeight.BOLD)
    dry_note = ft.Text("🧪 DRY-RUN: ордера НЕ отправляются"
                       if DRY_RUN else "⚠️ РЕАЛЬНЫЙ РЕЖИМ: ордера идут на биржу",
                       size=14, weight=ft.FontWeight.BOLD)
    moscow_note = ft.Text(MOSCOW_HINT, size=13)
    path_note = ft.Text(f"📁 Папка данных: {_DATA_DIR}", size=12, color=ft.Colors.BLUE)
    log_view = ft.ListView(spacing=5, auto_scroll=True, expand=True)
    cfg = {"token": "", "chat": ""}
    ui_lock = threading.Lock()
    warning_banner = ft.Container(
        content=ft.Text(WARNING_ISOLATED, color=ft.Colors.BLACK,
                        weight=ft.FontWeight.BOLD),
        bgcolor=ft.Colors.GREEN_100, padding=10, border_radius=6)
    btn_start = ft.FilledButton("Запустить", expand=True)
    btn_stop = ft.FilledButton("Остановить", expand=True, disabled=True)

    def add_log(msg, to_file=True):
        if to_file:
            log_to_file(msg)
        with ui_lock:
            log_view.controls.append(ft.Text(f"{dt.datetime.now():%H:%M:%S}  {msg}"))
            page.update()
        tg_send(cfg, msg)

    def bot_log(msg):
        add_log(msg, to_file=False)

    def _export(src, name):
        try:
            with open(src, "r", encoding="utf-8") as f:
                data = f.read()
        except Exception:
            add_log(f"{name}: файл не найден")
            return
        try:
            dest = os.path.join(_DATA_DIR, name)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(data)
            add_log(f"Сохранено: {dest}")
            return
        except Exception:
            pass
        for base in ("/storage/emulated/0/Download", "/sdcard/Download",
                     os.path.expanduser("~/Downloads")):
            try:
                dest = os.path.join(base, name)
                with open(dest, "w", encoding="utf-8") as f:
                    f.write(data)
                add_log(f"Сохранено: {dest}")
                return
            except Exception:
                continue
        add_log(f"{name}: нет доступа к папке")

    FIELDS = {"ex": ex, "mode": mode, "tf": tf, "trend_tf": trend_tf,
              "api_key": api_key, "secret": secret, "symbols": symbols,
              "deposit": deposit, "losses": losses, "maxpos": maxpos,
              "ema_prox": ema_prox, "tg_token": tg_token, "tg_chat": tg_chat}

    def collect():
        return {k: (f.value or "") for k, f in FIELDS.items()}
    def apply(data):
        for k, f in FIELDS.items():
            f.value = data.get(k, "")
        page.update()

    def refresh_dd(selected=None):
        profile_dd.options = [ft.dropdown.Option(k) for k in sorted(profiles)]
        profile_dd.value = selected if selected in profiles else \
            (sorted(profiles)[0] if profiles else None)
        page.update()

    def on_save(e):
        name = profile_name.value.strip() or profile_dd.value
        if not name:
            add_log("Введите имя профиля")
            return
        profiles[name] = collect()
        d = state_load()
        d["profiles"] = profiles
        d["last_profile"] = name
        state_save(d)
        refresh_dd(name)
        profile_name.value = ""
        add_log(f"Профиль «{name}» сохранён")

    def on_load(e):
        name = profile_dd.value
        if not name or name not in profiles:
            add_log("Выберите профиль из списка")
            return
        apply(profiles[name])
        d = state_load()
        d["last_profile"] = name
        state_save(d)
        add_log(f"Профиль «{name}» загружен")

    def on_delete(e):
        name = profile_dd.value
        if not name or name not in profiles:
            add_log("Выберите профиль для удаления")
            return
        del profiles[name]
        d = state_load()
        d["profiles"] = profiles
        state_save(d)
        refresh_dd()
        add_log(f"Профиль «{name}» удалён")

    profile_dd.on_change = on_load

    state = {"bot": None, "thread": None, "active": False}

    def set_buttons(running):
        with ui_lock:
            btn_start.disabled = running
            btn_stop.disabled = not running
            page.update()

    def on_start(e):
        if state["active"]:
            add_log("Бот уже работает — сначала нажмите Остановить")
            return
        pairs = [s.strip().replace("-", "").replace("/", "").upper()
                 for s in symbols.value.split(",") if s.strip()]
        if not pairs:
            add_log("Укажите хотя бы одну пару")
            return
        if not api_key.value.strip() or not secret.value.strip():
            add_log("Введите API-ключ и секрет")
            return
        tf_cfg = TIMEFRAMES.get(tf.value)
        if not tf_cfg:
            add_log("Выберите таймфрейм из списка")
            return
        if trend_tf.value != EMA_OFF:
            if trend_tf.value not in TIMEFRAMES:
                add_log("Выберите тренд-ТФ из списка")
                return
            if TF_ORDER.index(trend_tf.value) <= TF_ORDER.index(tf.value):
                add_log(f"Тренд-ТФ должен быть крупнее рабочего ({tf.value})")
                return
        try:
            prox = float(ema_prox.value)
        except ValueError:
            add_log("Проверьте число в поле «N% от EMA»")
            return
        try:
            risk = float(deposit.value)
            if not (0 < risk <= 0.05):
                raise ValueError
        except ValueError:
            add_log("Риск — доля баланса от 0 до 0.05 (например 0.015)")
            return
        try:
            n_loss, n_pos = int(losses.value), int(maxpos.value)
            if n_loss < 1 or n_pos < 1:
                raise ValueError
        except ValueError:
            add_log("Лимиты стопов и позиций — целые числа больше нуля")
            return
        if risk > MAX_SAME_DIR_RISK:
            add_log(f"Внимание: риск {risk * 100:.2f}% выше лимита на сторону "
                    f"{MAX_SAME_DIR_RISK * 100:.1f}% — вторая однонаправленная "
                    f"позиция будет отклоняться")
        demo = mode.value == "Демо"
        if ex.value == "Bybit":
            api = BybitAPI(api_key.value.strip(), secret.value.strip(),
                           demo, tf_cfg["interval_bybit"])
        else:
            api = BingXAPI(api_key.value.strip(), secret.value.strip(),
                           demo, tf_cfg["interval_bingx"])
        bot = Bot(api, tf.value, trend_tf.value, pairs, risk, n_loss, n_pos, bot_log,
                  ema_proximity_pct=prox, load_state=load_state, save_state=save_state)
        cfg["token"] = tg_token.value.strip()
        cfg["chat"] = tg_chat.value.strip()
        state["bot"], state["active"] = bot, True
        state["thread"] = threading.Thread(target=bot.run, daemon=True)
        state["thread"].start()
        status.value = f"Работает: {ex.value} / {mode.value} / {tf.value}"
        set_buttons(True)
        d = state_load()
        d["last_form"] = collect()
        state_save(d)

    def on_stop(e):
        if state["bot"]:
            state["bot"].stop_event.set()
        state["active"] = False
        status.value = "Остановлен"
        set_buttons(False)

    btn_start.on_click = on_start
    btn_stop.on_click = on_stop

    settings_tab = ft.Column([
        profile_dd, profile_name,
        ft.FilledButton("Сохранить настройки", on_click=on_save, expand=True),
        ft.Row([ft.FilledButton("Загрузить", on_click=on_load, expand=True),
                ft.OutlinedButton("Удалить", on_click=on_delete, expand=True)]),
        ft.Divider(),
        ex, mode, tf, trend_tf, api_key, secret, symbols,
        ft.Row([ft.Container(deposit, expand=1), ft.Container(losses, expand=1)]),
        ft.Container(maxpos, expand=1), ema_prox, tg_token, tg_chat,
        dry_note, moscow_note, path_note,
        ft.Row([btn_start, btn_stop]), status,
    ], spacing=12, scroll=ft.ScrollMode.AUTO, expand=True)

    log_tab = ft.Column([
        ft.Row([ft.OutlinedButton("Очистить", on_click=lambda e: (
            log_view.controls.clear(), page.update())),
                ft.FilledButton("Лог", on_click=lambda e: _export(LOG_FILE, "sfp_log.txt")),
                ft.FilledButton("Сделки", on_click=lambda e: _export(TRADES_FILE,
                                                                     "sfp_trades.csv"))]),
        log_view,
    ], spacing=10, expand=True)

    btn_tab_settings = ft.FilledButton("Настройки бота", expand=True)
    btn_tab_log = ft.FilledButton("Журнал", expand=True)
    content = ft.Column([settings_tab], spacing=10, expand=True)

    def show_tab(idx):
        btn_tab_settings.disabled = (idx == 0)
        btn_tab_log.disabled = (idx == 1)
        content.controls = [settings_tab] if idx == 0 else [log_tab]
        page.update()

    btn_tab_settings.on_click = lambda e: show_tab(0)
    btn_tab_log.on_click = lambda e: show_tab(1)

    page.add(ft.Column([warning_banner,
                        ft.Row([btn_tab_settings, btn_tab_log]), content],
                       spacing=10, expand=True))

    st = state_load()
    log_to_file(f"[START] file={STATE_FILE} exists={os.path.exists(STATE_FILE)} "
                f"profiles={list((st.get('profiles') or {}).keys())} "
                f"last={st.get('last_profile')}")
    last = st.get("last_profile")
    if not last and profiles:
        last = sorted(profiles)[0]          # запасной путь: грузим первый профиль
    if last and last in profiles:
        try:
            apply(profiles[last])
            refresh_dd(last)
            log_to_file(f"[START] профиль «{last}» загружен")
        except Exception as ex:
            log_to_file(f"[START] apply() упал: {ex}")
    else:
        lf = st.get("last_form") or {}
        if lf:
            try:
                apply(lf)
                log_to_file("[START] применены последние настройки (last_form)")
            except Exception as ex2:
                log_to_file(f"[START] apply(last_form) упал: {ex2}")
        else:
            log_to_file("[START] профиль НЕ загружен: нет last_profile и нет профилей")

ft.run(main)
