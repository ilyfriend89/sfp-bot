import time, hmac, hashlib, urllib.parse, threading, json
import datetime as dt
import requests
import flet as ft

# Для демо-торговли замените на "https://api-testnet.bybit.com"
# и получите ключи на testnet.bybit.com
BYBIT_URL = "https://api.bybit.com"


# ---------- подпись Bybit v5 ----------

def bybit_headers(api_key, secret, payload):
    ts = str(int(time.time() * 1000))
    sign = hmac.new(secret.encode(),
                    (ts + api_key + "5000" + payload).encode(),
                    hashlib.sha256).hexdigest()
    return {"X-BAPI-API-KEY": api_key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": "5000",
            "X-BAPI-SIGN": sign,
            "Content-Type": "application/json"}


# ---------- торговое ядро ----------

class Bot:
    def __init__(self, api_key, secret, symbol, deposit_pct,
                 max_losses_per_day, log):
        self.api_key, self.secret = api_key, secret
        self.symbol = symbol                  # формат Bybit: BTCUSDT
        self.deposit_pct = deposit_pct        # доля депозита на сделку, 0..1
        self.max_losses = max_losses_per_day
        self.log = log
        self.stop_event = threading.Event()
        self.rr = 2.0          # тейк = 2 риска
        self.lev = 3           # плечо
        self.day = dt.date.today()
        self.losses_today = 0
        self.position = None   # dict: side, entry, stop, tp, qty

    # --- API ---
    def req(self, method, path, params=None):
        params = params or {}
        if method == "GET":
            qs = urllib.parse.urlencode(params)
            headers = bybit_headers(self.api_key, self.secret, qs)
            r = requests.get(f"{BYBIT_URL}{path}?{qs}", headers=headers, timeout=15)
        else:
            body = json.dumps(params)
            headers = bybit_headers(self.api_key, self.secret, body)
            r = requests.post(f"{BYBIT_URL}{path}", data=body, headers=headers, timeout=15)
        d = r.json()
        if d.get("retCode") != 0:
            raise RuntimeError(f"Bybit: {d.get('retMsg')}")
        return d["result"]

    def candles(self, limit=30):
        d = self.req("GET", "/v5/market/kline",
                     {"category": "linear", "symbol": self.symbol,
                      "interval": "240", "limit": limit})  # 240 мин = 4H
        rows = [(float(k[1]), float(k[2]), float(k[3]),
                 float(k[4]), float(k[5])) for k in d["list"]]
        return list(reversed(rows))  # Bybit отдаёт от новых к старым

    def price(self):
        d = self.req("GET", "/v5/market/tickers",
                     {"category": "linear", "symbol": self.symbol})
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

    def order(self, side, qty, stop=None, reduce_only=False):
        p = {"category": "linear", "symbol": self.symbol,
             "side": side, "orderType": "Market",
             "qty": str(qty), "positionIdx": 0}
        if stop:
            p["triggerPrice"] = str(stop)
            p["triggerBy"] = "LastPrice"
        if reduce_only:
            p["reduceOnly"] = True
        return self.req("POST", "/v5/order/create", p)

    # --- стратегия: SFP (ложный пробой) на 4H ---
    def sfp_signal(self):
        c = self.candles(limit=30)
        prev, last = c[-2], c[-1]
        avg_vol = sum(x[4] for x in c[-21:-1]) / 20

        now = dt.datetime.utcnow()
        kill_zone = (7 <= now.hour < 10) or (13 <= now.hour < 16)  # Лондон/НЙ по UTC

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

    # --- цикл ---
    def run(self):
        self.log("Бот запущен, стратегия SFP на 4H (Bybit)")
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

        if self.position:
            p = self.position
            cur = self.price()
            hit_stop = (p["side"] == "LONG" and cur <= p["stop"]) or \
                       (p["side"] == "SHORT" and cur >= p["stop"])
            hit_tp = (p["side"] == "LONG" and cur >= p["tp"]) or \
                     (p["side"] == "SHORT" and cur <= p["tp"])
            if hit_stop or hit_tp:
                self.order("Sell" if p["side"] == "LONG" else "Buy",
                           p["qty"], reduce_only=True)
                if hit_stop:
                    self.losses_today += 1
                    self.log(f"СТОП по {self.symbol}. Стопов за день: {self.losses_today}")
                else:
                    self.log(f"ТЕЙК {self.symbol} достигнут (+2R)")
                self.position = None

        if self.losses_today >= self.max_losses:
            return

        if self.position:
            return

        side, stop = self.sfp_signal()
        if not side:
            return

        entry = self.price()
        qty = round(self.balance() * self.deposit_pct / entry, 3)
        if qty <= 0:
            return
        risk = abs(entry - stop)
        tp = entry + self.rr * risk if side == "LONG" else entry - self.rr * risk

        self.order("Buy" if side == "LONG" else "Sell", qty)
        self.order("Sell" if side == "LONG" else "Buy", qty,
                   stop=round(stop, 6), reduce_only=True)
        self.position = {"side": side, "entry": entry,
                         "stop": stop, "tp": tp, "qty": qty}
        self.log(f"ВХОД {side} {self.symbol} по {entry}, стоп {stop:.4f}, тейк {tp:.4f}")


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
    page.title = "SFP-бот Bybit"
    page.padding = 20

    api_key = ft.TextField(label="API-ключ Bybit", password=True, can_reveal_password=True)
    secret = ft.TextField(label="Секрет Bybit", password=True, can_reveal_password=True)
    symbol = ft.TextField(label="Пара (формат Bybit)", value="BTCUSDT")
    deposit = ft.TextField(label="Доля депозита на сделку (0.05 = 5%)", value="0.05")
    losses = ft.TextField(label="Макс. стопов в день", value="3")
    tg_token = ft.TextField(label="Токен Telegram-бота", password=True, can_reveal_password=True)
    tg_chat = ft.TextField(label="Ваш chat_id в Telegram")

    status = ft.Text("Остановлен", size=18, weight=ft.FontWeight.BOLD)
    log_view = ft.ListView(spacing=5, auto_scroll=True, expand=True)
    cfg = {"token": "", "chat": ""}

    def add_log(msg):
        log_view.controls.append(
            ft.Text(f"{dt.datetime.now():%H:%M:%S}  {msg}"))
        tg_send(cfg, msg)
        page.update()

    state = {"bot": None, "thread": None}

    def on_start(e):
        if state["thread"] and state["thread"].is_alive():
            return
        try:
            bot = Bot(api_key.value.strip(), secret.value.strip(),
                      symbol.value.strip().replace("-", ""),  # BTC-USDT -> BTCUSDT
                      float(deposit.value), int(losses.value), add_log)
        except ValueError:
            add_log("Проверьте числа в настройках")
            return
        cfg["token"] = tg_token.value.strip()
        cfg["chat"] = tg_chat.value.strip()
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
        ft.Row([tg_token, tg_chat]),
        ft.Row([ft.FilledButton("Запустить", on_click=on_start),
                ft.FilledButton("Остановить", on_click=on_stop),
                status]),
        ft.Divider(), ft.Text("Журнал:"), log_view,
    ], expand=True))


ft.run(main)
