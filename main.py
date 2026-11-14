# -*- coding: utf-8 -*-
# ======================================================================
#  ВНИМАНИЕ: ТОРГОВАТЬ ТОЛЬКО ИЗОЛИРОВАННОЙ МАРЖОЙ (ISOLATED) С ПЛЕЧОМ 5x
#  Размер позиции бот считает из риска (1.5% на сделку, КОМПАУНД) —
#  плечо в приложении биржи задаёт только объём залога под позицию.
#  v4: range и тренд-SFP активны ОДНОВРЕМЕННО. Конфликт сигналов на одном
#  баре: совпадение направления -> приоритет ДИАПАЗОНА; разнонаправленные
#  -> пропуск (канал сломан). Висящий лимит-ордер новым сигналом не
#  перебивается (FIFO). Килл-зоны отключены — торговля 24/7.
# ======================================================================

import time, hmac, hashlib, urllib.parse, threading, json, os
import datetime as dt
from decimal import Decimal, ROUND_DOWN
import requests
import flet as ft

WARNING_ISOLATED = ("⚠️ ТОРГОВАТЬ ТОЛЬКО ИЗОЛИРОВАННОЙ МАРЖОЙ (ISOLATED) С ПЛЕЧОМ 5x. "
                    "Размер позиции считает бот из риска (1.5%, компаунд) — "
                    "плечо задаёт только залог.")

BYBIT_REAL = "https://api.bybit.com"
BYBIT_DEMO = "https://api-demo.bybit.com"
BINGX_REAL = "https://open-api.bingx.com"
BINGX_DEMO = "https://open-api-vst.bingx.com"

# каталог данных приложения: переживает перезапуск и обновление APK
STATE_FILE = os.path.join(os.getenv("FLET_APP_STORAGE_DATA") or os.getcwd(),
                          "sfp_state.json")

MOSCOW_HINT = ("⏰ v4: килл-зоны отключены — бот торгует круглосуточно (24/7). "
               "Лимит входа ждёт исполнения 3 свечи, затем сигнал отменяется.")

TIMEFRAMES = {
    "1 минута": {"interval_bybit": "1",   "interval_bingx": "1m",  "vol_mult": 3.0},
    "5 минут":  {"interval_bybit": "5",   "interval_bingx": "5m",  "vol_mult": 2.8},
    "15 минут": {"interval_bybit": "15",  "interval_bingx": "15m", "vol_mult": 1.5},
    "30 минут": {"interval_bybit": "30",  "interval_bingx": "30m", "vol_mult": 2.2},
    "1 час":    {"interval_bybit": "60",  "interval_bingx": "1h",  "vol_mult": 2.0},
    "4 часа":   {"interval_bybit": "240", "interval_bingx": "4h",  "vol_mult": 1.5},
    "1 день":   {"interval_bybit": "D",   "interval_bingx": "1d",  "vol_mult": 1.2},
}

TF_MINUTES = {"1 минута": 1, "5 минут": 5, "15 минут": 15, "30 минут": 30,
              "1 час": 60, "4 часа": 240, "1 день": 1440}

TF_ORDER = ["1 минута", "5 минут", "15 минут", "30 минут",
            "1 час", "4 часа", "1 день"]

BYBIT_ALIASES = {
    "PEPEUSDT":  "1000PEPEUSDT",
    "BONKUSDT":  "1000BONKUSDT",
    "FLOKIUSDT": "1000FLOKIUSDT",
    "SHIBUSDT":  "1000SHIBUSDT",
    "PUMPUSDT":  "1000PUMPUSDT",
    "WLFIUSDT":  "1000WLFIUSDT",
    "WIFUSDT":   "WIFUSDT",
}

BINGX_ALIASES = {
    "PEPEUSDT":  "PEPEUSDT",
    "BONKUSDT":  "BONKUSDT",
    "FLOKIUSDT": "FLOKIUSDT",
    "SHIBUSDT":  "SHIBUSDT",
    "PUMPUSDT":  "1000PUMPUSDT",
    "WLFIUSDT":  "WLFIUSDT",
    "WIFUSDT":   "WIFUSDT",
}

EMA_PERIOD = 50      # период EMA для фильтра тренда (EMA50 на тренд-ТФ)
EMA_OFF = "Выключен"
TREND_CACHE_TTL = 60

FAST_PERIOD = 5      # проверка позиций каждые 5 секунд

# ---- зафиксированная конфигурация (эталон БингХ v4) ----
# DRY-RUN: бот считает сигналы, входы, тейки и переносы стопа и пишет всё
# в журнал, но ордера на биржу не отправляются.
# Для реальной торговли поставьте False.
DRY_RUN = True
DRY_RUN_BALANCE = 1000.0  # стартовый расчётный баланс для DRY-RUN (компаунд)

# Риск: КОМПАУНД 1.5% от ТЕКУЩЕГО баланса на сделку (эталон v4)
RISK_PCT = 0.015

# SFP-тренд (v4): тейки 1.00 / 1.60 R долями (0.5, 0.25) + хвост 25%
# позиции трейлингом за EMA20 рабочего ТФ (после исполнения всех тейков)
TP_TREND = [(1.0, 0.5), (1.6, 0.25)]   # (R, доля)
TREND_TAIL_SHARE = 0.25
TREND_TRAIL_EMA = 20

# Диапазонный модуль (v4): тейки 0.5 / 1.0 R долями (0.5, 0.5),
# BE (перенос стопа в вход) сразу после ТП1.
RANGE_DAYS = 3
RANGE_TOUCH_TOL = 0.0015    # допуск касания границы канала, 0.15%
RANGE_VOL_MULT = 1.3
RANGE_STOP_ADD = 0.008      # стоп на 0.8% за границей канала
TP_RANGE = [(0.5, 0.5), (1.0, 0.5)]

# SFP-тренд: свинг-окно 20 баров (валидация v3.1)
SFP_SWING = 20

RISK_CAP_LEVERAGE = 10.0   # лимит расчёта бота (в приложении биржи — 5x Isolated)
MIN_STOP_PCT = 0.8         # стоп ближе 0.8% — сигнал пропускаем («пыль»)
MAX_STOPS_PER_PAIR = 3     # стопов на одну пару в день
MAX_STOPS_BASKET = 5       # стопов на всю корзину в день (v4)
ENTRY_WAIT = 3             # свечей на исполнение лимит-входа (иначе отмена)


def state_load():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def state_save(data):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def round_step(value, step):
    if not step or step <= 0:
        return value
    s = Decimal(str(step))
    n = (Decimal(str(value)) / s).to_integral_value(rounding=ROUND_DOWN)
    return float(n * s)


def qstr(x):
    s = f"{float(x):.10f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _fmt_frac(w):
    return {0.5: "1/2", 0.25: "1/4"}.get(w, f"{w:.2f}")


def calc_tps(entry, stop, side, offsets):
    """Тейк-уровни по смещениям в R (v4: тренд (1.0, 1.6), диапазон (0.5, 1.0))."""
    r = abs(entry - stop)
    if r <= 0:
        r = entry * 0.01
    if side == "LONG":
        return [entry + o * r for o in offsets]
    return [entry - o * r for o in offsets]


def ema(values, period):
    """EMA по списку закрытий (для фильтра EMA50 тренд-ТФ)."""
    if len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def ema_series(values, n):
    """Серия EMA (как в эталоне walk): старт с первого значения."""
    if len(values) < n:
        return []
    k = 2.0 / (n + 1)
    out, e = [], values[0]
    for v in values:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def pick_signal(rsig, ssig):
    """v4: обе стратегии активны на каждом баре, детектор-шлюз убран.
    Конфликт: оба сигнала в одну сторону -> приоритет ДИАПАЗОНА;
    разные направления -> пропуск (канал сломан). Висящий pend-ордер
    новым сигналом не перебивается (FIFO — проверка до генерации)."""
    if rsig is not None and ssig is not None:
        if rsig[0] == ssig[0]:
            return "range", rsig
        return None, None          # разнонаправленные -> пропуск
    if rsig is not None:
        return "range", rsig
    if ssig is not None:
        return "trend", ssig
    return None, None


