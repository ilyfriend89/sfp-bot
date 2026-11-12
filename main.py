# -*- coding: utf-8 -*-
# ======================================================================
#  ВНИМАНИЕ: ТОРГОВАТЬ ТОЛЬКО ИЗОЛИРОВАННОЙ МАРЖОЙ (ISOLATED) С ПЛЕЧОМ 5x
#  Размер позиции бот считает из риска (1% на сделку) — плечо в приложении
#  биржи задаёт только объём залога под позицию.
# ======================================================================

import time, hmac, hashlib, urllib.parse, threading, json, os
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

# каталог данных приложения: переживает перезапуск и обновление APK
STATE_FILE = os.path.join(os.getenv("FLET_APP_STORAGE_DATA") or os.getcwd(),
                          "sfp_state.json")

MOSCOW_HINT = ("⏰ По Москве: килл-зоны 10:00–13:00 и 16:00–22:00. "
               "Бот включайте с 9:30 до 22:00 по МСК, в остальное время сигналов не будет.")

TIMEFRAMES = {
    "1 минута": {"interval_bybit": "1",   "interval_bingx": "1m",  "vol_mult": 3.0},
    "5 минут":  {"interval_bybit": "5",   "interval_bingx": "5m",  "vol_mult": 2.8},
    "15 минут": {"interval_bybit": "15",  "interval_bingx": "15m", "vol_mult": 1.5},
    "30 минут": {"interval_bybit": "30",  "interval_bingx": "30m", "vol_mult": 2.2},
    "1 час":    {"interval_bybit": "60",  "interval_bingx": "1h",  "vol_mult": 2.0},
    "4 часа":   {"interval_bybit": "240", "interval_bingx": "4h",  "vol_mult": 1.5},
    "1 день":   {"interval_bybit": "D",   "interval_bingx": "1d",  "vol_mult": 1.2},
}

# длительность свечи в минутах — для расчёта ожидания лимит-входа
TF_MINUTES = {"1 минута": 1, "5 минут": 5, "15 минут": 15, "30 минут": 30,
              "1 час": 60, "4 часа": 240, "1 день": 1440}

# порядок ТФ по возрастанию — для проверки «тренд крупнее рабочего»
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

EMA_PERIOD = 50      # период EMA для фильтра тренда (конфиг: EMA50 на 1H)
EMA_OFF = "Выключен" # значение тренд-ТФ, отключающее фильтр
TREND_CACHE_TTL = 60 # кэш EMA по парам, секунд

# сопровождение позиций: опрос каждые N секунд; сигналы — раз в минуту
FAST_PERIOD = 5      # проверка позиций каждые 5 секунд

# ---- зафиксированная конфигурация (результат бэктестов v3.1/v4) ----
# DRY-RUN: бот считает сигналы, входы, тейки и переносы стопа и пишет
# всё в журнал, но ордера на биржу не отправляются.
# Для реальной торговли поставьте False.
DRY_RUN = True
DRY_RUN_BALANCE = 1000.0  # расчётный баланс для DRY-RUN (риск = 1% от него)

# SFP-тренд: уровни тейков 0.63 / 1.27 / 1.9 R, доли объёма 1/2, 1/4, 1/4;
# вклад каждого тейка в итог полной сделки (в R):
TP_OFFSETS = (1.9 / 3, 2 * 1.9 / 3, 1.9)                      # 0.633, 1.267, 1.9
TP_SPLIT = (0.5, 0.25, 0.25)                                   # доли позиции
TP_R_CONTRIB = [o * w for o, w in zip(TP_OFFSETS, TP_SPLIT)]   # 0.317, 0.317, 0.475

# Диапазонный модуль (валидация v3.1): всегда включён, без EMA-фильтра.
# Канал RANGE_DAYS дней, объём сигнальной свечи xRANGE_VOL_MULT среднего,
# стоп за границей канала, тейки 0.5 / 1.0 R долями 1/2, 1/2.
RANGE_DAYS = 3
RANGE_TOUCH_TOL = 0.0015    # допуск касания границы канала, 0.15%
RANGE_VOL_MULT = 1.3
RANGE_STOP_ADD = 0.008      # стоп на 0.8% за границей канала
TP_RANGE_OFFSETS = (0.5, 1.0)
TP_RANGE_SPLIT = (0.5, 0.5)
TP_RANGE_R_CONTRIB = [o * w for o, w in zip(TP_RANGE_OFFSETS, TP_RANGE_SPLIT)]

