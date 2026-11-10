import time, hmac, hashlib, urllib.parse, threading, json
import datetime as dt
import requests
import flet as ft

# ---------- общие утилиты ----------

def ts_ms():
    return str(int(time.time() * 1000))

# ---------- API Bybit v5 ----------

def bybit_headers(api_key, secret, payload):
    ts = ts_ms()
    sign = hmac.new(secret.encode(),
                    (ts + api_key + "5000" + payload).encode(),
                    hashlib.sha256).hexdigest()
    return {"X-BAPI-API-KEY": api_key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": "5000",
            "X-BAPI-SIGN": sign,
            "Content-Type": "application/json"}

def bybit_req(base_url, api_key, secret, method, path, params=None):
    params = params or {}
    if method == "GET":
        qs = urllib.parse.urlencode(params)
        headers = bybit_headers(api_key, secret, qs)
        r = requests.get(f"{base_url}{path}?{qs}", headers=headers, timeout=15)
    else:
        body = json.dumps(params)
        headers = bybit_headers(api_key, secret, body)
        r = requests.post(f"{base_url}{path}", data=body, headers=headers, timeout=15)
    d = r.json()
    if d.get("retCode") != 0:
        raise RuntimeError(f"Bybit: {d.get('retMsg')}")
    return d["result"]

# ---------- API BingX v3 (Futures) ----------

def bingx_headers(api_key, secret, payload, ts):
    sign = hmac.new(secret.encode(), f"{ts}{payload}".encode(), hashlib.sha256).hexdigest()
    return {"X-BX-APIKEY": api_key,
            "Content-Type": "application/json",
            "X-BX-TIMESTAMP": ts,
            "X-BX-SIGNATURE": sign}

def bingx_req(base_url, api_key, secret, method, path, params=None):
    params = params or {}
    ts = ts_ms()
    if method == "GET":
        qs = urllib.parse.urlencode(params)
        headers = bingx_headers(api_key, secret, qs, ts)
        r = requests.get(f"{base_url}{path}?{qs}", headers=headers, timeout=15)
    else:
        body = json.dumps(params)
        headers = bingx_headers(api_key, secret, body, ts)
        r = requests.post(f"{base_url}{path}", data=body, headers=headers, timeout=15)
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"BingX: {d.get('msg')}")
    return d["data"]

# ---------- торговое ядро ----------

