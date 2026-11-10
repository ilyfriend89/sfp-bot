import time, hmac, hashlib, urllib.parse, threading, json
import datetime as dt
import requests
import flet as ft

BYBIT_REAL = "https://api.bybit.com"
BYBIT_DEMO = "https://api-testnet.bybit.com"
BINGX_REAL = "https://open-api.bingx.com"
BINGX_DEMO = "https://open-api-vst.bingx.com"

STORAGE_KEY = "sfp_profiles"        # все сохранённые профили
LAST_KEY = "sfp_last_profile"       # последний выбранный профиль


# ---------- API-адаптеры ----------

class BybitAPI:
    def __init__(self, api_key, secret, demo):
        self.api_key, self.secret = api_key, secret
        self.base = BYBIT_DEMO if demo else BYBIT_REAL

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
        d = r.json()
        if d.get("retCode") != 0:
            raise RuntimeError(f"Bybit: {d.get('retMsg')}")
        return d["result"]

    def candles(self, symbol, limit=30):
        d = self.req("GET", "/v5/market/kline",
                     {"category": "linear", "symbol": symbol,
                      "interval": "240", "limit": limit})
        rows = [(float(k[1]), float(k[2]), float(k[3]),
                 float(k[4]), float(k[5])) for k in d["list"]]
        return list(reversed(rows))

    def price(self, symbol):
        d = self.req("GET", "/v5/market/tickers",
                     {"category": "linear", "symbol": symbol})
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

    def order(self, symbol, side, qty, stop=None, reduce_only=False):
        p = {"category": "linear", "symbol": symbol,
             "side": side, "orderType": "Market",
             "qty": str(qty), "positionIdx": 0}
        if stop:
            p["triggerPrice"] = str(stop)
            p["triggerBy"] = "LastPrice"
        if reduce_only:
            p["reduceOnly"] = True
        return self.req("POST", "/v5/order/create", p)


class BingXAPI:
    def __init__(self, api_key, secret, demo):
        self.api_key, self.secret = api_key, secret
        self.base = BINGX_DEMO if demo else BINGX_REAL

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
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError(f"BingX: {d.get('msg')}")
        return d.get("data")

    def candles(self, symbol, limit=30):
        d = self.req("GET", "/openApi/swap/v2/quote/kline",
                     {"symbol": self.fmt(symbol), "interval": "4h", "limit": limit})
        rows = [(float(k["o"]), float(k["h"]), float(k["l"]),
                 float(k["c"]), float(k["v"])) for k in d]
        rows.sort(key=lambda x: x[0])
        return rows

    def fmt(self, symbol):  # BTCUSDT -> BTC-USDT
        for q in ("USDT", "USDC", "BTC"):
            if symbol.endswith(q):
                return symbol[:-len(q)] + "-" + q
        return symbol

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

    def order(self, symbol, side, qty, stop=None, reduce_only=False):
        p = {"symbol": self.fmt(symbol),
             "side": "BUY" if side == "Buy" else "SELL",
             "positionSide": "ONE_WAY",
             "type": "MARKET", "qty": str(qty)}
        if stop:
            p["type"] = "TRIGGER_MARKET"
            p["stopPrice"] = str(stop)
            p["workingType"] = "MARK_PRICE"
        if reduce_only:
            p["reduceOnly"] = True
        return self.req("POST", "/openApi/swap/v2/trade/order", p)


# ---------- торговое ядро ----------