class BybitAPI:
    def __init__(self, api_key, secret, demo, interval):
        self.api_key, self.secret = api_key, secret
        self.base = BYBIT_DEMO if demo else BYBIT_REAL
        self.interval = interval
        self.aliases = BYBIT_ALIASES
        self.reverse = {v: k for k, v in BYBIT_ALIASES.items()}
        self._lots = None

    def sym(self, s):
        return self.aliases.get(s, s)

    def to_universal(self, s):
        return self.reverse.get(s, s)

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
        if method == "GET":
            qs = urllib.parse.urlencode(params)
            r = requests.get(f"{self.base}{path}?{qs}",
                             headers=self._headers(qs), timeout=15)
        else:
            body = json.dumps(params)
            r = requests.post(f"{self.base}{path}", data=body,
                              headers=self._headers(body), timeout=15)
        if not r.text.strip():
            raise RuntimeError("пустой ответ — проверьте ключ и права")
        d = r.json()
        if d.get("retCode") != 0:
            raise RuntimeError(f"Bybit: {d.get('retMsg')}")
        return d["result"]

    def _load_lots(self):
        if self._lots is not None:
            return self._lots
        try:
            d = self.req("GET", "/v5/market/instruments-info",
                         {"category": "linear"})
            lots = {}
            for it in d.get("list", []):
                f = it.get("lotSizeFilter", {})
                pf = it.get("priceFilter", {})
                lots[it["symbol"]] = {
                    "min": float(f.get("minOrderQty") or 0),
                    "qstep": float(f.get("qtyStep") or 0),
                    "tick": float(pf.get("tickSize") or 0),
                    "max": float(f.get("maxMktOrderQty") or 0),
                }
            self._lots = lots
        except Exception:
            self._lots = None
        return self._lots or {}

    def lot_info(self, symbol):
        info = self._load_lots().get(self.sym(symbol))
        if info is not None:
            return info
        try:
            d = self.req("GET", "/v5/market/instruments-info",
                         {"category": "linear", "symbol": self.sym(symbol)})
            for it in d.get("list", []):
                f = it.get("lotSizeFilter", {})
                pf = it.get("priceFilter", {})
                info = {
                    "min": float(f.get("minOrderQty") or 0),
                    "qstep": float(f.get("qtyStep") or 0),
                    "tick": float(pf.get("tickSize") or 0),
                    "max": float(f.get("maxMktOrderQty") or 0),
                }
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
        info = self.lot_info(symbol)
        if not info or not info.get("tick"):
            return round(price, 6)
        return round_step(price, info["tick"])

    def candles(self, symbol, limit=30, interval=None):
        d = self.req("GET", "/v5/market/kline",
                     {"category": "linear", "symbol": self.sym(symbol),
                      "interval": interval or self.interval, "limit": limit})
        rows = [(float(k[1]), float(k[2]), float(k[3]),
                 float(k[4]), float(k[5])) for k in d["list"]]
        return list(reversed(rows))

    def price(self, symbol):
        d = self.req("GET", "/v5/market/tickers",
                     {"category": "linear", "symbol": self.sym(symbol)})
        return float(d["list"][0]["lastPrice"])

    def balance(self):
        for acct in ("UNIFIED", "CONTRACT"):
            try:
                d = self.req("GET", "/v5/account/wallet-balance",
                             {"accountType": acct})
                for coin in d["list"][0]["coin"]:
                    if coin["coin"] == "USDT":
                        return float(coin["walletBalance"])
            except Exception:
                continue
        return 0.0

    def open_positions(self):
        d = self.req("GET", "/v5/position/list",
                     {"category": "linear", "settleCoin": "USDT"})
        out = {}
        for p in d.get("list", []):
            if float(p.get("size", 0)) == 0:
                continue
            out[self.to_universal(p["symbol"])] = {
                "side": "LONG" if p["side"] == "Buy" else "SHORT",
                "qty": float(p["size"]),
                "entry": float(p["avgPrice"]),
            }
        return out

    def pending_exit_prices(self, symbol):
        sym = self.sym(symbol)
        stop = tp = None
        try:
            d = self.req("GET", "/v5/order/realtime",
                         {"category": "linear", "symbol": sym,
                          "openOnly": 0, "orderFilter": "StopOrder"})
            for o in d.get("list", []):
                if o.get("reduceOnly") and o.get("triggerPrice"):
                    stop = float(o["triggerPrice"])
                    break
        except Exception:
            pass
        try:
            d = self.req("GET", "/v5/order/realtime",
                         {"category": "linear", "symbol": sym,
                          "openOnly": 0, "orderFilter": "Order"})
            for o in d.get("list", []):
                if o.get("reduceOnly") and o.get("orderType") == "Limit":
                    tp = float(o["price"])
                    break
        except Exception:
            pass
        return stop, tp

    def open_reduce_orders(self, symbol):
        sym = self.sym(symbol)
        out = []
        for filt in ("StopOrder", "Order"):
            try:
                d = self.req("GET", "/v5/order/realtime",
                             {"category": "linear", "symbol": sym,
                              "openOnly": 0, "orderFilter": filt})
                for o in d.get("list", []):
                    if o.get("reduceOnly") and o.get("orderId"):
                        out.append(o["orderId"])
            except Exception:
                pass
        return out

    def cancel(self, symbol, order_id):
        try:
            self.req("POST", "/v5/order/cancel",
                     {"category": "linear", "symbol": self.sym(symbol),
                      "orderId": order_id})
            return True
        except Exception:
            return False

    def order(self, symbol, side, qty, stop=None, tp=None, reduce_only=False):
        p = {"category": "linear", "symbol": self.sym(symbol),
             "side": side, "qty": qstr(qty), "positionIdx": 0}
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
        return self.req("POST", "/v5/order/create", p)


