import time, hmac, hashlib, urllib.parse, threading
import datetime as dt
import requests
import flet as ft

API_URL = "https://open-api.bingx.com"

# ---------- торговое ядро ----------

class Bot:
    def __init__(self, api_key, secret, symbol, deposit_pct,
                 max_losses_per_day, log):
        self.api_key, self.secret = api_key, secret
        self.symbol = symbol
        self.deposit_pct = deposit_pct          # доля депозита на сделку, 0..1
        self.max_losses = max_losses_per_day    # стоп-торговля после N стопов
        self.log = log
        self.stop_event = threading.Event()
        self.rr = 2.0          # тейк = 2 риска
        self.lev = 3           # плечо для этой стратегии 3-5х
        self.day = dt.date.today()
        self.losses_today = 0
        self.position = None   # dict: side, entry, stop, tp, qty

    # --- API ---
    def sign(self, qs):
        return hmac.new(self.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()

    def req(self, method, path, params):
        params["timestamp"] = int(time.time() * 1000)
        qs = urllib.parse.urlencode(params)
        qs += "&signature=" + self.sign(qs)
        r = requests.request(method, f"{API_URL}{path}?{qs}",
                             headers={"X-BX-APIKEY": self.api_key}, timeout=15)
        r.raise_for_status()
        return r.json()

    def candles(self, interval="4h", limit=60):
        d = self.req("GET", "/openApi/swap/v2/quote/klines",
                     {"symbol": self.symbol, "interval": interval, "limit": limit})
        return [(float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5]))
                for c in d["data"]["candles"]]  # (open, high, low, close, volume)

    def price(self):
        d = self.req("GET", "/openApi/swap/v2/quote/price", {"symbol": self.symbol})
        return float(d["data"]["price"])

    def balance(self):
        d = self.req("GET", "/openApi/swap/v2/user/balance", {})
        for a in d["data"]["balance"]:
            if a["asset"] == "USDT":
                return float(a["balance"])
        return 0.0

    def order(self, side, qty, stop=None, reduce_only=False):
        p = {"symbol": self.symbol, "side": side,
             "type": "TRIGGER_MARKET" if stop else "MARKET",
             "quantity": qty, "positionSide": "ONE_WAY", "workingType": "MARKET_PRICE"}
        if stop:
            p["stopPrice"] = stop
        if reduce_only:
            p["reduceOnly"] = True
        return self.req("POST", "/openApi/swap/v2/trade/order", p)

    # --- стратегия: SFP (ложный пробой) на 4H ---
    def sfp_signal(self):
        """Возвращает 'LONG'/'SHORT' или None.
        SFP: свеча хвостом выходит за экстремум предыдущей свечи,
        но закрывается обратно внутри. Плюс фильтры объёма и kill zones."""
        c = self.candles(limit=30)
        prev, last = c[-2], c[-1]
        avg_vol = sum(x[4] for x in c[-21:-1]) / 20

        now = dt.datetime.utcnow()
        kill_zone = (7 <= now.hour < 10) or (13 <= now.hour < 16)  # Лондон/НЙ по UTC

        long_sig = (last[2] < prev[2] and            # минимум пробил низ прошлой свечи
                    last[3] > prev[2] and            # закрылась обратно выше
                    last[4] > 1.5 * avg_vol and      # объём
                    kill_zone)
        short_sig = (last[1] > prev[1] and           # максимум пробил верх
                     last[3] < prev[1] and           # закрылась обратно ниже
                     last[4] > 1.5 * avg_vol and
                     kill_zone)
        if long_sig:
            return "LONG", last[2]   # стоп под хвостом
        if short_sig:
            return "SHORT", last[1]  # стоп над хвостом
        return None, None

    # --- цикл ---
    def run(self):
        self.log("Бот запущен, стратегия SFP на 4H")
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

        # сопровождение открытой позиции
        if self.position:
            p = self.position
            cur = self.price()
            hit_stop = (p["side"] == "LONG" and cur <= p["stop"]) or \
                       (p["side"] == "SHORT" and cur >= p["stop"])
            hit_tp = (p["side"] == "LONG" and cur >= p["tp"]) or \
                     (p["side"] == "SHORT" and cur <= p["tp"])
            if hit_stop or hit_tp:
                self.order("SELL" if p["side"] == "LONG" else "BUY",
                           p["qty"], reduce_only=True)
                if hit_stop:
                    self.losses_today += 1
                    self.log(f"СТОП по {self.symbol}. Стопов за день: {self.losses_today}")
                else:
                    self.log(f"ТЕЙК {self.symbol} достигнут (+2R)")
                self.position = None

        if self.losses_today >= self.max_losses:
            return  # дневной лимит потерь — ждём завтра

        if self.position:
            return

        side, stop = self.sfp_signal()
        if not side:
            return

        entry = self.price()
        qty = round(self.balance() * self.deposit_pct / entry, 4)
        if qty <= 0:
            return
        risk = abs(entry - stop)
        tp = entry + self.rr * risk if side == "LONG" else entry - self.rr * risk

        self.order("BUY" if side == "LONG" else "SELL", qty)
        # защитный стоп ордером, чтобы сработал даже если бот ляжет
        self.order("SELL" if side == "LONG" else "BUY", qty,
                   stop=round(stop, 6), reduce_only=True)
        self.position = {"side": side, "entry": entry,
                         "stop": stop, "tp": tp, "qty": qty}
        self.log(f"ВХОД {side} {self.symbol} по {entry}, стоп {stop:.4f}, тейк {tp:.4f}")

# ---------- интерфейс ----------

def main(page: ft.Page):
    page.title = "SFP-бот BingX"
    page.padding = 20

    api_key = ft.TextField(label="API-ключ", password=True, can_reveal_password=True)
    secret = ft.TextField(label="Секрет", password=True, can_reveal_password=True)
    symbol = ft.TextField(label="Пара", value="BTC-USDT")
    deposit = ft.TextField(label="Доля депозита на сделку (0.05 = 5%)", value="0.05")
    losses = ft.TextField(label="Макс. стопов в день", value="3")

    status = ft.Text("Остановлен", size=18, weight=ft.FontWeight.BOLD)
    log_view = ft.ListView(spacing=5, auto_scroll=True, expand=True)

    def add_log(msg):
        log_view.controls.append(
            ft.Text(f"{dt.datetime.now():%H:%M:%S}  {msg}"))
        page.update()

    state = {"bot": None, "thread": None}

    def on_start(e):
        if state["thread"] and state["thread"].is_alive():
            return
        try:
            bot = Bot(api_key.value.strip(), secret.value.strip(),
                      symbol.value.strip(), float(deposit.value),
                      int(losses.value), add_log)
        except ValueError:
            add_log("Проверьте числа в настройках")
            return
        state["bot"] = bot
        state["thread"] = threading.Thread(target=bot.run, daemon=True)
        state["thread"].start()
        status.value = "Работает"
        page.update()

    def on_stop(e):
        if state["bot"]:
            state["bot"].stop_event.set()
        status.value = "Остановлен"
        page.update()

    page.add(ft.Column([
        api_key, secret,
        ft.Row([symbol, deposit, losses]),
        ft.Row([ft.FilledButton("Запустить", on_click=on_start),
                ft.FilledButton("Остановить", on_click=on_stop),
                status]),
        ft.Divider(), ft.Text("Журнал:"), log_view,
    ], expand=True))

ft.run(main)