# SFP-тренд: свинг-окно 20 баров (валидация v3.1)
SFP_SWING = 20

RISK_CAP_LEVERAGE = 10.0   # лимит расчёта бота (в приложении биржи — 5x Isolated)
MIN_STOP_PCT = 0.8         # стоп ближе 0.8% — сигнал пропускаем («пыль»)
MAX_STOPS_PER_PAIR = 3     # стопов на одну пару в день
ENTRY_WAIT = 3             # свечей на исполнение лимит-входа (иначе отмена)


def state_load():
    """Читает всё хранилище (профили, последний профиль, счётчик стопов)."""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def state_save(data):
    """Записывает всё хранилище целиком."""
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def round_step(value, step):
    """Округление вниз до кратности шагу, без float-артефактов."""
    if not step or step <= 0:
        return value
    s = Decimal(str(step))
    n = (Decimal(str(value)) / s).to_integral_value(rounding=ROUND_DOWN)
    return float(n * s)


def qstr(x):
    """Число строкой без хвостовых нулей и артефактов float."""
    s = f"{float(x):.10f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _fmt_frac(w):
    """Доля позиции строкой: 0.5 -> 1/2, 0.25 -> 1/4."""
    return {0.5: "1/2", 0.25: "1/4"}.get(w, f"{w:.2f}")


def in_kill_zone():
    """Килл-зоны по Москве 10:00–13:00 и 16:00–22:00 = UTC 7–10 и 13–19."""
    now = dt.datetime.utcnow()
    return (7 <= now.hour < 10) or (13 <= now.hour < 19)


def calc_tps(entry, stop, side, offsets):
    """Тейк-уровни по смещениям в R. Схема задаётся списком offsets:
    SFP-тренд — (0.633, 1.267, 1.9), диапазон — (0.5, 1.0)."""
    r = abs(entry - stop)
    if r <= 0:
        r = entry * 0.01
    if side == "LONG":
        return [entry + o * r for o in offsets]
    return [entry - o * r for o in offsets]