class BingXAPI:
    def __init__(self, api_key, secret, demo, interval):
        self.api_key, self.secret = api_key, secret
        self.base = BINGX_DEMO if demo else BINGX_REAL
        self.interval = interval
        self.aliases = BINGX_ALIASES
        self.reverse = {v: k for k, v in BINGX_ALIASES.items()}
        self._lots = None

    def sym(self, s):
        return self.aliases.get(s, s)

    def to_universal(self, s):
        s = self.reverse.get(s, s)
        for q in ("USDT", "USDC", "BTC"):
            if s.endswith(q):
                return s[:-len(q)] + q
        return s

    def _query(self, params):
        p = dict(params)
        p["timestamp"] = str(int(time.time() * 1000))
        p["recvWindow"] = "5000"
        qs = urllib.parse.urlencode(sorted(p.items()))
        sign = hmac.new(self.secret.encode(), qs.encode(),
                        hashlib.sha256).hexdigest()
        return qs, {"X-BX-APIKEY": self.api_key,
                    "X-BX-SIGNATURE": sign,
                    "X-BX-TIMESTAMP": p["timestamp"]}

    def req(self, method, path, params=None):
        qs, headers = self._query(params or {})
        url = f"{self.base}{path}?{qs}"
        r = requests.post(url, headers=headers, timeout=15) if method == "POST" \
            else requests.get(url, headers=headers, timeout=15)
        if not r.text.strip():
            raise RuntimeError("пустой ответ — проверьте ключ и права")
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError(f"BingX: {d.get('msg')}")
        return d.get("data")

    def fmt(self, symbol):
        s = self.sym(symbol)
        for q in ("USDT", "USDC", "BTC"):
            if s.endswith(q):
                return s[:-len(q)] + "-" + q
        return s

    def _load_lots(self):
        if self._lots is not None:
            return self._lots
        try:
            d = self.req("GET", "/openApi/swap/v2/quote/contracts", {})
            lots = {}
            for it in d or []:
                sym = (it.get("symbol") or "").replace("-", "")
                try:
                    prec_q = int(it.get("quantityPrecision", 8))
                except Exception:
                    prec_q = 8
                try:
                    prec_p = int(it.get("pricePrecision", 8))
                except Exception:
                    prec_p = 8
                lots[sym] = {
                    "min": float(it.get("tradeMinQuantity", 0) or 0),
                    "qstep": 10 ** (-prec_q) if prec_q > 0 else 1.0,
                    "tick": 10 ** (-prec_p) if prec_p > 0 else 0.0,
                    "min_usdt": float(it.get("tradeMinUSDT", 0) or 0),
                }
            self._lots = lots
        except Exception:
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
        info = self._load_lots().get(self.sym(symbol))
        if info is None:
            self._lots = None
            info = self._load_lots().get(self.sym(symbol))
        if not info or not info.get("tick"):
            return round(price, 6)
        return round_step(price, info["tick"])

    def candles(self, symbol, limit=30, interval=None):
        d = self.req("GET", "/openApi/swap/v2/quote/kline",
                     {"symbol": self.fmt(symbol),
                      "interval": interval or self.interval,
                      "limit": limit})
        rows = [(float(k["o"]), float(k["h"]), float(k["l"]),
                 float(k["c"]), float(k["v"])) for k in d]
        rows.sort(key=lambda x: x[0])
        return rows

    def price(self, symbol):
        d = self.req("GET", "/openApi/swap/v2/quote/ticker",
                     {"symbol": self.fmt(symbol)})
        return float(d[0]["lastPrice"])

    def balance(self):
        try:
            d = self.req("GET", "/openApi/swap/v2/user/balance", {})
            if isinstance(d, dict):
                for key in ("balance", "usableMargin", "accountMargin"):
                    if d.get(key) not in (None, ""):
                        return float(d[key])
            elif isinstance(d, list):
                for coin in d:
                    if coin.get("coin") == "USDT":
                        return float(coin.get("balance") or 0)
        except Exception:
            pass
        return 0.0

    def open_positions(self):
        try:
            d = self.req("GET", "/openApi/swap/v2/user/positions", {})
        except Exception:
            return {}
        out = {}
        for p in d or []:
            amt = abs(float(p.get("positionAmt", 0) or 0))
            if amt == 0:
                continue
            out[self.to_universal(p["symbol"])] = {
                "side": p.get("side", "LONG").upper(),
                "qty": amt,
                "entry": float(p.get("avgPrice", 0) or 0),
            }
        return out

    def pending_exit_prices(self, symbol):
        sym = self.fmt(symbol)
        stop = tp = None
        try:
            d = self.req("GET", "/openApi/swap/v2/trade/openOrders",
                         {"symbol": sym})
            for o in d or []:
                if o.get("reduceOnly") in (True, "true", 1):
                    if o.get("type") == "TRIGGER_MARKET" and o.get("stopPrice"):
                        stop = float(o["stopPrice"])
                    elif o.get("type") == "LIMIT" and o.get("price")
                        tp = float(o["price"])
        except Exception:
            pass
        return stop, tp

    def open_reduce_orders(self, symbol):
        out = []
        try:
            d = self.req("GET", "/openApi/swap/v2/trade/openOrders",
                         {"symbol": self.fmt(symbol)})
            for o in d or []:
                if o.get("reduceOnly") in (True, "true", 1):
                    oid = o.get("orderId") or o.get("id")
                    if oid:
                        out.append(oid)
        except Exception:
            pass
        return out

    def cancel(self, symbol, order_id):
        try:
            self.req("POST", "/openApi/swap/v2/trade/cancelOrder",
                     {"symbol": self.fmt(symbol), "orderId": str(order_id)})
            return True
        except Exception:
            return False

    def order(self, symbol, side, qty, stop=None, tp=None, reduce_only=False):
        p = {"symbol": self.fmt(symbol),
             "side": "BUY" if side == "Buy" else "SELL",
             "positionSide": "ONE_WAY",
             "qty": qstr(qty)}
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
            p["reduceOnly"] = True
        return self.req("POST", "/openApi/swap/v2/trade/order", p)


