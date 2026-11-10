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
    def __init__(self, api_key, secret, symbols, deposit_pct,
                 max_losses_per_day, max_positions, log):
        self.api_key, self.secret = api_key, secret
        self.symbols = symbols               # список пар для сканирования
        self.deposit_pct = deposit_pct
        self.max_losses = max_losses_per_day
        self.max_positions = max_positions   # сколько позиций держим одновременно
        self.log = log
        self.stop_event = threading.Event()
        self.rr = 2.0
        self.lev = 3
        self.day = dt.date.today()
        self.losses_today = 0
        self.positions = {}                  # {symbol: {side, entry, stop, tp, qty}}

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

    # --- стратегия: SFP (ложный пробой) на 4H ---
    def sfp_signal(self, symbol):
        c = self.candles(symbol, limit=30)
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

    # --- цикл ---
    def run(self):
        self.log(f"Бот запущен. Пары: {', '.join(self.symbols)}. "
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

        # сопровождение открытых позиций
        for symbol in list(self.positions):
            p = self.positions[symbol]
            try:
                cur = self.price(symbol)
            except Exception:
                continue  # цена не пришла — проверим на следующем тике
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
    page.title = "SFP-бот Bybit"
    page.padding = 20

    api_key = ft.TextField(label="API-ключ Bybit", password=True, can_reveal_password=True)
    secret = ft.TextField(label="Секрет Bybit", password=True, can_reveal_password=True)
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
        pairs = [s.strip().replace("-", "").replace("/", "").upper()
                 for s in symbols.value.split(",") if s.strip()]
        if not pairs:
            add_log("Укажите хотя бы одну пару")
            return
        try:
            bot = Bot(api_key.value.strip(), secret.value.strip(),
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
        page.update()

    def on_stop(e):
        if state["bot"]:
            state["bot"].stop_event.set()
        status.value = "Остановлен"
        page.update()

    page.add(ft.Column([
        api_key, secret,
        symbols,
        ft.Row([deposit, losses, maxpos]),
        ft.Row([tg_token, tg_chat]),
        ft.Row([ft.FilledButton("Запустить", on_click=on_start),
                ft.FilledButton("Остановить", on_click=on_stop),
                status]),
        ft.Divider(), ft.Text("Журнал:"), log_view,
    ], expand=True))


ft.run(main)