class Bot:
    def __init__(self, exchange, is_demo, api_key, secret, symbols, deposit_pct,
                 max_losses_per_day, max_positions, log):
        self.exchange = exchange          # "bybit" | "bingx"
        self.is_demo = is_demo            # True = testnet
        self.api_key, self.secret = api_key, secret
        self.symbols = symbols
        self.deposit_pct = deposit_pct
        self.max_losses = max_losses_per_day
        self.max_positions = max_positions
        self.log = log
        self.stop_event = threading.Event()
        self.rr = 2.0
        self.day = dt.date.today()
        self.losses_today = 0
        self.positions = {}

    # --- URL и методы по бирже ---
    def base_url(self):
        if self.exchange == "bybit":
            return "https://api-testnet.bybit.com" if self.is_demo else "https://api.bybit.com"
        else:  # bingx
            return "https://open-api.bingx.com" if self.is_demo else "https://open-api.bingx.com"

    def req(self, method, path, params=None):
        if self.exchange == "bybit":
            return bybit_req(self.base_url(), self.api_key, self.secret, method, path, params)
        else:
            return bingx_req(self.base_url(), self.api_key, self.secret, method, path, params)

    def candles(self, symbol, limit=30):
        if self.exchange == "bybit":
            d = self.req("GET", "/v5/market/kline",
                         {"category": "linear", "symbol": symbol,
                          "interval": "240", "limit": limit})
            rows = [(float(k), float(k), float(k), [1](https://bybot.me/ru/docs/exchanges/bingx/)[2](https://ivankornev.com/ru/articles/android/setup/ios/setup/kak-vklyuchit-avtopovorot-ekrana-ispravit-povernite-telefon/)[3](https://martinbots.work/knowledge/faq/kak-perevesti-bota-iz-demo-v-real-ili-naoborot)
                     float(k), float(k)) for k in d["list"]] [4](https://dlthub.com/context/source/bingx)[5](https://media.halvacard.ru/tech/avtopovorot-ekrana)
            return list(reversed(rows))
        else:  # bingx
            d = self.req("GET", "/v3/public/klines",
                         {"symbol": symbol, "interval": "240", "limit": limit})
            # OHLCV: open, high, low, close, volume
            rows = [(float(k), float(k), float(k), [1](https://bybot.me/ru/docs/exchanges/bingx/)[3](https://martinbots.work/knowledge/faq/kak-perevesti-bota-iz-demo-v-real-ili-naoborot)[4](https://dlthub.com/context/source/bingx)
                     float(k), float(k)) for k in d] [2](https://ivankornev.com/ru/articles/android/setup/ios/setup/kak-vklyuchit-avtopovorot-ekrana-ispravit-povernite-telefon/)[5](https://media.halvacard.ru/tech/avtopovorot-ekrana)
            return list(reversed(rows))

    def price(self, symbol):
        if self.exchange == "bybit":
            d = self.req("GET", "/v5/market/tickers",
                         {"category": "linear", "symbol": symbol})
            return float(d["list"]["lastPrice"])
        else:
            d = self.req("GET", "/v2/public/ticker", {"symbol": symbol})
            return float(d["last"])

    def balance(self):
        if self.exchange == "bybit":
            for acct in ("UNIFIED", "CONTRACT"):
                try:
                    d = self.req("GET", "/v5/account/wallet-balance",
                                 {"accountType": acct})
                    for coin in d["list"]["coin"]:
                        if coin["coin"] == "USDT":
                            return float(coin["walletBalance"])
                except Exception:
                    continue
            return 0.0
        else:  # bingx
            try:
                d = self.req("GET", "/v2/user/balance")
                for a in d["data"]:
                    if a["coin"] == "USDT":
                        return float(a["available"])
            except Exception:
                pass
            return 0.0

    def order(self, symbol, side, qty, stop=None, reduce_only=False):
        if self.exchange == "bybit":
            p = {"category": "linear", "symbol": symbol,
                 "side": side, "orderType": "Market",
                 "qty": str(qty), "positionIdx": 0}
            if stop:
                p["triggerPrice"] = str(stop)
                p["triggerBy"] = "LastPrice"
            if reduce_only:
                p["reduceOnly"] = True
            return self.req("POST", "/v5/order/create", p)
        else:  # bingx
            p = {"symbol": symbol, "side": side,
                 "type": "market", "marginType": "cross",
                 "quantity": str(qty)}
            if stop:
                p["triggerPrice"] = str(stop)
                p["triggerType"] = "mark"
            if reduce_only:
                p["reduceOnly"] = True
            return self.req("POST", "/v3/order/place", p)

    # --- стратегия: SFP (ложный пробой) на 4H ---
    def sfp_signal(self, symbol):
        c = self.candles(symbol, limit=30)
        if len(c) < 2:
            return None, None
        prev, last = c[-2], c[-1]
        avg_vol = sum(x for x in c[-21:-1]) / 20 if len(c) > 21 else c[-1] [4](https://dlthub.com/context/source/bingx)

        now = dt.datetime.utcnow()
        kill_zone = (7 <= now.hour < 10) or (13 <= now.hour < 16)

        # Bybit: [open, high, low, close, vol]
        # BingX: [open, low, high, close, vol] -> high=x, low=x [2](https://ivankornev.com/ru/articles/android/setup/ios/setup/kak-vklyuchit-avtopovorot-ekrana-ispravit-povernite-telefon/)[3](https://martinbots.work/knowledge/faq/kak-perevesti-bota-iz-demo-v-real-ili-naoborot)
        if self.exchange == "bybit":
            prev_high, prev_low = prev, prev [2](https://ivankornev.com/ru/articles/android/setup/ios/setup/kak-vklyuchit-avtopovorot-ekrana-ispravit-povernite-telefon/)[3](https://martinbots.work/knowledge/faq/kak-perevesti-bota-iz-demo-v-real-ili-naoborot)
            last_high, last_low = last, last [2](https://ivankornev.com/ru/articles/android/setup/ios/setup/kak-vklyuchit-avtopovorot-ekrana-ispravit-povernite-telefon/)[3](https://martinbots.work/knowledge/faq/kak-perevesti-bota-iz-demo-v-real-ili-naoborot)
            last_close = last [3](https://martinbots.work/knowledge/faq/kak-perevesti-bota-iz-demo-v-real-ili-naoborot)
        else:
            prev_high, prev_low = prev, prev [2](https://ivankornev.com/ru/articles/android/setup/ios/setup/kak-vklyuchit-avtopovorot-ekrana-ispravit-povernite-telefon/)[3](https://martinbots.work/knowledge/faq/kak-perevesti-bota-iz-demo-v-real-ili-naoborot)
            last_high, last_low = last, last [2](https://ivankornev.com/ru/articles/android/setup/ios/setup/kak-vklyuchit-avtopovorot-ekrana-ispravit-povernite-telefon/)[3](https://martinbots.work/knowledge/faq/kak-perevesti-bota-iz-demo-v-real-ili-naoborot)
            last_close = last [3](https://martinbots.work/knowledge/faq/kak-perevesti-bota-iz-demo-v-real-ili-naoborot)

        long_sig = (last_low < prev_low and
                    last_high > prev_low and
                    last > 1.5 * avg_vol and [4](https://dlthub.com/context/source/bingx)
                    kill_zone)
        short_sig = (last_high > prev_high and
                     last_low < prev_high and
                     last > 1.5 * avg_vol and [4](https://dlthub.com/context/source/bingx)
                     kill_zone)
        if long_sig:
            return "LONG", prev_low
        if short_sig:
            return "SHORT", prev_high
        return None, None

    # --- цикл ---
    def run(self):
        self.log(f"Бот запущен. Биржа: {self.exchange}, счёт: {'демо' if self.is_demo else 'реал'}, "
                 f"пары: {', '.join(self.symbols)}, лимит позиций: {self.max_positions}")
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

        # сопровождение открытых позиций
        for symbol in list(self.positions):
            p = self.positions[symbol]
            try:
                cur = self.price(symbol)
            except Exception:
                continue
            hit_stop = (p["side"] == "LONG" and cur <= p["stop"]) or \
                       (p["side"] == "SHORT" and cur >= p["stop"])
            hit_tp = (p["side"] == "LONG" and cur >= p["tp"]) or \
                     (p["side"] == "SHORT" and cur <= p["tp"])
            if hit_stop or hit_tp:
                try:
                    self.order(symbol,
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

        # сканируем пары, по которым ещё нет позиции
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

            entry = self.price(symbol)
            qty = round(self.balance() * self.deposit_pct / entry, 3)
            if qty <= 0:
                continue
            risk = abs(entry - stop)
            tp = entry + self.rr * risk if side == "LONG" else entry - self.rr * risk

            try:
                self.order(symbol, "Buy" if side == "LONG" else "Sell", qty)
                self.order(symbol, "Sell" if side == "LONG" else "Buy",
                           qty, stop=round(stop, 6), reduce_only=True)
            except Exception as ex:
                self.log(f"{symbol}: ордер не прошёл — {ex}")
                continue
            self.positions[symbol] = {"side": side, "entry": entry,
                                      "stop": stop, "tp": tp, "qty": qty}
            self.log(f"ВХОД {side} {symbol} по {entry}, "
                     f"стоп {stop:.4f}, тейк {tp:.4f} "
                     f"[{len(self.positions)}/{self.max_positions}]")

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
    page.title = "SFP-бот (Bybit/BingX)"
    page.padding = 10
    page.scroll = ft.ScrollMode.AUTO  # вертикальная прокрутка всей страницы

    # поля
    api_key = ft.TextField(label="API-ключ", password=True, can_reveal_password=True, expand=True)
    secret = ft.TextField(label="Секрет", password=True, can_reveal_password=True, expand=True)
    exchange = ft.Dropdown(
        label="Биржа",
        options=[ft.dropdown.Option("Bybit"), ft.dropdown.Option("BingX")],
        value="Bybit",
        width=180
    )
    is_demo = ft.Checkbox(label="Демо-счёт", value=True)
    symbols = ft.TextField(
        label="Пары через запятую",
        value="BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,LINKUSDT,LTCUSDT,"
              "AVAXUSDT,TRXUSDT,DOTUSDT,MATICUSDT,NEARUSDT,APTUSDT,ARBUSDT,"
              "OPUSDT,SUIUSDT,TONUSDT,FILUSDT,ATOMUSDT",
        multiline=True,
        height=100,
        expand=True
    )
    deposit = ft.TextField(label="Доля депозита на сделку (0.05 = 5%)", value="0.05")
    losses = ft.TextField(label="Макс. стопов в день", value="3")
    maxpos = ft.TextField(label="Макс. позиций одновременно", value="2")
    tg_token = ft.TextField(label="Токен Telegram-бота", password=True, can_reveal_password=True, expand=True)
    tg_chat = ft.TextField(label="Ваш chat_id в Telegram", expand=True)

    status = ft.Text("Остановлен", size=18, weight=ft.FontWeight.BOLD, color=ft.colors.GREY)
    log_view = ft.ListView(spacing=5, auto_scroll=True, expand=True, height=300)
    cfg = {"token": "", "chat": ""}

    def add_log(msg):
        log_view.controls.append(ft.Text(f"{dt.datetime.now():%H:%M:%S}  {msg}"))
        tg_send(cfg, msg)
        page.update()

    state = {"bot": None, "thread": None}

    def on_start(e):
        if state["thread"] and state["thread"].is_alive():
            return
        pairs = [s.strip().replace("-", "").replace("/", "").upper()
                 for s in symbols.value.split(",") if s.strip()]
        if not pairs:
            add_log("Укажите хотя бы одну пару")
            return
        ex_name = exchange.value.lower()
        if ex_name not in ("bybit", "bingx"):
            add_log("Выберите корректную биржу")
            return
        try:
            bot = Bot(ex_name, is_demo.value,
                      api_key.value.strip(), secret.value.strip(),
                      pairs, float(deposit.value), int(losses.value),
                      int(maxpos.value), add_log)
        except ValueError:
            add_log("Проверьте числа в настройках")
            return
        cfg["token"] = tg_token.value.strip()
        cfg["chat"] = tg_chat.value.strip()
        state["bot"] = bot
        state["thread"] = threading.Thread(target=bot.run, daemon=True)
        state["thread"].start()
        status.value = "Работает"
        status.color = ft.colors.GREEN
        page.update()

    def on_stop(e):
        if state["bot"]:
            state["bot"].stop_event.set()
        status.value = "Остановлен"
        status.color = ft.colors.GREY
        page.update()

    # адаптивная сетка
    settings_row_1 = ft.Row([exchange, is_demo], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)
    settings_row_2 = ft.Row([deposit, losses, maxpos], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)

    page.add(
        ft.Column([
            ft.Text("Настройки бота", size=20, weight=ft.FontWeight.BOLD),
            ft.Divider(),
            api_key,
            secret,
            settings_row_1,
            symbols,
            settings_row_2,
            ft.Text("Telegram-уведомления", size=16, weight=ft.FontWeight.BOLD),
            tg_token,
            tg_chat,
            ft.Divider(),
            ft.Row([
                ft.FilledButton("Запустить", on_click=on_start, expand=True),
                ft.FilledButton("Остановить", on_click=on_stop, expand=True),
                status
            ]),
            ft.Text("Журнал", size=16, weight=ft.FontWeight.BOLD),
            log_view,
        ], expand=True)
    )

ft.run(main)