class Bot:
    def __init__(self, api, tf_label, trend_label, symbols, risk_pct,
                 max_losses_per_day, max_positions, log,
                 ema_proximity_pct=0.0,
                 load_state=None, save_state=None):
        self.api = api
        self.tf_label = tf_label
        self.vol_mult = TIMEFRAMES[tf_label]["vol_mult"]
        # risk_pct — доля риска на сделку (0.015 = 1.5%), КОМПАУНД:
        # объём считается от ТЕКУЩЕГО баланса (в DRY-RUN — расчётного)
        self.deposit_pct = risk_pct
        self.trend_label = trend_label
        self.trend_enabled = (trend_label != EMA_OFF)
        self.ema_proximity_pct = ema_proximity_pct   # NO_CHASE, %
        if self.trend_enabled:
            if api.base in (BYBIT_REAL, BYBIT_DEMO):
                self.trend_interval = TIMEFRAMES[trend_label]["interval_bybit"]
            else:
                self.trend_interval = TIMEFRAMES[trend_label]["interval_bingx"]
        else:
            self.trend_interval = None
        self.trend_cache = {}
        self.range_bars = max(30, RANGE_DAYS * 1440 // TF_MINUTES.get(tf_label, 15))
        self.symbols = symbols
        self.max_losses = max_losses_per_day   # корзинный лимит (v4: 5)
        self.max_positions = max_positions
        self.log = log
        self._load_state = load_state
        self._save_state = save_state
        self.stop_event = threading.Event()
        self.day = dt.date.today()
        self.losses_today = 0
        self.losses_by_pair = {}
        self.positions = {}
        self.dry_balance = DRY_RUN_BALANCE   # компаунд в DRY-RUN
        self.stat_signals = 0
        self.stat_skipped = 0
        self.stat_entries = 0
        self.stat_range_signals = 0
        self.stat_range_entries = 0
        self.last_stat_hour = None
        self.stat_out = {"СТОП": 0, "БЕЗУБЫТОК": 0, "ТРЕЙЛ": 0,
                         "ТЕЙК-ПОЛНЫЙ": 0, "ПРОЧЕЕ": 0}
        self.stat_r = 0.0
        self.stat_r_usdt = 0.0

    def load_losses(self):
        if not self._load_state:
            return
        try:
            st = self._load_state()
            if st and st.get("date") == dt.date.today().isoformat():
                self.losses_today = int(st.get("losses", 0))
                self.losses_by_pair = dict(st.get("losses_by_pair") or {})
                if self.losses_today:
                    self.log(f"Восстановлен счётчик стопов за сегодня: {self.losses_today}")
        except Exception:
            pass

    def persist_losses(self):
        if not self._save_state:
            return
        try:
            self._save_state({"date": dt.date.today().isoformat(),
                              "losses": self.losses_today,
                              "losses_by_pair": self.losses_by_pair})
        except Exception:
            pass

    def log_stats_if_due(self):
        hour = dt.datetime.now().hour
        if self.last_stat_hour is not None and hour == self.last_stat_hour:
            return
        self.last_stat_hour = hour
        o = self.stat_out
        trend_part = (f"пропущено EMA — {self.stat_skipped}, "
                      if self.trend_enabled else "EMA-фильтр отключён, ")
        self.log(f"Статистика за {self.day.strftime('%d.%m.%Y')}: "
                 f"сигналов SFP — {self.stat_signals}, {trend_part}"
                 f"входов SFP — {self.stat_entries}; диапазонных сигналов — "
                 f"{self.stat_range_signals}, входов — {self.stat_range_entries}. "
                 f"Сделки: СТОП — {o['СТОП']}, БЕЗУБЫТОК — {o['БЕЗУБЫТОК']}, "
                 f"ТРЕЙЛ — {o['ТРЕЙЛ']}, полный тейк — {o['ТЕЙК-ПОЛНЫЙ']}, "
                 f"прочее — {o['ПРОЧЕЕ']}. "
                 f"Суммарно за день: {self.stat_r:+.2f}R "
                 f"({self.stat_r_usdt:+.2f} USDT)")

    def close_stats(self, symbol, p, category, cur=None):
        """Закрытая сделка в дневной статистике. Схемы v4:
        тренд — вклады тейков 1.0*0.5 + 1.6*0.25 = 0.90R, хвост 0.25;
        диапазон — 0.5*0.5 + 1.0*0.5 = 0.75R, хвоста нет."""
        if p.get("scheme") == "RANGE":
            contrib = [r * w for (r, w) in TP_RANGE]
            splits = [w for (_, w) in TP_RANGE]
        else:
            contrib = [r * w for (r, w) in TP_TREND]
            splits = [w for (_, w) in TP_TREND]
        filled = sum(p["tp_filled"])
        r_total = sum(contrib[:filled])
        remaining = 1.0 - sum(splits[:filled])
        risk = p.get("risk") or 0.0
        if cur is not None and risk > 0 and remaining > 0:
            if p["side"] == "LONG":
                r_close = (cur - p["entry"]) / risk
            else:
                r_close = (p["entry"] - cur) / risk
        else:
            r_close = -1.0 if category == "СТОП" else 0.0
        r_total += remaining * r_close

        r_usdt = abs(p["entry"] - p["stop_price"]) * (p.get("qty_total") or 0.0)
        usdt_total = r_total * r_usdt
        if DRY_RUN:
            self.dry_balance += usdt_total   # компаунд расчётного баланса

        self.stat_out[category] = self.stat_out.get(category, 0) + 1
        self.stat_r += r_total
        self.stat_r_usdt += usdt_total
        self.log(f"Итог сделки {symbol} [{p.get('scheme', 'SFP')}]: {category}, "
                 f"результат {r_total:+.2f}R ({usdt_total:+.2f} USDT) "
                 f"(тейков исполнено: {filled}/{len(splits)}), "
                 f"за день {self.stat_r:+.2f}R ({self.stat_r_usdt:+.2f} USDT)")

    def reset_daily_stats(self):
        self.day = dt.date.today()
        self.losses_today = 0
        self.losses_by_pair = {}
        self.stat_signals = 0
        self.stat_skipped = 0
        self.stat_entries = 0
        self.stat_range_signals = 0
        self.stat_range_entries = 0
        self.last_stat_hour = None
        self.stat_out = {"СТОП": 0, "БЕЗУБЫТОК": 0, "ТРЕЙЛ": 0,
                         "ТЕЙК-ПОЛНЫЙ": 0, "ПРОЧЕЕ": 0}
        self.stat_r = 0.0
        self.stat_r_usdt = 0.0
        self.persist_losses()

    def cleanup_orders(self, symbol):
        try:
            oids = self.api.open_reduce_orders(symbol)
        except Exception as ex:
            self.log(f"{symbol}: не удалось получить ордера для снятия — {ex}")
            return
        for oid in oids:
            if self.api.cancel(symbol, oid):
                self.log(f"{symbol}: снят оставшийся ордер {oid}")

    def place_stop(self, symbol, p, qty, stop_price):
        if DRY_RUN:
            return None
        try:
            res = self.api.order(symbol,
                                 "Sell" if p["side"] == "LONG" else "Buy",
                                 qty, stop=stop_price, reduce_only=True)
            return res.get("orderId") if isinstance(res, dict) else None
        except Exception as ex:
            self.log(f"{symbol}: стоп-ордер не выставлен — {ex}. "
                     f"Страховочное закрытие на стороне бота активно")
            return None

    def move_stop(self, symbol, p, new_stop, new_qty):
        if DRY_RUN:
            p["stop_price"] = new_stop
            return
        new_stop = self.api.price_ok(symbol, new_stop)
        if p.get("stop_id"):
            self.api.cancel(symbol, p["stop_id"])
        p["stop_price"] = new_stop
        p["stop_id"] = None
        oid = self.place_stop(symbol, p, new_qty, new_stop)
        p["stop_id"] = oid
        if oid:
            self.log(f"Стоп по {symbol} перенесён на {qstr(new_stop)} "
                     f"(объём {qstr(new_qty)})")
        else:
            self.log(f"{symbol}: перенос стопа не удался — старый стоп "
                     f"мог остаться, проверьте вручную")

    def place_tps(self, symbol, p):
        """Лимитные тейки по долям схемы: тренд 1/2 + 1/4 (хвост 1/4
        трейлится за EMA20 без тейк-ордера), диапазон 1/2 + 1/2."""
        if DRY_RUN:
            return 0
        side = p["side"]
        qty = p["qty_total"]
        splits = p["splits"]
        placed = 0
        q_left = qty
        for i, (w, tp) in enumerate(zip(splits, p["tp_levels"])):
            if i < len(splits) - 1:
                q = self.api.qty_ok(symbol, qty * w, price=p["entry"])
            else:
                q = self.api.qty_ok(symbol, q_left, price=p["entry"])
            if q <= 0:
                self.log(f"{symbol}: тейк {i + 1} ниже минимума биржи — пропущен")
                continue
            try:
                self.api.order(symbol, "Sell" if side == "LONG" else "Buy",
                               q, tp=tp, reduce_only=True)
                placed += 1
            except Exception as ex:
                self.log(f"{symbol}: тейк {i + 1} не выставлен — {ex}")
            q_left -= q
        if placed == 0:
            self.log(f"{symbol}: тейки не выставлены — сопровождение по стопу")
        return placed

    def get_ema20(self, symbol):
        """EMA20 рабочего ТФ для трейлинга хвоста тренда. Последнее значение
        или None при нехватке свечей."""
        try:
            c = self.api.candles(symbol, limit=TREND_TRAIL_EMA * 3)
        except Exception:
            return None
        closes = [x[3] for x in c]
        s = ema_series(closes, TREND_TRAIL_EMA)
        return s[-1] if s else None

    def recover(self):
        if DRY_RUN:
            self.load_losses()
            return
        try:
            open_pos = self.api.open_positions()
        except Exception as ex:
            self.log(f"Не удалось получить открытые позиции: {ex}")
            return
        for sym, info in open_pos.items():
            if sym not in self.symbols:
                continue
            side, entry, qty = info["side"], info["entry"], info["qty"]
            stop, _ = self.api.pending_exit_prices(sym)
            for oid in self.api.open_reduce_orders(sym):
                self.api.cancel(sym, oid)
            if not stop or stop == entry:
                stop = entry - entry * 0.01 if side == "LONG" else entry + entry * 0.01
                self.log(f"{sym}: биржевой стоп не найден — взят расчётный 1%")
            stop = self.api.price_ok(sym, stop)
            offsets = [r for (r, _) in TP_TREND]
            tps = [self.api.price_ok(sym, t) for t in
                   calc_tps(entry, stop, side, offsets)]
            self.positions[sym] = {
                "phase": "ACTIVE",
                "scheme": "SFP", "splits": [w for (_, w) in TP_TREND],
                "side": side, "entry": entry, "stop_price": stop,
                "risk": abs(entry - stop),
                "stop_id": None, "tp_levels": tps,
                "tp_filled": [False] * len(TP_TREND),
                "tail": False,
                "qty_total": qty, "qty": qty,
            }
            self.positions[sym]["stop_id"] = self.place_stop(
                sym, self.positions[sym], qty, stop)
            self.place_tps(sym, self.positions[sym])
            self.log(f"Восстановлена позиция {side} {sym} по {qstr(entry)}, "
                     f"объём {qstr(qty)}: стоп {qstr(stop)}, "
                     f"тейки {' / '.join(qstr(t) for t in tps)} "
                     f"(хвост 25% — трейлинг EMA{TREND_TRAIL_EMA})")
        if open_pos:
            self.log(f"Итого восстановлено позиций: {len(open_pos)}")
        self.load_losses()

    def trend_ok(self, symbol, side, entry):
        """EMA50 тренд-ТФ + NO_CHASE (как в эталоне v4):
        LONG — цена выше EMA и не дальше N% в сторону сигнала,
        SHORT — ниже EMA и не дальше N%."""
        if not self.trend_enabled:
            return True
        e = self.get_ema_trend(symbol)
        if e is None:
            self.log(f"{symbol}: мало свечей для EMA{EMA_PERIOD} — сигнал пропущен")
            return False
        if e <= 0:
            return False
        dev = (entry - e) / e * 100.0
        if side == "SHORT":
            if dev >= 0:
                return False
            if self.ema_proximity_pct > 0 and dev < -self.ema_proximity_pct:
                return False
        else:
            if dev <= 0:
                return False
            if self.ema_proximity_pct > 0 and dev > self.ema_proximity_pct:
                return False
        return True

    def get_ema_trend(self, symbol):
        now = time.time()
        cached = self.trend_cache.get(symbol)
        if cached and now - cached[0] < TREND_CACHE_TTL:
            return cached[1]
        try:
            c = self.api.candles(symbol, limit=EMA_PERIOD * 2,
                                 interval=self.trend_interval)
        except Exception as ex:
            self.log(f"{symbol}: EMA не получена ({ex}) — сигнал пропущен")
            return None
        closes = [x[3] for x in c]
        e = ema(closes, EMA_PERIOD)
        if e is not None:
            self.trend_cache[symbol] = (now, e)
        return e

    def sfp_signal(self, symbol):
        """SFP-тренд (v4): свайп экстремума за SFP_SWING закрытых баров с
        возвратом, объём x vol_mult среднего. Вход — уровень свинга,
        стоп — за свайпнутым экстремумом (max(h, swing)*1.001 / min(l, ...)*0.999).
        Килл-зоны отключены — круглосуточно."""
        c = self.api.candles(symbol, limit=SFP_SWING + 2)
        if len(c) < SFP_SWING + 1:
            return None, None, None
        window = c[-(SFP_SWING + 1):-1]
        sw_lo = min(x[2] for x in window)
        sw_hi = max(x[1] for x in window)
        avg_vol = sum(x[4] for x in window) / len(window)
        last = c[-1]
        if last[4] < self.vol_mult * avg_vol:
            return None, None, None
        if last[1] > sw_hi and last[3] < sw_hi:
            return "SHORT", sw_hi, max(last[1], sw_hi) * 1.001
        if last[2] < sw_lo and last[3] > sw_lo:
            return "LONG", sw_lo, min(last[2], sw_lo) * 0.999
        return None, None, None

    def range_signal(self, symbol):
        """Диапазонный модуль (v4): канал RANGE_DAYS дней, >=2 касаний,
        реверс от границы с закрытием внутрь, объём x1.3 среднего.
        Вход — граница канала (лимит), стоп за границей (+0.8%)."""
        need = self.range_bars + 2
        c = self.api.candles(symbol, limit=need)
        if len(c) < need:
            return None, None, None
        hist = c[-(self.range_bars + 1):-1]
        upper = max(x[1] for x in hist)
        lower = min(x[2] for x in hist)
        avg_vol = sum(x[4] for x in hist) / len(hist)
        tol = RANGE_TOUCH_TOL
        touch_up = sum(1 for x in hist if x[1] >= upper * (1 - tol))
        touch_dn = sum(1 for x in hist if x[2] <= lower * (1 + tol))
        last = c[-1]
        if last[4] < RANGE_VOL_MULT * avg_vol:
            return None, None, None
        if (touch_up >= 2 and last[1] >= upper * (1 - tol) and
                last[3] < upper * (1 - tol)):
            return "SHORT", upper, upper * (1 + RANGE_STOP_ADD)
        if (touch_dn >= 2 and last[2] <= lower * (1 + tol) and
                last[3] > lower * (1 + tol)):
            return "LONG", lower, lower * (1 - RANGE_STOP_ADD)
        return None, None, None

    def try_enter(self, symbol, side, entry, stop, scheme):
        """Вход лимитом по УРОВНЮ СИГНАЛА (как в эталоне: fill только при
        касании границы/свинга). Риск — компаунд 1.5% от текущего баланса."""
        entry = self.api.price_ok(symbol, entry)
        stop = self.api.price_ok(symbol, stop)
        risk = abs(entry - stop)
        if risk <= 0:
            return
        stop_frac = risk / entry
        if stop_frac * 100.0 < MIN_STOP_PCT:
            self.log(f"{symbol}: стоп {stop_frac * 100:.2f}% — ближе минимальных "
                     f"{MIN_STOP_PCT}% — сигнал пропущен")
            return
        bal = self.dry_balance if DRY_RUN else self.api.balance()
        if bal <= 0:
            return
        notional = bal * self.deposit_pct / stop_frac
        notional = min(notional, bal * RISK_CAP_LEVERAGE)
        raw_qty = notional / entry
        qty = self.api.qty_ok(symbol, raw_qty, price=entry)
        if qty <= 0:
            self.log(f"{symbol}: объём {raw_qty:.6f} меньше минимума биржи — пропущено")
            return
        scheme_cfg = TP_RANGE if scheme == "RANGE" else TP_TREND
        offsets = [r for (r, _) in scheme_cfg]
        splits = [w for (_, w) in scheme_cfg]
        tps = [self.api.price_ok(symbol, t)
               for t in calc_tps(entry, stop, side, offsets)]
        oid = None
        if not DRY_RUN:
            try:
                res = self.api.order(symbol, "Buy" if side == "LONG" else "Sell",
                                     qty, tp=entry)
                oid = res.get("orderId") if isinstance(res, dict) else None
            except Exception as ex:
                self.log(f"{symbol}: лимит-вход не выставлен — {ex}")
                return
        self.positions[symbol] = {
            "phase": "WAIT_FILL",
            "placed_ts": time.time(),
            "order_id": oid,
            "scheme": scheme, "splits": splits,
            "side": side, "entry": entry, "stop_price": stop,
            "risk": risk, "stop_id": None, "tp_levels": tps,
            "tp_filled": [False] * len(splits),
            "tail": False,
            "qty_total": qty, "qty": qty,
        }
        tag = "DRY-RUN " if DRY_RUN else ""
        tp_txt = " / ".join(qstr(t) for t in tps)
        split_txt = ", ".join(_fmt_frac(w) for w in splits)
        self.log(f"{tag}ЛИМИТ-ВХОД {side} {symbol} [{scheme}] по {qstr(entry)}, "
                 f"стоп {qstr(stop)} ({stop_frac * 100:.2f}%), "
                 f"тейки: {tp_txt} ({split_txt})"
                 + (f" + хвост 1/4 трейлинг EMA{TREND_TRAIL_EMA}"
                    if scheme == "SFP" else "")
                 f" [{len(self.positions)}/{self.max_positions}]")

    def scan_signals(self):
        """v4: обе стратегии активны на каждом баре. Конфликт решает
        pick_signal: совпадение -> диапазон, разнонаправленные -> пропуск.
        Висящий лимит-ордер не перебивается (позиция/pend уже есть — пропуск)."""
        if self.losses_today >= self.max_losses:
            return
        if len(self.positions) >= self.max_positions:
            return
        for symbol in self.symbols:
            if self.stop_event.is_set():
                break
            if symbol in self.positions:
                continue
            if len(self.positions) >= self.max_positions:
                break
            if self.losses_by_pair.get(symbol, 0) >= MAX_STOPS_PER_PAIR:
                continue
            try:
                ssig = self.sfp_signal(symbol)
                if ssig[0]:
                    self.stat_signals += 1
                    if not self.trend_ok(symbol, ssig[0], ssig[1]):
                        self.stat_skipped += 1
                        if self.trend_enabled:
                            self.log(f"{symbol}: SFP {ssig[0]} против тренда или "
                                     f"вдали от EMA (EMA{EMA_PERIOD} на "
                                     f"{self.trend_label}) — пропущено")
                        ssig = (None, None, None)
                rsig = self.range_signal(symbol)
                kind, sig = pick_signal(rsig, ssig)
                if kind is None:
                    continue
                side, entry, stop = sig
                if kind == "range":
                    self.stat_range_signals += 1
                    self.try_enter(symbol, side, entry, stop, "RANGE")
                else:
                    self.try_enter(symbol, side, entry, stop, "SFP")
            except Exception as ex:
                self.log(f"{symbol}: ошибка запроса — {ex}")
                continue
        self.log_stats_if_due()

    def run(self):
        if self.trend_enabled:
            trend_info = f"тренд: EMA{EMA_PERIOD} на {self.trend_label}"
            if self.ema_proximity_pct > 0:
                trend_info += (f", вход не дальше {self.ema_proximity_pct}% от EMA")
            else:
                trend_info += ", только сторона тренда"
        else:
            trend_info = "EMA-фильтр выключен"
        mode_info = "DRY-RUN (ордера НЕ отправляются)" if DRY_RUN else "РЕАЛЬНЫЙ РЕЖИМ"
        self.log(WARNING_ISOLATED)
        self.log(f"Бот запущен ({self.api.base}). {mode_info}. ТФ: {self.tf_label}")
        self.log(f"v4: килл-зоны отключены, торговля 24/7. "
                 f"Конфликт сигналов: совпадение -> ДИАПАЗОН, разнонаправленные -> пропуск")
        self.log(f"Диапазон: канал {RANGE_DAYS} дн. ({self.range_bars} бар), "
                 f"касания >=2, объём x{RANGE_VOL_MULT}, вход лимитом по границе, "
                 f"стоп за границей, тейки 0.5R/1.0R долями 1/2, 1/2, "
                 f"BE сразу после ТП1")
        self.log(f"SFP-тренд: свинг {SFP_SWING} бар, объём x{self.vol_mult}, "
                 f"{trend_info}. Тейки 1.0R (1/2) и 1.6R (1/4), BE после ТП1, "
                 f"хвост 25% трейлингом EMA{TREND_TRAIL_EMA}")
        self.log(f"Риск {self.deposit_pct * 100:.1f}% КОМПАУНД от текущего баланса "
                 f"(плечо расчёта до x{RISK_CAP_LEVERAGE:.0f}). "
                 f"Вход лимитом, ожидание {ENTRY_WAIT} свечей. "
                 f"Лимиты стопов: {MAX_STOPS_PER_PAIR}/пара, "
                 f"{self.max_losses}/корзина в день. "
                 f"Пары: {', '.join(self.symbols)}. "
                 f"Лимит позиций: {self.max_positions}. "
                 f"Позиции проверяются каждые {FAST_PERIOD} сек")
        self.log(MOSCOW_HINT)
        if DRY_RUN:
            self.log(f"DRY-RUN: стартовый баланс {DRY_RUN_BALANCE:.0f} USDT "
                     f"(компаунд), риск {DRY_RUN_BALANCE * self.deposit_pct:.2f} "
                     f"USDT на первую сделку")
        else:
            try:
                bal = self.api.balance()
                self.log(f"Баланс: {bal:.2f} USDT — API-доступ подтверждён")
            except Exception as ex:
                self.log(f"[API FAIL] Баланс не получен: {ex}")
        self.recover()
        loop_count = 0
        while not self.stop_event.is_set():
            try:
                self.manage_positions()
            except Exception as ex:
                self.log(f"Ошибка сопровождения позиций: {ex}")
            loop_count += 1
            if loop_count % 12 == 0:
                try:
                    self.scan_signals()
                    self.log(f"Цикл выполнен. Открытых позиций: {len(self.positions)}")
                except Exception as ex:
                    self.log(f"Ошибка: {ex}")
            for _ in range(FAST_PERIOD):
                if self.stop_event.is_set():
                    break
                time.sleep(1)
        self.log("Бот остановлен")

    def manage_positions(self):
        if self.day != dt.date.today():
            self.reset_daily_stats()
        if DRY_RUN:
            self._manage_dry()
            return
        try:
            open_pos = self.api.open_positions()
        except Exception as ex:
            self.log(f"Ошибка получения позиций: {ex}")
            open_pos = None

        for symbol in list(self.positions):
            p = self.positions[symbol]
            splits = p["splits"]
            n_tps = len(splits)

            if p.get("phase") == "WAIT_FILL":
                filled = open_pos.get(symbol) if open_pos is not None else None
                if filled is not None:
                    if p.get("order_id"):
                        self.api.cancel(symbol, p["order_id"])
                    qty = filled["qty"]
                    p["qty_total"] = qty
                    p["qty"] = qty
                    p["stop_id"] = self.place_stop(symbol, p, qty, p["stop_price"])
                    self.place_tps(symbol, p)
                    p["phase"] = "ACTIVE"
                    if p["scheme"] == "RANGE":
                        self.stat_range_entries += 1
                    else:
                        self.stat_entries += 1
                    self.log(f"ВХОД исполнен {p['side']} {symbol} [{p['scheme']}] по "
                             f"{qstr(filled.get('entry') or p['entry'])}, "
                             f"стоп {qstr(p['stop_price'])}, защита выставлена")
                elif time.time() > p["placed_ts"] + \
                        ENTRY_WAIT * TF_MINUTES.get(self.tf_label, 15) * 60:
                    if p.get("order_id"):
                        self.api.cancel(symbol, p["order_id"])
                    self.log(f"{symbol}: лимит не исполнен за {ENTRY_WAIT} свечей — "
                             f"сигнал пропущен (не догоняем рынок)")
                    del self.positions[symbol]
                continue

            info = open_pos.get(symbol) if open_pos is not None else None

            if info is None:
                if open_pos is None:
                    continue
                try:
                    cur = self.api.price(symbol)
                except Exception:
                    cur = None
                stopped = (cur is not None and p["stop_price"] is not None and
                           ((p["side"] == "LONG" and cur <= p["stop_price"]) or
                            (p["side"] == "SHORT" and cur >= p["stop_price"])))
                loss_close = (cur is not None and
                              ((p["side"] == "LONG" and cur < p["entry"]) or
                               (p["side"] == "SHORT" and cur > p["entry"])))
                if stopped and loss_close:
                    self.losses_today += 1
                    self.losses_by_pair[symbol] = self.losses_by_pair.get(symbol, 0) + 1
                    self.persist_losses()
                    self.log(f"СТОП по {symbol}. Стопов за день: {self.losses_today}")
                    self.close_stats(symbol, p, "СТОП", cur)
                elif stopped:
                    cat = "ТРЕЙЛ" if p.get("tail") else "БЕЗУБЫТОК"
                    self.log(f"{symbol}: закрытие по перенесённому стопу — "
                             f"безубыток/профит, в счёт стопов не идёт")
                    self.close_stats(symbol, p, cat, cur)
                elif cur is not None and (
                        (p["side"] == "LONG" and cur >= p["tp_levels"][-1]) or
                        (p["side"] == "SHORT" and cur <= p["tp_levels"][-1])):
                    self.log(f"Полный тейк по {symbol}: все {n_tps} тейка отработаны")
                    self.close_stats(symbol, p, "ТЕЙК-ПОЛНЫЙ", cur)
                else:
                    self.log(f"{symbol}: позиция закрыта на бирже")
                    self.close_stats(symbol, p, "ПРОЧЕЕ", cur)
                self.cleanup_orders(symbol)
                del self.positions[symbol]
                continue

            closed_frac = (p["qty_total"] - info["qty"]) / p["qty_total"]
            filled_now = 0
            cum = 0.0
            for w in splits:
                cum += w
                if closed_frac + 0.06 >= cum:
                    filled_now += 1
            filled_now = max(0, min(n_tps, filled_now))
            newly = filled_now - sum(p["tp_filled"])
            if newly > 0:
                for i in range(sum(p["tp_filled"]), filled_now):
                    p["tp_filled"][i] = True
                    self.log(f"ТЕЙК {i + 1}/{n_tps} по {symbol} достигнут "
                             f"({qstr(p['tp_levels'][i])})")
                    if i == 0:
                        self.move_stop(symbol, p, p["entry"], info["qty"])
                if filled_now >= n_tps:
                    if p["scheme"] == "SFP":
                        # все тейки тренда отработали — остаётся хвост 25%,
                        # стоп трейлится за EMA20 рабочего ТФ
                        p["tail"] = True
                        self.log(f"{symbol}: тейки тренда отработаны, "
                                 f"хвост 25% трейлится за EMA{TREND_TRAIL_EMA}")
                    else:
                        self.log(f"{symbol}: все {n_tps} тейка отработали, "
                                 f"позиция закрыта")
                        self.close_stats(symbol, p, "ТЕЙК-ПОЛНЫЙ")
                        self.cleanup_orders(symbol)
                        del self.positions[symbol]
                        continue
            p["qty"] = info["qty"]

            # трейлинг хвоста тренда за EMA20
            if p.get("tail") and info["qty"] > 0:
                e = self.get_ema20(symbol)
                if e is not None:
                    if p["side"] == "LONG":
                        new_stop = max(p["stop_price"], e)
                    else:
                        new_stop = min(p["stop_price"], e)
                    if new_stop != p["stop_price"]:
                        self.move_stop(symbol, p, new_stop, info["qty"])

            # страховочное закрытие, если биржевого стопа нет
            if not p.get("stop_id"):
                try:
                    cur = self.api.price(symbol)
                except Exception:
                    cur = None
                if cur is not None and p["stop_price"] is not None and (
                        (p["side"] == "LONG" and cur <= p["stop_price"]) or
                        (p["side"] == "SHORT" and cur >= p["stop_price"])):
                    try:
                        self.api.order(symbol,
                                       "Sell" if p["side"] == "LONG" else "Buy",
                                       p["qty"], reduce_only=True)
                        loss_close = ((p["side"] == "LONG" and cur < p["entry"]) or
                                      (p["side"] == "SHORT" and cur > p["entry"]))
                        if loss_close:
                            self.losses_today += 1
                            self.losses_by_pair[symbol] = self.losses_by_pair.get(symbol, 0) + 1
                            self.persist_losses()
                            self.log(f"СТОП по {symbol} (резервное закрытие). "
                                     f"Стопов за день: {self.losses_today}")
                            self.close_stats(symbol, p, "СТОП", cur)
                        else:
                            cat = "ТРЕЙЛ" if p.get("tail") else "БЕЗУБЫТОК"
                            self.log(f"{symbol}: резервное закрытие — "
                                     f"безубыток/профит, в счёт стопов не идёт")
                            self.close_stats(symbol, p, cat, cur)
                        self.cleanup_orders(symbol)
                        del self.positions[symbol]
                    except Exception as ex:
                        self.log(f"{symbol}: не удалось закрыть — {ex}")

    def _manage_dry(self):
        """DRY-RUN: локальное сопровождение по текущей цене. Стоп проверяется
        первым; после всех тейков тренда хвост трейлится за EMA20."""
        for symbol in list(self.positions):
            p = self.positions[symbol]
            splits = p["splits"]
            n_tps = len(splits)
            try:
                cur = self.api.price(symbol)
            except Exception as ex:
                self.log(f"{symbol}: цена не получена ({ex}) — пропуск цикла")
                continue

            if p.get("phase") == "WAIT_FILL":
                hit = (cur <= p["entry"]) if p["side"] == "LONG" else (cur >= p["entry"])
                if hit:
                    p["phase"] = "ACTIVE"
                    if p["scheme"] == "RANGE":
                        self.stat_range_entries += 1
                    else:
                        self.stat_entries += 1
                    self.log(f"DRY-RUN: ВХОД исполнен {p['side']} {symbol} "
                             f"[{p['scheme']}] по {qstr(p['entry'])}, "
                             f"стоп {qstr(p['stop_price'])}")
                elif time.time() > p["placed_ts"] + \
                        ENTRY_WAIT * TF_MINUTES.get(self.tf_label, 15) * 60:
                    self.log(f"{symbol}: DRY-RUN лимит не исполнен за {ENTRY_WAIT} "
                             f"свечей — сигнал пропущен")
                    del self.positions[symbol]
                continue

            # трейлинг хвоста тренда за EMA20 (обновление стопа до проверки)
            if p.get("tail"):
                e = self.get_ema20(symbol)
                if e is not None:
                    if p["side"] == "LONG":
                        new_stop = max(p["stop_price"], e)
                    else:
                        new_stop = min(p["stop_price"], e)
                    if new_stop != p["stop_price"]:
                        self.log(f"DRY-RUN: трейлинг {symbol} — стоп на "
                                 f"EMA{TREND_TRAIL_EMA} {qstr(new_stop)}")
                        p["stop_price"] = new_stop

            # 1) стоп
            if ((p["side"] == "LONG" and cur <= p["stop_price"]) or
                    (p["side"] == "SHORT" and cur >= p["stop_price"])):
                loss_close = ((p["side"] == "LONG" and p["stop_price"] < p["entry"]) or
                              (p["side"] == "SHORT" and p["stop_price"] > p["entry"]))
                if loss_close:
                    self.losses_today += 1
                    self.losses_by_pair[symbol] = self.losses_by_pair.get(symbol, 0) + 1
                    self.persist_losses()
                    self.log(f"DRY-RUN: СТОП по {symbol}. "
                             f"Стопов за день: {self.losses_today}")
                    self.close_stats(symbol, p, "СТОП", p["stop_price"])
                elif p.get("tail"):
                    self.log(f"DRY-RUN: {symbol} закрыт трейлингом EMA — профит")
                    self.close_stats(symbol, p, "ТРЕЙЛ", p["stop_price"])
                else:
                    self.log(f"DRY-RUN: {symbol} закрыт по перенесённому стопу — "
                             f"безубыток/профит, в счёт стопов не идёт")
                    self.close_stats(symbol, p, "БЕЗУБЫТОК", p["stop_price"])
                del self.positions[symbol]
                continue

            # 2) тейки
            for i in range(n_tps):
                if p["tp_filled"][i]:
                    continue
                hit = ((p["side"] == "LONG" and cur >= p["tp_levels"][i]) or
                       (p["side"] == "SHORT" and cur <= p["tp_levels"][i]))
                if not hit:
                    break
                p["tp_filled"][i] = True
                self.log(f"DRY-RUN: ТЕЙК {i + 1}/{n_tps} по {symbol} достигнут "
                         f"({qstr(p['tp_levels'][i])})")
                if i == 0:
                    p["stop_price"] = p["entry"]
                    self.log(f"{symbol}: стоп перенесён на безубыток "
                             f"{qstr(p['entry'])}")
            if sum(p["tp_filled"]) >= n_tps:
                if p["scheme"] == "SFP":
                    p["tail"] = True
                    self.log(f"DRY-RUN: {symbol} тейки тренда отработаны, "
                             f"хвост 25% трейлится за EMA{TREND_TRAIL_EMA}")
                else:
                    self.log(f"DRY-RUN: {symbol} все {n_tps} тейка отработали, "
                             f"позиция закрыта")
                    self.close_stats(symbol, p, "ТЕЙК-ПОЛНЫЙ", p["tp_levels"][-1])
                    del self.positions[symbol]


def tg_send(cfg, text):
    if not cfg.get("token") or not cfg.get("chat"):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{cfg['token']}/sendMessage",
            json={"chat_id": cfg["chat"], "text": text},
            timeout=10,
        )
    except Exception:
        pass


