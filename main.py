import time, hmac, hashlib, urllib.parse, threading, json
import datetime as dt
from decimal import Decimal, ROUND_DOWN
import requests
import flet as ft

BYBIT_REAL = "https://api.bybit.com"
BYBIT_DEMO = "https://api-demo.bybit.com"
BINGX_REAL = "https://open-api.bingx.com"
BINGX_DEMO = "https://open-api-vst.bingx.com"

STORAGE_KEY = "sfp_profiles"
LAST_KEY = "sfp_last_profile"
LOSSES_KEY = "sfp_losses_state"

MOSCOW_HINT = ("⏰ По Москве: кил-зоны 10:00–13:00 и 16:00–19:00. "
               "Бот включайте с 9:30 до 19:00 по МСК, в остальное время сигналов не будет.")

TIMEFRAMES = {
    "4 часа":   {"interval_bybit": "240", "interval_bingx": "4h",  "vol_mult": 1.5},
    "1 час":    {"interval_bybit": "60",  "interval_bingx": "1h",  "vol_mult": 2.0},
    "30 минут": {"interval_bybit": "30",  "interval_bingx": "30m", "vol_mult": 2.2},
    "15 минут": {"interval_bybit": "15",  "interval_bingx": "15m", "vol_mult": 2.5},
}

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
    "PUMPUSDT":  "PUMPUSDT",
    "WLFIUSDT":  "WLFIUSDT",
    "WIFUSDT":   "WIFUSDT",
}


def round_step(value, step):
    """Округление вниз до шага без float-артефактов."""
    if not step or step <= 0:
        return value
    s = Decimal(str(step))
    text = str(s)
    dec = len(text.split(".")[-1].rstrip("0")) if "." in text else 0
    q = (Decimal(str(value)) / s).quantize(Decimal(1).scaleb(-dec),
                                           rounding=ROUND_DOWN)
    return float(q)


def qstr(x):
    """Число строкой без хвостовых нулей и артефактов float."""
    s = f"{float(x):.10f}".rstrip("0").rstrip(".")
    return s if s else "0"