class Bot:
    def __init__(self, api, symbols, deposit_pct,
                 max_losses_per_day, max_positions, log):
        self.api = api
        self.symbols = symbols
        self.deposit_pct = deposit_pct
        self.max_losses = max_losses_per_day
        self.max_positions = max_positions
        self.log = log
        self.stop_event = threading.Event()
        self.rr = 2.0
        self.lev = 3
        self.day = dt.date.today()
        self.losses_today = 0
        self.positions = {}

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
                    last[4] > 1.5 * avg_vol and
                    kill_zone)
        short_sig = (last[1] > prev[1] and
                     last[3] < prev[1] and
                     last[4] > 1.5 * avg_vol and
                     kill_zone)
        if long_sig:
            return "LONG", last[2]
        if short_sig:
            return "SHORT", last[1]
        return None, None

    def run(self):
        self.log(f"Бот запущен ({self.api.base}). Пары: {', '.join(self.symbols)}. "
                 f"Лимит позиций: {self.max_positions}")
        while not self.stop_event.is_set():
            try:
                self.tick()
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

        for symbol in list(self.positions):
            p = self.positions[symbol]
            try:
                cur = self.api.price(symbol)
            except Exception:
                continue
            hit_stop = (p["side"] == "LONG" and cur <= p["stop"]) or \
                       (p["side"] == "SHORT" and cur >= p["stop"])
            hit_tp = (p["side"] == "LONG" and cur >= p["tp"]) or \
                     (p["side"] == "SHORT" and cur <= p["tp"])
            if hit_stop or hit_tp:
                try:
                    self.api.order(symbol,
                                   "Sell" if p["side"] == "LONG" else "Buy",
                                   p["qty"], reduce_only=True)
                except Exception as ex:
                    self.log(f"{symbol}: не удалось закрыть — {ex}")
                    continue
                if hit_stop:
                    self.losses_today += 1
                    self.log(f"СТОП по {symbol}. Стопов за день: {self.losses_today}")
                else:
                    self.log(f"ТЕЙК {symbol} достигнут (+2R)")
                del self.positions[symbol]

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
                side, stop = self.sfp_signal(symbol)
            except Exception as ex:
                self.log(f"{symbol}: ошибка запроса — {ex}")
                continue
            if not side:
                continue

            entry = self.api.price(symbol)
            qty = round(self.api.balance() * self.deposit_pct / entry, 3)
            if qty <= 0:
                continue
            risk = abs(entry - stop)
            tp = entry + self.rr * risk if side == "LONG" else entry - self.rr * risk

            try:
                self.api.order(symbol, "Buy" if side == "LONG" else "Sell", qty)
                self.api.order(symbol, "Sell" if side == "LONG" else "Buy",
                               qty, stop=round(stop, 6), reduce_only=True)
            except Exception as ex:
                self.log(f"{symbol}: ордер не прошёл — {ex}")
                continue
            self.positions[symbol] = {"side": side, "entry": entry,
                                      "stop": stop, "tp": tp, "qty": qty}
            self.log(f"ВХОД {side} {symbol} по {entry}, "
                     f"стоп {stop:.4f}, тейк {tp:.4f} "
                     f"[{len(self.positions)}/{self.max_positions}]")
            break