def main(page: ft.Page):
    page.title = "SFP-бот v4"
    page.padding = 48

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

    ex = ft.Dropdown(label="Биржа",
                     options=[ft.dropdown.Option("Bybit"),
                              ft.dropdown.Option("BingX")],
                     value="BingX")
    mode = ft.Dropdown(label="Режим",
                       options=[ft.dropdown.Option("Демо"),
                                ft.dropdown.Option("Реальный")],
                       value="Демо")
    tf = ft.Dropdown(label="Таймфрейм",
                     options=[ft.dropdown.Option(k) for k in TIMEFRAMES],
                     value="15 минут")
    trend_tf = ft.Dropdown(label="Тренд-ТФ (EMA)",
                           options=[ft.dropdown.Option(EMA_OFF)] +
                                   [ft.dropdown.Option(k) for k in TIMEFRAMES],
                           value="1 час")
    api_key = ft.TextField(label="API-ключ", password=True, can_reveal_password=True)
    secret = ft.TextField(label="Секрет", password=True, can_reveal_password=True)
    symbols = ft.TextField(
        label="Пары через запятую",
        value="BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,BNBUSDT,ZECUSDT,HYPEUSDT,XPLUSDT,"
              "UNIUSDT,NEARUSDT,DOGEUSDT,ADAUSDT,LTCUSDT,LINKUSDT,AVAXUSDT,TRXUSDT,"
              "DOTUSDT,SUIUSDT,APTUSDT,ARBUSDT,OPUSDT,JUPUSDT,FILUSDT,ATOMUSDT,"
              "XLMUSDT,HBARUSDT,INJUSDT,PEPEUSDT,WIFUSDT,TIAUSDT,SEIUSDT,SUSDT,"
              "ICPUSDT,ETCUSDT,BCHUSDT,POLUSDT,ALGOUSDT,VETUSDT,AAVEUSDT,CRVUSDT,"
              "LDOUSDT,GRTUSDT,FETUSDT,RENDERUSDT,TAOUSDT,ASTERUSDT,NIGHTUSDT,ENAUSDT")
    deposit = ft.TextField(label="Риск на сделку (0.015 = 1.5% компаунд от баланса)",
                           value="0.015")
    losses = ft.TextField(label="Макс. стопов на корзину в день (на пару: 3)",
                          value="5")
    maxpos = ft.TextField(label="Макс. позиций одновременно", value="2")
    ema_prox = ft.TextField(label="Вход не дальше N% от EMA (0 = только сторона тренда)",
                            value="3.0")
    tg_token = ft.TextField(label="Токен Telegram-бота", password=True, can_reveal_password=True)
    tg_chat = ft.TextField(label="Ваш chat_id в Telegram")

    profile_name = ft.TextField(label="Имя профиля", hint_text="например: BingX-демо-15m")
    profile_dd = ft.Dropdown(
        label="Сохранённые профили",
        options=[ft.dropdown.Option(k) for k in sorted(profiles)],
        value=sorted(profiles)[0] if profiles else None)

    status = ft.Text("Остановлен", size=18, weight=ft.FontWeight.BOLD)
    moscow_note = ft.Text(MOSCOW_HINT, size=14, weight=ft.FontWeight.BOLD)
    log_view = ft.ListView(spacing=5, auto_scroll=True, expand=True)
    cfg = {"token": "", "chat": ""}

    warning_banner = ft.Container(
        content=ft.Text(WARNING_ISOLATED, color=ft.Colors.BLACK,
                        weight=ft.FontWeight.BOLD),
        bgcolor=ft.Colors.GREEN_100, padding=10, border_radius=6,
        margin=ft.Margin(left=0, top=0, right=0, bottom=4),
    )

    btn_start = ft.FilledButton("Запустить", on_click=None, expand=True)
    btn_stop = ft.FilledButton("Остановить", on_click=None, expand=True,
                               disabled=True)

    def add_log(msg):
        log_view.controls.append(
            ft.Text(f"{dt.datetime.now():%H:%M:%S}  {msg}"))
        tg_send(cfg, msg)
        page.update()

    def on_clear_log(e):
        log_view.controls.clear()
        log_view.controls.append(
            ft.Text(f"{dt.datetime.now():%H:%M:%S}  Журнал очищен (бот продолжает работать)"))
        page.update()

    FIELDS = {"ex": ex, "mode": mode, "tf": tf, "trend_tf": trend_tf,
              "api_key": api_key, "secret": secret, "symbols": symbols,
              "deposit": deposit, "losses": losses, "maxpos": maxpos,
              "ema_prox": ema_prox,
              "tg_token": tg_token, "tg_chat": tg_chat}

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
        save_profiles(profiles)
        d = state_load()
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
        save_profiles(profiles)
        refresh_dd()
        add_log(f"Профиль «{name}» удалён")

    profile_dd.on_change = on_load

    state = {"bot": None, "thread": None, "active": False}

    def set_buttons(running):
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
                add_log(f"Тренд-ТФ должен быть крупнее рабочего ТФ "
                        f"(рабочий: {tf.value})")
                return
        try:
            prox = float(ema_prox.value)
        except ValueError:
            add_log("Проверьте число в поле «Вход не дальше N% от EMA»")
            return
        try:
            risk = float(deposit.value)
            if not (0 < risk <= 0.05):
                raise ValueError
        except ValueError:
            add_log("Риск на сделку — доля баланса от 0 до 0.05 (например 0.015)")
            return
        demo = mode.value == "Демо"
        if ex.value == "Bybit":
            api = BybitAPI(api_key.value.strip(), secret.value.strip(),
                           demo, tf_cfg["interval_bybit"])
        else:
            api = BingXAPI(api_key.value.strip(), secret.value.strip(),
                           demo, tf_cfg["interval_bingx"])
        bot = Bot(api, tf.value, trend_tf.value, pairs,
                  risk,
                  int(losses.value), int(maxpos.value), add_log,
                  ema_proximity_pct=prox,
                  load_state=load_state, save_state=save_state)
        cfg["token"] = tg_token.value.strip()
        cfg["chat"] = tg_chat.value.strip()
        state["bot"] = bot
        state["active"] = True
        state["thread"] = threading.Thread(target=bot.run, daemon=True)
        state["thread"].start()
        add_log(WARNING_ISOLATED)
        status.value = f"Работает: {ex.value} / {mode.value} / {tf.value}"
        set_buttons(True)

    def on_stop(e):
        if state["bot"]:
            state["bot"].stop_event.set()
        state["active"] = False
        status.value = "Остановлен"
        set_buttons(False)

    btn_start.on_click = on_start
    btn_stop.on_click = on_stop

    settings_tab = ft.Column([
        profile_dd,
        profile_name,
        ft.FilledButton("Сохранить настройки", on_click=on_save, expand=True),
        ft.Row([
            ft.FilledButton("Загрузить", on_click=on_load, expand=True),
            ft.OutlinedButton("Удалить", on_click=on_delete, expand=True),
        ]),
        ft.Divider(),
        ex, mode, tf, trend_tf,
        api_key, secret,
        symbols,
        ft.Row([
            ft.Container(deposit, expand=1),
            ft.Container(losses, expand=1),
        ]),
        ft.Container(maxpos, expand=1),
        ema_prox,
        tg_token, tg_chat,
        moscow_note,
        ft.Row([btn_start, btn_stop]),
        status,
    ], spacing=12, scroll=ft.ScrollMode.AUTO, expand=True)

    log_tab = ft.Column([
        ft.Row([ft.OutlinedButton("Очистить журнал", on_click=on_clear_log)]),
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

    page.add(ft.Column([
        warning_banner,
        ft.Row([btn_tab_settings, btn_tab_log]),
        content,
    ], spacing=10, expand=True))

    try:
        last = state_load().get("last_profile")
        if last and last in profiles:
            apply(profiles[last])
            refresh_dd(last)
    except Exception:
        pass


ft.run(main)