def calc_3tp(entry, stop, side):
    """Три тейка на равном расстоянии. Полная цель — 1.9R вместо прежних 2R.
    Возвращает [tp1, tp2, tp3]."""
    r = abs(entry - stop)
    if r <= 0:
        r = entry * 0.01
    step = 1.9 * r / 3.0
    if side == "LONG":
        return [entry + step, entry + 2 * step, entry + 3 * step]
    return [entry - step, entry - 2 * step, entry - 3 * step]


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
        """Правила инструментов: минимум объёма, шаг объёма, шаг цены."""
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
                }
            self._lots = lots
        except Exception:
            self._lots = {}
        return self._lots

    def qty_ok(self, symbol, qty, price=None):
        info = self._load_lots().get(self.sym(symbol))
        if not info:
            return round(qty, 3)
        qty = round_step(qty, info["qstep"])
        return qty if qty >= info["min"] else 0.0

    def price_ok(self, symbol, price):
        info = self._load_lots().get(self.sym(symbol))
        if not info or not info.get("tick"):
            return round(price, 6)
        return round_step(price, info["tick"])

    def candles(self, symbol, limit=30):
        d = self.req("GET", "/v5/market/kline",
                     {"category": "linear", "symbol": self.sym(symbol),
                      "interval": self.interval, "limit": limit})
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
        r = requests.post(url, timeout=15) if method == "POST" \
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
            self._lots = {}
        return self._lots

    def qty_ok(self, symbol, qty, price=None):
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
        if not info or not info.get("tick"):
            return round(price, 6)
        return round_step(price, info["tick"])

    def candles(self, symbol, limit=30):
        d = self.req("GET", "/openApi/swap/v2/quote/kline",
                     {"symbol": self.fmt(symbol), "interval": self.interval,
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
    def __init__(self, api, tf_label, symbols, deposit_pct,
                 max_losses_per_day, max_positions, log,
                 load_state=None, save_state=None):
        self.api = api
        self.tf_label = tf_label
        self.vol_mult = TIMEFRAMES[tf_label]["vol_mult"]
        self.symbols = symbols
        self.deposit_pct = deposit_pct
        self.max_losses = max_losses_per_day
        self.max_positions = max_positions
        self.log = log
        self._load_state = load_state
        self._save_state = save_state
        self.stop_event = threading.Event()
        self.total_rr = 1.9  # полная цель по тейкам: чуть меньше прежних 2R
        self.day = dt.date.today()
        self.losses_today = 0
        self.positions = {}

    def load_losses(self):
        if not self._load_state:
            return
        try:
            st = self._load_state()
            if st and st.get("date") == dt.date.today().isoformat():
                self.losses_today = int(st.get("losses", 0))
                if self.losses_today:
                    self.log(f"Восстановлен счётчик стопов за сегодня: {self.losses_today}")
        except Exception:
            pass

    def persist_losses(self):
        if not self._save_state:
            return
        try:
            self._save_state({"date": dt.date.today().isoformat(),
                              "losses": self.losses_today})
        except Exception:
            pass

    def place_stop(self, symbol, p, qty, stop_price):
        """Ставит биржевой стоп-ордер, возвращает его ID или None."""
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

    def recover(self):
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
            # снимаем старые стоп/тейк-ордера и перевыставляем по новой схеме
            for oid in self.api.open_reduce_orders(sym):
                self.api.cancel(sym, oid)
            if not stop or stop == entry:
                stop = entry - entry * 0.01 if side == "LONG" else entry + entry * 0.01
                self.log(f"{sym}: биржевой стоп не найден — взят расчётный 1%")
            stop = self.api.price_ok(sym, stop)
            tps = [self.api.price_ok(sym, t) for t in calc_3tp(entry, stop, side)]
            q1 = self.api.qty_ok(sym, qty / 3, price=entry)
            q3 = self.api.qty_ok(sym, qty - 2 * q1, price=entry) if q1 > 0 else 0.0
            self.positions[sym] = {
                "side": side, "entry": entry, "stop_price": stop,
                "stop_id": None, "tp_levels": tps,
                "tp_filled": [False, False, False],
                "qty_total": qty, "qty": qty,
            }
            self.positions[sym]["stop_id"] = self.place_stop(sym, self.positions[sym], qty, stop)
            if q1 > 0 and q3 > 0:
                for i, (q, tp) in enumerate(((q1, tps[0]), (q1, tps[1]), (q3, tps[2]))):
                    try:
                        self.api.order(sym, "Sell" if side == "LONG" else "Buy",
                                       q, tp=tp, reduce_only=True)
                    except Exception as ex:
                        self.log(f"{sym}: тейк {i+1} не выставлен — {ex}")
            self.log(f"Восстановлена позиция {side} {sym} по {qstr(entry)}, "
                     f"объём {qstr(qty)}: стоп {qstr(stop)}, "
                     f"тейки {qstr(tps[0])} / {qstr(tps[1])} / {qstr(tps[2])}")
        if open_pos:
            self.log(f"Итого восстановлено позиций: {len(open_pos)}")
        self.load_losses()

    def sfp_signal(self, symbol):
        c = self.api.candles(symbol, limit=30)
        if len(c) < 22:
            return None, None
        prev, last = c[-2], c[-1]
        avg_vol = sum(x[4] for x in c[-21:-1]) / 20

        now = dt.datetime.utcnow()
        kill_zone = (7 <= now.hour < 10) or (13 <= now.hour < 16)

        long_sig = (last[2] < prev[2] and
                    last[3] > prev[2] and
                    last[4] > self.vol_mult * avg_vol and
                    kill_zone)
        short_sig = (last[1] > prev[1] and
                     last[3] < prev[1] and
                     last[4] > self.vol_mult * avg_vol and
                     kill_zone)
        if long_sig:
            return "LONG", last[2]
        if short_sig:
            return "SHORT", last[1]
        return None, None

    def run(self):
        self.log(f"Бот запущен ({self.api.base}). ТФ: {self.tf_label} "
                 f"(фильтр объёма ×{self.vol_mult}). "
                 f"Пары: {', '.join(self.symbols)}. Лимит позиций: {self.max_positions}")
        self.log(MOSCOW_HINT)
        try:
            bal = self.api.balance()
            self.log(f"Баланс: {bal:.2f} USDT — API-доступ подтверждён")
        except Exception as ex:
            self.log(f"[API FAIL] Баланс не получен: {ex}")
        self.recover()
        while not self.stop_event.is_set():
            try:
                self.tick()
                self.log(f"Цикл выполнен. Открытых позиций: {len(self.positions)}")
            except Exception as ex:
                self.log(f"Ошибка: {ex}")
            for _ in range(60):
                if self.stop_event.is_set():
                    break
                time.sleep(1)
        self.log("Бот остановлен")

    def tick(self):
        if self.day != dt.date.today():
            self.day, self.losses_today = dt.date.today(), 0
            self.persist_losses()

        try:
            open_pos = self.api.open_positions()
        except Exception as ex:
            self.log(f"Ошибка получения позиций: {ex}")
            open_pos = None

        for symbol in list(self.positions):
            p = self.positions[symbol]
            info = open_pos.get(symbol) if open_pos is not None else None

            if info is None:
                if open_pos is None:
                    continue  # биржа не ответила — не считаем позицию закрытой
                try:
                    cur = self.api.price(symbol)
                except Exception:
                    cur = None
                if cur is not None and p["stop_price"] is not None and (
                        (p["side"] == "LONG" and cur <= p["stop_price"]) or
                        (p["side"] == "SHORT" and cur >= p["stop_price"])):
                    self.losses_today += 1
                    self.persist_losses()
                    self.log(f"СТОП по {symbol}. Стопов за день: {self.losses_today}")
                elif cur is not None and (
                        (p["side"] == "LONG" and cur >= p["tp_levels"][2]) or
                        (p["side"] == "SHORT" and cur <= p["tp_levels"][2])):
                    self.log(f"Полный тейк по {symbol}: все 3 тейка отработаны")
                else:
                    self.log(f"{symbol}: позиция закрыта на бирже")
                del self.positions[symbol]
                continue

            # сколько тейков сработало на бирже (по уменьшению объёма)
            filled_now = int(round(
                (p["qty_total"] - info["qty"]) / p["qty_total"] * 3))
            filled_now = max(0, min(3, filled_now))
            newly = filled_now - sum(p["tp_filled"])
            if newly > 0:
                for i in range(sum(p["tp_filled"]), filled_now):
                    p["tp_filled"][i] = True
                    self.log(f"ТЕЙК {i + 1}/3 по {symbol} достигнут "
                             f"({qstr(p['tp_levels'][i])})")
                    if i == 0:
                        self.move_stop(symbol, p, p["entry"], info["qty"])
                    elif i == 1:
                        self.move_stop(symbol, p, p["tp_levels"][0], info["qty"])
                if filled_now >= 3:
                    self.log(f"{symbol}: все 3 тейка отработали, позиция закрыта")
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
                        self.losses_today += 1
                        self.persist_losses()
                        self.log(f"СТОП по {symbol} (резервное закрытие). "
                                 f"Стопов за день: {self.losses_today}")
                        del self.positions[symbol]
                    except Exception as ex:
                        self.log(f"{symbol}: не удалось закрыть — {ex}")

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
            try:
                side, stop_lvl = self.sfp_signal(symbol)
            except Exception as ex:
                self.log(f"{symbol}: ошибка запроса — {ex}")
                continue
            if not side:
                continue

            entry = self.api.price(symbol)
            stop = self.api.price_ok(symbol, stop_lvl)
            raw_qty = self.api.balance() * self.deposit_pct / entry
            if raw_qty <= 0:
                continue
            qty = self.api.qty_ok(symbol, raw_qty, price=entry)
            if qty <= 0:
                self.log(f"{symbol}: объём {raw_qty:.6f} меньше минимума биржи — пропущено")
                continue

            tps = [self.api.price_ok(symbol, t) for t in calc_3tp(entry, stop, side)]
            q1 = self.api.qty_ok(symbol, qty / 3, price=entry)
            q3 = self.api.qty_ok(symbol, qty - 2 * q1, price=entry) if q1 > 0 else 0.0

            try:
                self.api.order(symbol, "Buy" if side == "LONG" else "Sell", qty)
            except Exception as ex:
                self.log(f"{symbol}: вход не прошёл — {ex}")
                continue

            self.positions[symbol] = {
                "side": side, "entry": entry, "stop_price": stop,
                "stop_id": None, "tp_levels": tps,
                "tp_filled": [False, False, False],
                "qty_total": qty, "qty": qty,
            }
            self.log(f"ВХОД {side} {symbol} по {qstr(entry)}, стоп {qstr(stop)}, "
                     f"тейки: {qstr(tps[0])} / {qstr(tps[1])} / {qstr(tps[2])} "
                     f"(по 1/3) [{len(self.positions)}/{self.max_positions}]")

            # биржевой стоп на весь объём
            self.positions[symbol]["stop_id"] = \
                self.place_stop(symbol, self.positions[symbol], qty, stop)

            # три лимитных тейк-ордера по 1/3 объёма
            if q1 > 0 and q3 > 0:
                for i, (q, tp) in enumerate(((q1, tps[0]), (q1, tps[1]), (q3, tps[2]))):
                    try:
                        self.api.order(symbol,
                                       "Sell" if side == "LONG" else "Buy",
                                       q, tp=tp, reduce_only=True)
                    except Exception as ex:
                        self.log(f"{symbol}: тейк {i+1} не выставлен — {ex}")
            else:
                self.log(f"{symbol}: треть объёма ниже минимума биржи — "
                         f"тейки не выставлены, сопровождение по стопу")
            break


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
    page.padding = 16

    def load_profiles():
        try:
            return page.client_storage.get(STORAGE_KEY) or {}
        except Exception:
            return {}

    def save_profiles(p):
        try:
            page.client_storage.set(STORAGE_KEY, p)
        except Exception:
            pass

    profiles = load_profiles()

    def load_state():
        try:
            return page.client_storage.get(LOSSES_KEY)
        except Exception:
            return None

    def save_state(data):
        try:
            page.client_storage.set(LOSSES_KEY, data)
        except Exception:
            pass

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
                     value="4 часа")
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
    deposit = ft.TextField(label="Доля депозита на сделку (0.05 = 5%)", value="0.05")
    losses = ft.TextField(label="Макс. стопов в день", value="3")
    maxpos = ft.TextField(label="Макс. позиций одновременно", value="2")
    tg_token = ft.TextField(label="Токен Telegram-бота", password=True, can_reveal_password=True)
    tg_chat = ft.TextField(label="Ваш chat_id в Telegram")

    profile_name = ft.TextField(label="Имя профиля", hint_text="например: Bybit-демо-4h")
    profile_dd = ft.Dropdown(
        label="Сохранённые профили",
        options=[ft.dropdown.Option(k) for k in sorted(profiles)],
        value=sorted(profiles)[0] if profiles else None)

    status = ft.Text("Остановлен", size=18, weight=ft.FontWeight.BOLD)
    moscow_note = ft.Text(MOSCOW_HINT, size=14, weight=ft.FontWeight.BOLD)
    log_view = ft.ListView(spacing=5, auto_scroll=True, expand=True)
    cfg = {"token": "", "chat": ""}

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

    FIELDS = {"ex": ex, "mode": mode, "tf": tf, "api_key": api_key,
              "secret": secret, "symbols": symbols, "deposit": deposit,
              "losses": losses, "maxpos": maxpos,
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
        refresh_dd(name)
        profile_name.value = ""
        add_log(f"Профиль «{name}» сохранён")

    def on_load(e):
        name = profile_dd.value
        if not name or name not in profiles:
            add_log("Выберите профиль из списка")
            return
        apply(profiles[name])
        try:
            page.client_storage.set(LAST_KEY, name)
        except Exception:
            pass
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
        demo = mode.value == "Демо"
        if ex.value == "Bybit":
            api = BybitAPI(api_key.value.strip(), secret.value.strip(),
                           demo, tf_cfg["interval_bybit"])
        else:
            api = BingXAPI(api_key.value.strip(), secret.value.strip(),
                           demo, tf_cfg["interval_bingx"])
        try:
            bot = Bot(api, tf.value, pairs, float(deposit.value),
                      int(losses.value), int(maxpos.value), add_log,
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

    page.add(ft.Column([
        profile_dd,
        profile_name,
        ft.FilledButton("Сохранить настройки", on_click=on_save, expand=True),
        ft.Row([
            ft.FilledButton("Загрузить", on_click=on_load, expand=True),
            ft.OutlinedButton("Удалить", on_click=on_delete, expand=True),
        ]),
        ft.Divider(),
        ex, mode, tf,
        api_key, secret,
        symbols,
        ft.Row([
            ft.Container(deposit, expand=1),
            ft.Container(losses, expand=1),
        ]),
        ft.Container(maxpos, expand=1),
        tg_token, tg_chat,
        moscow_note,
        ft.Row([btn_start, btn_stop]),
        ft.OutlinedButton("Очистить журнал", on_click=on_clear_log, expand=True),
        status,
        ft.Divider(), ft.Text("Журнал:"), log_view,
    ], spacing=12, scroll=ft.ScrollMode.AUTO, expand=True))

    try:
        last = page.client_storage.get(LAST_KEY)
        if last and last in profiles:
            apply(profiles[last])
            refresh_dd(last)
    except Exception:
        pass


ft.run(main)