# ---------- интерфейс ----------

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

    # ---------- хранилище профилей ----------
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

    profiles = load_profiles()   # {имя: {поле: значение}}

    # ---------- поля ----------
    ex = ft.Dropdown(label="Биржа",
                     options=[ft.dropdown.Option("Bybit"),
                              ft.dropdown.Option("BingX")],
                     value="Bybit")
    mode = ft.Dropdown(label="Режим",
                       options=[ft.dropdown.Option("Демо"),
                                ft.dropdown.Option("Реальный")],
                       value="Демо")
    api_key = ft.TextField(label="API-ключ", password=True, can_reveal_password=True)
    secret = ft.TextField(label="Секрет", password=True, can_reveal_password=True)
    symbols = ft.TextField(
        label="Пары через запятую",
        value="BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,LINKUSDT,LTCUSDT,"
              "AVAXUSDT,TRXUSDT,DOTUSDT,MATICUSDT,NEARUSDT,APTUSDT,ARBUSDT,"
              "OPUSDT,SUIUSDT,TONUSDT,FILUSDT,ATOMUSDT")
    deposit = ft.TextField(label="Доля депозита на сделку (0.05 = 5%)", value="0.05")
    losses = ft.TextField(label="Макс. стопов в день", value="3")
    maxpos = ft.TextField(label="Макс. позиций одновременно", value="2")
    tg_token = ft.TextField(label="Токен Telegram-бота", password=True, can_reveal_password=True)
    tg_chat = ft.TextField(label="Ваш chat_id в Telegram")

    profile_name = ft.TextField(label="Имя профиля", hint_text="например: Bybit-демо")
    profile_dd = ft.Dropdown(
        label="Сохранённые профили",
        options=[ft.dropdown.Option(k) for k in sorted(profiles)],
        value=sorted(profiles)[0] if profiles else None)

    status = ft.Text("Остановлен", size=18, weight=ft.FontWeight.BOLD)
    log_view = ft.ListView(spacing=5, auto_scroll=True, expand=True)
    cfg = {"token": "", "chat": ""}

    def add_log(msg):
        log_view.controls.append(
            ft.Text(f"{dt.datetime.now():%H:%M:%S}  {msg}"))
        tg_send(cfg, msg)
        page.update()

    # ---------- работа с профилями ----------
    FIELDS = {"ex": ex, "mode": mode, "api_key": api_key, "secret": secret,
              "symbols": symbols, "deposit": deposit, "losses": losses,
              "maxpos": maxpos, "tg_token": tg_token, "tg_chat": tg_chat}

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

    def on_pick(e):
        # мгновенная подгрузка при выборе из списка
        on_load(None)

    profile_dd.on_change = on_pick

    state = {"bot": None, "thread": None}

    def on_start(e):
        if state["thread"] and state["thread"].is_alive():
            return
        pairs = [s.strip().replace("-", "").replace("/", "").upper()
                 for s in symbols.value.split(",") if s.strip()]
        if not pairs:
            add_log("Укажите хотя бы одну пару")
            return
        if not api_key.value.strip() or not secret.value.strip():
            add_log("Введите API-ключ и секрет")
            return
        demo = mode.value == "Демо"
        if ex.value == "Bybit":
            api = BybitAPI(api_key.value.strip(), secret.value.strip(), demo)
        else:
            api = BingXAPI(api_key.value.strip(), secret.value.strip(), demo)
        try:
            bot = Bot(api, pairs, float(deposit.value), int(losses.value),
                      int(maxpos.value), add_log)
        except ValueError:
            add_log("Проверьте числа в настройках")
            return
        cfg["token"] = tg_token.value.strip()
        cfg["chat"] = tg_chat.value.strip()
        state["bot"] = bot
        state["thread"] = threading.Thread(target=bot.run, daemon=True)
        state["thread"].start()
        status.value = f"Работает: {ex.value} / {mode.value}"
        page.update()

    def on_stop(e):
        if state["bot"]:
            state["bot"].stop_event.set()
        status.value = "Остановлен"
        page.update()

    page.add(ft.Column([
        # --- профили ---
        profile_dd,
        ft.Row([profile_name,
                ft.FilledButton("Сохранить", on_click=on_save)]),
        ft.Row([ft.FilledButton("Загрузить", on_click=on_load),
                ft.OutlinedButton("Удалить", on_click=on_delete)]),
        ft.Divider(),
        # --- настройки ---
        ex, mode,
        api_key, secret,
        symbols,
        deposit, losses, maxpos,
        tg_token, tg_chat,
        ft.Row([ft.FilledButton("Запустить", on_click=on_start),
                ft.FilledButton("Остановить", on_click=on_stop)]),
        status,
        ft.Divider(), ft.Text("Журнал:"), log_view,
    ], spacing=12, scroll=ft.ScrollMode.AUTO, expand=True))

    # автозагрузка последнего профиля при старте приложения
    try:
        last = page.client_storage.get(LAST_KEY)
        if last and last in profiles:
            apply(profiles[last])
            refresh_dd(last)
    except Exception:
        pass


ft.run(main)