def ema(values, period):
    """EMA по списку закрытий. Возвращает последнее значение или None."""
    if len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    e = sum(values[:period]) / period  # старт: SMA первых period значений
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


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
        """Правила инструментов: минимум объёма, шаг объёма, шаг цены,
        максимум рыночного ордера. При сбое запроса кэш не заполняется —
        следующая попытка сделает запрос заново."""
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
        """Правила инструмента. Если полного списка нет — точечный запрос
        по конкретному символу с догрузкой в кэш."""
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
        """ID всех открытых reduce-only ордеров (стопы и тейки)."""
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
            # Sell-стоп (закрытие LONG) срабатывает при падении — "2",
            # Buy-стоп (закрытие SHORT) — при росте — "1"
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
        """quantityPrecision — знаки после запятой, шаг = 10^-precision.
        Плюс шаг цены и минимум в деньгах tradeMinUSDT."""
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
                    elif o.get("type") == "LIMIT" and o.get("price"):
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
    def __init__(self, api, tf_label, trend_label, symbols, deposit_pct,
                 max_losses_per_day, max_positions, log,
                 ema_proximity_pct=0.0,
                 load_state=None, save_state=None):
        self.api = api
        self.tf_label = tf_label
        self.vol_mult = TIMEFRAMES[tf_label]["vol_mult"]  # для SFP-ветки
        # deposit_pct — ДОЛЯ РИСКА на сделку (0.01 = 1% баланса в стопе),
        # объём позиции считается от расстояния до стопа, не от депозита
        self.deposit_pct = deposit_pct
        self.trend_label = trend_label
        self.trend_enabled = (trend_label != EMA_OFF)
        self.ema_proximity_pct = ema_proximity_pct
        if self.trend_enabled:
            if api.base in (BYBIT_REAL, BYBIT_DEMO):
                self.trend_interval = TIMEFRAMES[trend_label]["interval_bybit"]
            else:
                self.trend_interval = TIMEFRAMES[trend_label]["interval_bingx"]
        else:
            self.trend_interval = None
        self.trend_cache = {}   # symbol -> (timestamp, ema_value)
        # канал диапазонного модуля: RANGE_DAYS дней на рабочем ТФ
        self.range_bars = max(30, RANGE_DAYS * 1440 // TF_MINUTES.get(tf_label, 15))
        self.symbols = symbols
        self.max_losses = max_losses_per_day   # на корзину в день
        self.max_positions = max_positions
        self.log = log
        self._load_state = load_state
        self._save_state = save_state
        self.stop_event = threading.Event()
        self.day = dt.date.today()
        self.losses_today = 0
        self.losses_by_pair = {}   # symbol -> стопов за сегодня (лимит 3/пара)
        self.positions = {}
        # дневная статистика сигналов (не переживает перезапуск)
        self.stat_signals = 0        # SFP-сигналы, прошедшие объём и килл-зону
        self.stat_skipped = 0        # из них отсечено EMA-фильтром
        self.stat_entries = 0        # реальных входов SFP (исполненный лимит)
        self.stat_range_signals = 0  # диапазонные сигналы
        self.stat_range_entries = 0  # диапазонные входы
        self.last_stat_hour = None
        # статистика исходов сделок за день
        self.stat_out = {"СТОП": 0, "БЕЗУБЫТОК": 0,
                         "ТЕЙК-ПОЛНЫЙ": 0, "ПРОЧЕЕ": 0}
        self.stat_r = 0.0       # суммарный результат за день в R
        self.stat_r_usdt = 0.0  # суммарный результат за день в USDT

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
        """Раз в час пишет сводку по сигналам и сделкам за текущее число."""
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
                 f"полный тейк — {o['ТЕЙК-ПОЛНЫЙ']}, прочее — {o['ПРОЧЕЕ']}. "
                 f"Суммарно за день: {self.stat_r:+.2f}R "
                 f"({self.stat_r_usdt:+.2f} USDT)")

    def close_stats(self, symbol, p, category, cur=None):
        """Учитывает закрытую сделку в дневной статистике: категория, R и USDT.
        R = вклад исполненных тейков (по долям схемы) + результат закрытия
        остатка. R в деньгах = расстояние до стопа * полный объём позиции."""
        if p.get("scheme") == "RANGE":
            splits, contrib = TP_RANGE_SPLIT, TP_RANGE_R_CONTRIB
        else:
            splits, contrib = TP_SPLIT, TP_R_CONTRIB
        n_tps = len(splits)
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

        self.stat_out[category] = self.stat_out.get(category, 0) + 1
        self.stat_r += r_total
        self.stat_r_usdt += usdt_total
        self.log(f"Итог сделки {symbol} [{p.get('scheme', 'SFP')}]: {category}, "
                 f"результат {r_total:+.2f}R ({usdt_total:+.2f} USDT) "
                 f"(тейков исполнено: {filled}/{n_tps}), "
                 f"за день {self.stat_r:+.2f}R ({self.stat_r_usdt:+.2f} USDT)")

    def reset_daily_stats(self):
        """Сброс дневных счётчиков: лимит стопов, сигналы, исходы сделок."""
        self.day = dt.date.today()
        self.losses_today = 0
        self.losses_by_pair = {}
        self.stat_signals = 0
        self.stat_skipped = 0
        self.stat_entries = 0
        self.stat_range_signals = 0
        self.stat_range_entries = 0
        self.last_stat_hour = None
        self.stat_out = {"СТОП": 0, "БЕЗУБЫТОК": 0,
                         "ТЕЙК-ПОЛНЫЙ": 0, "ПРОЧЕЕ": 0}
        self.stat_r = 0.0
        self.stat_r_usdt = 0.0
        self.persist_losses()

    def cleanup_orders(self, symbol):
        """Снимает все оставшиеся reduce-only ордера по паре
        (стопы и неисполненные тейки) после закрытия позиции."""
        try:
            oids = self.api.open_reduce_orders(symbol)
        except Exception as ex:
            self.log(f"{symbol}: не удалось получить ордера для снятия — {ex}")
            return
        for oid in oids:
            if self.api.cancel(symbol, oid):
                self.log(f"{symbol}: снят оставшийся ордер {oid}")

    def place_stop(self, symbol, p, qty, stop_price):
        """Ставит биржевой стоп-ордер, возвращает его ID или None.
        В DRY-RUN ордер не отправляется — сопровождение локальное."""
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
        """Отменяет старый стоп и ставит новый на оставшийся объём."""
        if DRY_RUN:
            p["stop_price"] = new_stop
            self.log(f"{symbol}: DRY-RUN стоп перенесён на {qstr(new_stop)}")
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
        """Лимитные тейк-ордера по долям схемы (SFP: 1/2, 1/4, 1/4;
        диапазон: 1/2, 1/2; последний — остаток)."""
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
            tps = [self.api.price_ok(sym, t) for t in
                   calc_tps(entry, stop, side, TP_OFFSETS)]
            self.positions[sym] = {
                "phase": "ACTIVE",
                "scheme": "SFP", "splits": TP_SPLIT,
                "side": side, "entry": entry, "stop_price": stop,
                "risk": abs(entry - stop),
                "stop_id": None, "tp_levels": tps,
                "tp_filled": [False] * len(TP_SPLIT),
                "qty_total": qty, "qty": qty,
            }
            self.positions[sym]["stop_id"] = self.place_stop(
                sym, self.positions[sym], qty, stop)
            self.place_tps(sym, self.positions[sym])
            self.log(f"Восстановлена позиция {side} {sym} по {qstr(entry)}, "
                     f"объём {qstr(qty)}: стоп {qstr(stop)}, "
                     f"тейки {' / '.join(qstr(t) for t in tps)}")
        if open_pos:
            self.log(f"Итого восстановлено позиций: {len(open_pos)}")
        self.load_losses()

    def get_ema_trend(self, symbol):
        """EMA50 на тренд-ТФ с кэшем на TREND_CACHE_TTL секунд.
        Возвращает значение или None при ошибке/нехватке свечей."""
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

    def trend_ok(self, symbol, side):
        """Фильтр тренда и «Не догоняем»:
        1) LONG — только выше EMA50, SHORT — только ниже;
        2) если включён ema_proximity_pct — цена не дальше N% от EMA50.
        Если фильтр выключен в настройках, всегда разрешает вход."""
        if not self.trend_enabled:
            return True
        e = self.get_ema_trend(symbol)
        if e is None:
            self.log(f"{symbol}: мало свечей для EMA{EMA_PERIOD} — сигнал пропущен")
            return False
        try:
            cur = self.api.price(symbol)
        except Exception as ex:
            self.log(f"{symbol}: цена не получена ({ex}) — сигнал пропущен")
            return False
        if side == "LONG" and cur <= e:
            return False
        if side == "SHORT" and cur >= e:
            return False
        if self.ema_proximity_pct > 0:
            dist_pct = abs(cur - e) / e * 100.0
            if dist_pct > self.ema_proximity_pct:
                return False
        return True

    def sfp_signal(self, symbol):
        """SFP-тренд: свайп экстремума за SFP_SWING закрытых баров с возвратом
        за него же, объём x(vol_mult) среднего за окно. Стоп — за свайпнутым
        экстремумом. Сигнал только в килл-зонах."""
        c = self.api.candles(symbol, limit=SFP_SWING + 2)
        if len(c) < SFP_SWING + 1:
            return None, None
        window = c[-(SFP_SWING + 1):-1]
        swing_low = min(x[2] for x in window)
        swing_high = max(x[1] for x in window)
        avg_vol = sum(x[4] for x in window) / len(window)
        last = c[-1]
        if not in_kill_zone():
            return None, None
        long_sig = (last[2] < swing_low and
                    last[3] > swing_low and
                    last[4] >= self.vol_mult * avg_vol)
        short_sig = (last[1] > swing_high and
                     last[3] < swing_high and
                     last[4] >= self.vol_mult * avg_vol)
        if long_sig:
            return "LONG", last[2]
        if short_sig:
            return "SHORT", last[1]
        return None, None

    def range_signal(self, symbol):
        """Диапазонный модуль: канал RANGE_DAYS дней, не менее 2 касаний
        границы, реверс от границы с объёмом xRANGE_VOL_MULT среднего.
        Стоп — за границей канала. Сигнал только в килл-зонах."""
        need = self.range_bars + 2
        c = self.api.candles(symbol, limit=need)
        if len(c) < need:
            return None, None
        hist = c[-(self.range_bars + 1):-1]   # закрытые бары канала
        upper = max(x[1] for x in hist)
        lower = min(x[2] for x in hist)
        avg_vol = sum(x[4] for x in hist) / len(hist)
        tol = RANGE_TOUCH_TOL
        touch_up = sum(1 for x in hist if x[1] >= upper * (1 - tol))
        touch_dn = sum(1 for x in hist if x[2] <= lower * (1 + tol))
        last = c[-1]
        if not in_kill_zone():
            return None, None
        long_sig = (touch_dn >= 2 and
                    last[2] <= lower * (1 + tol) and
                    last[3] > lower and
                    last[3] > last[0] and
                    last[4] >= RANGE_VOL_MULT * avg_vol)
        short_sig = (touch_up >= 2 and
                     last[1] >= upper * (1 - tol) and
                     last[3] < upper and
                     last[3] < last[0] and
                     last[4] >= RANGE_VOL_MULT * avg_vol)
        if long_sig:
            return "LONG", lower * (1 - RANGE_STOP_ADD)
        if short_sig:
            return "SHORT", upper * (1 + RANGE_STOP_ADD)
        return None, None

    def try_enter(self, symbol, side, stop, scheme):
        """Общий вход для обеих веток: лимит-ордер по текущей цене,
        расчёт объёма из риска, тейки по схеме. Квоты стопов проверены
        вызывающим кодом до этого вызова."""
        entry = self.api.price_ok(symbol, self.api.price(symbol))
        stop = self.api.price_ok(symbol, stop)
        risk = abs(entry - stop)
        if risk <= 0:
            return
        stop_pct = risk / entry * 100.0
        if stop_pct < MIN_STOP_PCT:
            self.log(f"{symbol}: стоп {stop_pct:.2f}% — ближе минимальных "
                     f"{MIN_STOP_PCT}% — сигнал пропущен")
            return
        bal = DRY_RUN_BALANCE if DRY_RUN else self.api.balance()
        if bal <= 0:
            return
        notional = bal * self.deposit_pct / (risk / entry)
        notional = min(notional, bal * RISK_CAP_LEVERAGE)
        raw_qty = notional / entry
        qty = self.api.qty_ok(symbol, raw_qty, price=entry)
        if qty <= 0:
            self.log(f"{symbol}: объём {raw_qty:.6f} меньше минимума биржи — пропущено")
            return
        offsets = TP_RANGE_OFFSETS if scheme == "RANGE" else TP_OFFSETS
        splits = TP_RANGE_SPLIT if scheme == "RANGE" else TP_SPLIT
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
            "qty_total": qty, "qty": qty,
        }
        tag = "DRY-RUN " if DRY_RUN else ""
        tp_txt = " / ".join(qstr(t) for t in tps)
        split_txt = ", ".join(_fmt_frac(w) for w in splits)
        self.log(f"{tag}ЛИМИТ-ВХОД {side} {symbol} [{scheme}] по {qstr(entry)}, "
                 f"стоп {qstr(stop)} ({stop_pct:.2f}%), "
                 f"тейки: {tp_txt} ({split_txt}) "
                 f"[{len(self.positions)}/{self.max_positions}]")

    def scan_signals(self):
        """Сканирование пар: сначала SFP-тренд, затем диапазонный модуль.
        Одна позиция на пару — ветки не могут открыть встречные позиции.
        Квоты стопов проверяются до входа."""
        if self.losses_today >= self.max_losses:
            return
        if len(self.positions) >= self.max_positions:
            return
        scanned_any_signal = False
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
                side, stop_lvl = self.sfp_signal(symbol)
                if side:
                    self.stat_signals += 1
                    scanned_any_signal = True
                    if not self.trend_ok(symbol, side):
                        self.stat_skipped += 1
                        if self.trend_enabled:
                            self.log(f"{symbol}: SFP {side} против тренда или вдали "
                                     f"от EMA (EMA{EMA_PERIOD} на {self.trend_label}) "
                                     f"— пропущено")
                        side = None
                    else:
                        self.try_enter(symbol, side, stop_lvl, "SFP")
                        continue
                side2, stop2 = self.range_signal(symbol)
                if side2:
                    self.stat_range_signals += 1
                    scanned_any_signal = True
                    self.try_enter(symbol, side2, stop2, "RANGE")
            except Exception as ex:
                self.log(f"{symbol}: ошибка запроса — {ex}")
                continue
        if scanned_any_signal:
            self.log_stats_if_due()

    def run(self):
        if self.trend_enabled:
            trend_info = f"тренд: EMA{EMA_PERIOD} на {self.trend_label}"
            if self.ema_proximity_pct > 0:
                trend_info += (f", вход только в пределах "
                               f"{self.ema_proximity_pct}% от EMA")
            else:
                trend_info += ", фильтр «у EMA» выключен"
        else:
            trend_info = "EMA-фильтр выключен"
        mode_info = "DRY-RUN (ордера НЕ отправляются)" if DRY_RUN else "РЕАЛЬНЫЙ РЕЖИМ"
        self.log(WARNING_ISOLATED)
        self.log(f"Бот запущен ({self.api.base}). {mode_info}. ТФ: {self.tf_label}")
        self.log(f"Диапазонный модуль: канал {RANGE_DAYS} дн. "
                 f"({self.range_bars} бар), касания >=2, объём x{RANGE_VOL_MULT}, "
                 f"стоп за границей, тейки 0.5R/1.0R долями 1/2, 1/2")
        self.log(f"SFP-тренд: свинг {SFP_SWING} бар, объём x{self.vol_mult}, "
                 f"{trend_info}. Тейки 0.63/1.27/1.9R долями 1/2, 1/4, 1/4; "
                 f"стоп после ТП1 — безубыток, после ТП2 — на ТП1")
        self.log(f"Риск {self.deposit_pct * 100:.1f}% на сделку "
                 f"(плечо расчёта до x{RISK_CAP_LEVERAGE:.0f}). "
                 f"Вход лимитом, ожидание {ENTRY_WAIT} свечей. "
                 f"Лимиты стопов: {MAX_STOPS_PER_PAIR}/пара, "
                 f"{self.max_losses}/корзина в день. "
                 f"Пары: {', '.join(self.symbols)}. "
                 f"Лимит позиций: {self.max_positions}. "
                 f"Позиции проверяются каждые {FAST_PERIOD} сек")
        self.log(MOSCOW_HINT)
        if DRY_RUN:
            self.log(f"DRY-RUN: расчётный баланс {DRY_RUN_BALANCE:.0f} USDT, "
                     f"риск {DRY_RUN_BALANCE * self.deposit_pct:.2f} USDT на сделку")
        else:
            try:
                bal = self.api.balance()
                self.log(f"Баланс: {bal:.2f} USDT — API-доступ подтверждён")
            except Exception as ex:
                self.log(f"[API FAIL] Баланс не получен: {ex}")
        self.recover()
        loop_count = 0
        while not self.stop_event.is_set():
            # быстрый контур: сопровождение позиций каждые 5 секунд
            try:
                self.manage_positions()
            except Exception as ex:
                self.log(f"Ошибка сопровождения позиций: {ex}")
            loop_count += 1
            # медленный контур: сканирование сигналов раз в минуту
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
        """Сопровождение открытых позиций каждые FAST_PERIOD секунд:
        тейки, переносы стопов, закрытия. В DRY-RUN всё считается локально
        по текущей цене, в реальном режиме — по данным биржи."""
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

            # ---- ожидание исполнения лимит-входа ----
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
                    continue  # биржа не ответила — не считаем позицию закрытой
                try:
                    cur = self.api.price(symbol)
                except Exception:
                    cur = None
                # настоящая остановка по стопу — только если закрытие хуже входа
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
                    self.log(f"{symbol}: позиция закрыта по перенесённому стопу — "
                             f"безубыток/профит, в счёт стопов не идёт")
                    self.close_stats(symbol, p, "БЕЗУБЫТОК", cur)
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

            # сколько тейков сработало на бирже — по накопленным долям объёма
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
                    elif i == 1 and n_tps == 3:
                        self.move_stop(symbol, p, p["tp_levels"][0], info["qty"])
                if filled_now >= n_tps:
                    self.log(f"{symbol}: все {n_tps} тейка отработали, позиция закрыта")
                    self.close_stats(symbol, p, "ТЕЙК-ПОЛНЫЙ")
                    self.cleanup_orders(symbol)
                    del self.positions[symbol]
                    continue
            p["qty"] = info["qty"]

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
                            self.log(f"{symbol}: резервное закрытие — "
                                     f"безубыток/профит, в счёт стопов не идёт")
                            self.close_stats(symbol, p, "БЕЗУБЫТОК", cur)
                        self.cleanup_orders(symbol)
                        del self.positions[symbol]
                    except Exception as ex:
                        self.log(f"{symbol}: не удалось закрыть — {ex}")

    def _manage_dry(self):
        """DRY-RUN: локальное сопровождение по текущей цене.
        Лимит-вход исполняется при касании цены, тейки — при достижении
        уровня, стоп — при пересечении. Стоп проверяется первым."""
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

            # 1) стоп (консервативно: если стоп и тейк в одном цикле — стоп первым)
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
                elif i == 1 and n_tps == 3:
                    p["stop_price"] = p["tp_levels"][0]
                    self.log(f"{symbol}: стоп перенесён на ТП1 "
                             f"{qstr(p['tp_levels'][0])}")
            if sum(p["tp_filled"]) >= n_tps:
                self.log(f"{symbol}: все {n_tps} тейка отработали, позиция закрыта")
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
    page.title = "SFP-бот"
    # отступ сверху, чтобы статус-бар телефона не перекрывал интерфейс
    page.padding = 48

    # ---- файловое хранилище вместо client_storage ----
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
                     value="Bybit")
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
    deposit = ft.TextField(label="Риск на сделку (0.01 = 1% баланса в стопе)",
                           value="0.01")
    losses = ft.TextField(label="Макс. стопов на корзину в день (на пару: 3)",
                          value="5")
    maxpos = ft.TextField(label="Макс. позиций одновременно", value="2")
    ema_prox = ft.TextField(label="Вход только у EMA (%, 0 = выкл)", value="3.0")
    tg_token = ft.TextField(label="Токен Telegram-бота", password=True, can_reveal_password=True)
    tg_chat = ft.TextField(label="Ваш chat_id в Telegram")

    profile_name = ft.TextField(label="Имя профиля", hint_text="например: Bybit-демо-15m")
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
        margin=ft.margin.only(bottom=4),
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
        # тренд-ТФ должен быть строго крупнее рабочего ТФ
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
            add_log("Проверьте число в поле «Вход только у EMA»")
            return
        if prox > 0 and trend_tf.value == EMA_OFF:
            add_log("Для фильтра «у EMA» нужен тренд-ТФ — выберите его вместо «Выключен»")
            return
        demo = mode.value == "Демо"
        if ex.value == "Bybit":
            api = BybitAPI(api_key.value.strip(), secret.value.strip(),
                           demo, tf_cfg["interval_bybit"])
        else:
            api = BingXAPI(api_key.value.strip(), secret.value.strip(),
                           demo, tf_cfg["interval_bingx"])
        try:
            bot = Bot(api, tf.value, trend_tf.value, pairs,
                      float(deposit.value),
                      int(losses.value), int(maxpos.value), add_log,
                      ema_proximity_pct=prox,
                      load_state=load_state, save_state=save_state)
        except ValueError:
            add_log("Проверьте числа в настройках")
            return
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
        # компактная кнопка: ширина по тексту, слева, не растянута на весь экран
        ft.Row([ft.OutlinedButton("Очистить журнал", on_click=on_clear_log)]),
        log_view,
    ], spacing=10, expand=True)

    # переключение экранов двумя кнопками вместо вкладок — работает на любой версии флета
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

    # восстановление последнего профиля из файла
    try:
        last = state_load().get("last_profile")
        if last and last in profiles:
            apply(profiles[last])
            refresh_dd(last)
    except Exception:
        pass


ft.run(main)

