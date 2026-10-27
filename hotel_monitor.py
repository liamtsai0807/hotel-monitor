#!/usr/bin/env python3
"""訂房資訊監控工具。

第一階段：監控「指定房源」，價格一下降就通知。
（另外保留區域搜尋 [[watch]]：依日期、人數、評分、價格區間找新飯店。）

用法：
    python hotel_monitor.py find "飯店名稱"        # 找房源，取得 token 貼進 config
    python hotel_monitor.py check                  # 檢查一次
    python hotel_monitor.py watch                  # 持續監控（依 interval_minutes）
    python hotel_monitor.py history <飯店關鍵字>   # 看價格歷史
    python hotel_monitor.py serve                  # 開網頁看價格走勢圖（並自動檢查）
    python hotel_monitor.py export --out site      # 輸出靜態網頁（雲端部署用）
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
import time
import tomllib
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

# ---------------------------------------------------------------- data models


@dataclass
class Trip:
    """入住日期與人數（價格會隨這些條件變動）。"""
    check_in: str
    check_out: str
    adults: int = 2
    children: int = 0
    children_ages: list[int] = field(default_factory=list)

    def validate(self, label: str) -> None:
        ci, co = date.fromisoformat(self.check_in), date.fromisoformat(self.check_out)
        if co <= ci:
            raise ValueError(f"[{label}] check_out 必須晚於 check_in")
        if ci < date.today():
            raise ValueError(f"[{label}] check_in 已經是過去的日期")
        if self.adults < 1:
            raise ValueError(f"[{label}] 至少要 1 位大人")
        if len(self.children_ages) != self.children:
            raise ValueError(f"[{label}] children_ages 的數量必須等於 children ({self.children})")

    @property
    def nights(self) -> int:
        return (date.fromisoformat(self.check_out) - date.fromisoformat(self.check_in)).days

    @property
    def key(self) -> str:
        ages = ",".join(map(str, self.children_ages))
        return f"{self.check_in}~{self.check_out}|A{self.adults}|C{self.children}({ages})"

    def describe(self) -> str:
        return (f"{self.check_in} → {self.check_out}（{self.nights} 晚）"
                f"｜大人 {self.adults} 人、小孩 {self.children} 人")


@dataclass
class PropertyWatch:
    """指定房源。"""
    name: str
    token: str
    trip: Trip
    target_price: float = 0  # 每晚低於此價格時額外提醒，0 = 不設


@dataclass
class Watch:
    """區域搜尋。"""
    name: str
    location: str
    trip: Trip
    min_rating: float = 0.0
    min_price: float = 0
    max_price: float = 0  # 0 = 不設上限
    max_results: int = 20


@dataclass
class Offer:
    source: str
    price_per_night: float | None
    total_price: float | None
    link: str | None = None


@dataclass
class Hotel:
    key: str               # 唯一識別（property token 或名稱）
    name: str
    price_per_night: float | None
    total_price: float | None
    rating: float | None
    reviews: int | None
    hotel_class: str | None
    link: str | None
    offers: list[Offer] = field(default_factory=list)  # 各訂房網站報價（只有房源查詢才有）

    def best_offer(self) -> Offer | None:
        priced = [o for o in self.offers if o.price_per_night is not None]
        return min(priced, key=lambda o: o.price_per_night) if priced else None

# ------------------------------------------------------------------ providers


def _parse_hotel(p: dict) -> Hotel:
    offers = []
    for o in p.get("prices", []) or p.get("featured_prices", []):
        offers.append(Offer(
            source=o.get("source", "?"),
            price_per_night=(o.get("rate_per_night") or {}).get("extracted_lowest"),
            total_price=(o.get("total_rate") or {}).get("extracted_lowest"),
            link=o.get("link"),
        ))
    return Hotel(
        key=p.get("property_token") or p.get("name", ""),
        name=p.get("name", "(未知)"),
        price_per_night=(p.get("rate_per_night") or {}).get("extracted_lowest"),
        total_price=(p.get("total_rate") or {}).get("extracted_lowest"),
        rating=p.get("overall_rating"),
        reviews=p.get("reviews"),
        hotel_class=p.get("hotel_class"),
        link=p.get("link"),
        offers=offers,
    )


class SerpApiGoogleHotels:
    """透過 SerpApi 查 Google Hotels（彙整 Booking、Agoda、Expedia、官網等價格）。

    需要環境變數 SERPAPI_KEY（https://serpapi.com 免費方案每月 100 次）。
    """

    URL = "https://serpapi.com/search.json"

    def __init__(self, settings: dict):
        self.api_key = os.environ.get("SERPAPI_KEY")
        if not self.api_key:
            sys.exit("請先設定環境變數 SERPAPI_KEY（或在 config.toml 把 provider 改成 \"mock\" 測試）")
        self.settings = settings

    def _base_params(self, q: str, trip: Trip) -> dict:
        params = {
            "engine": "google_hotels",
            "q": q,
            "check_in_date": trip.check_in,
            "check_out_date": trip.check_out,
            "adults": trip.adults,
            "currency": self.settings.get("currency", "TWD"),
            "hl": self.settings.get("language", "zh-TW"),
            "gl": self.settings.get("country", "tw"),
            "api_key": self.api_key,
        }
        if trip.children:
            params["children"] = trip.children
            params["children_ages"] = ",".join(map(str, trip.children_ages))
        return params

    def _get(self, params: dict) -> dict:
        url = f"{self.URL}?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(url, timeout=60) as resp:
            return json.load(resp)

    @staticmethod
    def _rating_param(min_rating: float) -> int | None:
        # Google Hotels 只支援 3.5+ / 4.0+ / 4.5+ 三種篩選，其餘在本地再過濾
        for threshold, code in ((4.5, 9), (4.0, 8), (3.5, 7)):
            if min_rating >= threshold:
                return code
        return None

    def find(self, query: str, trip: Trip) -> list[Hotel]:
        data = self._get(self._base_params(query, trip))
        if "error" in data:
            raise RuntimeError(f"SerpApi 錯誤：{data['error']}")
        if "properties" not in data and data.get("name"):  # 名稱精確命中，直接回傳房源詳情
            return [_parse_hotel(data)]
        return [_parse_hotel(p) for p in data.get("properties", [])]

    def property(self, pw: PropertyWatch) -> Hotel:
        params = self._base_params(pw.name, pw.trip)
        params["property_token"] = pw.token
        data = self._get(params)
        if "error" in data:
            raise RuntimeError(f"SerpApi 錯誤：{data['error']}")
        hotel = _parse_hotel(data)
        hotel.key = pw.token
        return hotel

    def search(self, w: Watch) -> list[Hotel]:
        params = self._base_params(w.location, w.trip)
        params["sort_by"] = 3  # 最低價優先
        if w.min_price:
            params["min_price"] = int(w.min_price)
        if w.max_price:
            params["max_price"] = int(w.max_price)
        if (code := self._rating_param(w.min_rating)) is not None:
            params["rating"] = code

        hotels: list[Hotel] = []
        while len(hotels) < w.max_results:
            data = self._get(params)
            if "error" in data:
                if hotels or "hasn't returned any results" in data["error"]:
                    break
                raise RuntimeError(f"SerpApi 錯誤：{data['error']}")
            hotels += [_parse_hotel(p) for p in data.get("properties", [])]
            token = (data.get("serpapi_pagination") or {}).get("next_page_token")
            if not token:
                break
            params["next_page_token"] = token
        return hotels[: w.max_results]


class MockProvider:
    """產生隨機假資料，用來測試流程（不需要 API key）。"""

    NAMES = ["櫻花飯店", "城市景觀酒店", "河畔旅館", "車站前商務飯店", "和風溫泉旅館",
             "設計師精品酒店", "家庭公寓式酒店", "中央廣場大飯店", "青年旅舍 Plus", "海港度假村"]
    SOURCES = ["Booking.com", "Agoda", "Expedia", "Trip.com", "官方網站"]

    def __init__(self, settings: dict):
        pass

    def _hotel(self, key: str, name: str, nights: int) -> Hotel:
        rnd = random.Random(name)  # 每間飯店的基準價固定
        base = rnd.randint(2000, 12000)
        offers = []
        for src in self.SOURCES:
            price = round(base * random.uniform(0.85, 1.1))  # 每次查詢小幅波動
            offers.append(Offer(src, price, price * nights, f"https://example.com/{src}"))
        best = min(o.price_per_night for o in offers)
        return Hotel(key=key, name=name, price_per_night=best, total_price=best * nights,
                     rating=round(rnd.uniform(3.0, 4.9), 1), reviews=rnd.randint(20, 3000),
                     hotel_class=f"{rnd.randint(2, 5)} 星級飯店", link=None, offers=offers)

    def find(self, query: str, trip: Trip) -> list[Hotel]:
        return [self._hotel(f"mock-{i}", f"{query} {n}", trip.nights)
                for i, n in enumerate(self.NAMES[:5])]

    def property(self, pw: PropertyWatch) -> Hotel:
        return self._hotel(pw.token, pw.name, pw.trip.nights)

    def search(self, w: Watch) -> list[Hotel]:
        return [self._hotel(f"mock-{i}", f"{w.location} {n}", w.trip.nights)
                for i, n in enumerate(self.NAMES)]


PROVIDERS = {"serpapi": SerpApiGoogleHotels, "mock": MockProvider}

# -------------------------------------------------------------------- filters


def matches(h: Hotel, w: Watch) -> bool:
    """本地再過濾一次，確保評分與價格條件精確符合。"""
    if h.price_per_night is None:
        return False
    if w.min_rating and (h.rating is None or h.rating < w.min_rating):
        return False
    if w.min_price and h.price_per_night < w.min_price:
        return False
    if w.max_price and h.price_per_night > w.max_price:
        return False
    return True

# -------------------------------------------------------------------- storage


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS prices (
                watch TEXT, hotel_key TEXT, hotel_name TEXT,
                price_per_night REAL, total_price REAL, rating REAL,
                checked_at TEXT
            )""")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_prices ON prices(watch, hotel_key, checked_at)")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS property_prices (
                token TEXT, trip TEXT, hotel_name TEXT,
                price_per_night REAL, total_price REAL, source TEXT,
                checked_at TEXT
            )""")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_prop ON property_prices(token, trip, checked_at)")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS offer_prices (
                token TEXT, trip TEXT, source TEXT,
                price_per_night REAL, total_price REAL, checked_at TEXT
            )""")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_offer ON offer_prices(token, trip, checked_at)")

    # --- 區域搜尋
    def last_price(self, watch: str, hotel_key: str) -> float | None:
        row = self.db.execute(
            "SELECT price_per_night FROM prices WHERE watch=? AND hotel_key=? "
            "ORDER BY checked_at DESC LIMIT 1", (watch, hotel_key)).fetchone()
        return row[0] if row else None

    def save(self, watch: str, hotels: list[Hotel], ts: str) -> None:
        self.db.executemany(
            "INSERT INTO prices VALUES (?,?,?,?,?,?,?)",
            [(watch, h.key, h.name, h.price_per_night, h.total_price, h.rating, ts) for h in hotels])
        self.db.commit()

    # --- 指定房源
    def property_stats(self, token: str, trip: str) -> tuple[float | None, float | None]:
        """回傳 (上次價格, 歷史最低價)。"""
        last = self.db.execute(
            "SELECT price_per_night FROM property_prices WHERE token=? AND trip=? "
            "ORDER BY checked_at DESC LIMIT 1", (token, trip)).fetchone()
        low = self.db.execute(
            "SELECT MIN(price_per_night) FROM property_prices WHERE token=? AND trip=?",
            (token, trip)).fetchone()
        return (last[0] if last else None), (low[0] if low else None)

    def save_property(self, token: str, trip: str, h: Hotel, source: str, ts: str) -> None:
        self.db.execute("INSERT INTO property_prices VALUES (?,?,?,?,?,?,?)",
                        (token, trip, h.name, h.price_per_night, h.total_price, source, ts))
        self.db.executemany("INSERT INTO offer_prices VALUES (?,?,?,?,?,?)",
                            [(token, trip, o.source, o.price_per_night, o.total_price, ts)
                             for o in h.offers if o.price_per_night is not None])
        self.db.commit()

    def property_series(self, token: str, trip: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT checked_at, price_per_night, total_price, source FROM property_prices "
            "WHERE token=? AND trip=? ORDER BY checked_at", (token, trip)).fetchall()
        return [{"t": t, "price": p, "total": tot, "source": s} for t, p, tot, s in rows]

    def offer_series(self, token: str, trip: str) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for t, src, p in self.db.execute(
                "SELECT checked_at, source, price_per_night FROM offer_prices "
                "WHERE token=? AND trip=? ORDER BY checked_at", (token, trip)):
            out.setdefault(src, []).append({"t": t, "price": p})
        return out

    def history(self, keyword: str) -> list[tuple]:
        return self.db.execute(
            "SELECT hotel_name, trip, price_per_night, source, checked_at FROM property_prices "
            "WHERE hotel_name LIKE ? "
            "UNION ALL "
            "SELECT hotel_name, watch, price_per_night, '', checked_at FROM prices "
            "WHERE hotel_name LIKE ? ORDER BY 1, 2, 5", (f"%{keyword}%", f"%{keyword}%")).fetchall()

# -------------------------------------------------------------- notification


def notify(title: str, lines: list[str]) -> None:
    text = f"🔔 {title}\n" + "\n".join(lines)
    print(text)
    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        body = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", body, timeout=30)
        except Exception as e:  # 通知失敗不影響監控
            print(f"(Telegram 通知失敗：{e})")

# ------------------------------------------------------------------ main flow


def fmt_price(v: float | None, cur: str) -> str:
    return "—" if v is None else f"{cur} {v:,.0f}"


def check_properties(provider, props: list[PropertyWatch], store: Store, settings: dict, ts: str) -> None:
    cur = settings.get("currency", "TWD")
    drop_pct = settings.get("property_drop_alert_pct", 0)

    for pw in props:
        print(f"\n=== 🏨 {pw.name}｜{pw.trip.describe()} ===")
        try:
            h = provider.property(pw)
        except Exception as e:
            print(f"  查詢失敗：{e}")
            continue
        if h.price_per_night is None:
            print("  目前查不到價格（可能已客滿或該日期無房）")
            continue

        best = h.best_offer()
        source = best.source if best and best.price_per_night == h.price_per_night else ""
        link = (best.link if source else None) or h.link
        prev, low = store.property_stats(pw.token, pw.trip.key)
        store.save_property(pw.token, pw.trip.key, h, source, ts)

        for o in sorted((o for o in h.offers if o.price_per_night is not None),
                        key=lambda o: o.price_per_night):
            print(f"  {o.source:<14} 每晚 {fmt_price(o.price_per_night, cur)}"
                  f"  總價 {fmt_price(o.total_price, cur)}")
        summary = (f"最低 每晚 {fmt_price(h.price_per_night, cur)}（總價 {fmt_price(h.total_price, cur)}）"
                   + (f" @ {source}" if source else ""))
        print(f"  ➜ {summary}")

        if prev is None:
            print("  （第一次查詢，已記錄為基準價）")
            continue
        print(f"  上次 {fmt_price(prev, cur)}｜歷史最低 {fmt_price(low, cur)}")

        alerts = []
        if h.price_per_night < prev * (1 - drop_pct / 100):
            diff = prev - h.price_per_night
            alerts.append(f"📉 降價 {fmt_price(diff, cur)}（{diff / prev:.1%}）："
                          f"{fmt_price(prev, cur)} → {fmt_price(h.price_per_night, cur)}")
            if low is not None and h.price_per_night < low:
                alerts.append("🏆 歷史新低！")
        if pw.target_price and h.price_per_night <= pw.target_price < prev:
            alerts.append(f"🎯 低於目標價 {fmt_price(pw.target_price, cur)}")
        if alerts:
            notify(f"{pw.name} 降價了", [pw.trip.describe(), *alerts, summary]
                   + ([link] if link else []))


def check_watches(provider, watches: list[Watch], store: Store, settings: dict, ts: str) -> None:
    cur = settings.get("currency", "TWD")
    drop_pct = settings.get("price_drop_alert_pct", 5)

    for w in watches:
        print(f"\n=== 🔎 {w.name}｜{w.location}｜{w.trip.describe()}"
              f"｜評分 ≥ {w.min_rating}｜每晚 {w.min_price:,.0f}–{w.max_price or '∞'} ===")
        try:
            found = [h for h in provider.search(w) if matches(h, w)]
        except Exception as e:
            print(f"  查詢失敗：{e}")
            continue

        alerts = []
        for h in sorted(found, key=lambda h: h.price_per_night):
            prev = store.last_price(w.name, h.key)
            tag = ""
            if prev is None:
                tag = "🆕 新上榜"
            elif h.price_per_night < prev * (1 - drop_pct / 100):
                tag = f"📉 降價 {(prev - h.price_per_night) / prev:.0%}（原 {fmt_price(prev, cur)}）"
            stars = f"⭐{h.rating}" if h.rating else "⭐—"
            line = (f"{h.name}  {stars}（{h.reviews or 0} 則）  每晚 {fmt_price(h.price_per_night, cur)}"
                    f"  總價 {fmt_price(h.total_price, cur)}")
            print(f"  {line}  {tag}")
            if tag:
                alerts.append(f"{line}  {tag}" + (f"\n    {h.link}" if h.link else ""))

        if not found:
            print("  （目前沒有符合條件的飯店）")
        store.save(w.name, found, ts)
        if alerts:
            notify(f"{w.name}：{len(alerts)} 筆新消息", alerts)


def run_check(cfg: dict, props: list[PropertyWatch], watches: list[Watch], store: Store) -> None:
    settings = cfg["settings"]
    provider = PROVIDERS[settings.get("provider", "serpapi")](settings)
    ts = datetime.now().astimezone().isoformat(timespec="seconds")  # 帶時區，雲端（UTC）網頁才不會差 8 小時
    print(f"\n######## 檢查時間 {ts} ########")
    check_properties(provider, props, store, settings, ts)
    check_watches(provider, watches, store, settings, ts)


TRIP_FIELDS = ("check_in", "check_out", "adults", "children", "children_ages")


def _split_trip(entry: dict, defaults: dict) -> tuple[Trip, dict]:
    """把 entry 拆成 Trip 與其他欄位；沒填的日期/人數用 [defaults] 補上。"""
    merged = {k: defaults[k] for k in TRIP_FIELDS if k in defaults}
    merged.update({k: entry[k] for k in TRIP_FIELDS if k in entry})
    rest = {k: v for k, v in entry.items() if k not in TRIP_FIELDS}
    return Trip(**merged), rest


def load_config(path: str) -> tuple[dict, list[PropertyWatch], list[Watch]]:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    defaults = cfg.get("defaults", {})
    props, watches = [], []
    for e in cfg.get("property", []):
        trip, rest = _split_trip(e, defaults)
        props.append(PropertyWatch(trip=trip, **rest))
    for e in cfg.get("watch", []):
        trip, rest = _split_trip(e, defaults)
        watches.append(Watch(trip=trip, **rest))
    if not props and not watches:
        sys.exit("config 裡沒有任何 [[property]] 或 [[watch]]")
    for x in [*props, *watches]:
        x.trip.validate(x.name)
        if isinstance(x, PropertyWatch) and not x.token:
            raise ValueError(f"[{x.name}] 缺少 token，請先用 find 指令查詢")
    return cfg, props, watches


def cmd_find(cfg: dict, query: str, trip: Trip) -> None:
    settings = cfg["settings"]
    cur = settings.get("currency", "TWD")
    provider = PROVIDERS[settings.get("provider", "serpapi")](settings)
    hotels = provider.find(query, trip)
    if not hotels:
        print("找不到符合的房源，試試更完整的名稱（例如加上城市）")
        return
    print(f"找到 {len(hotels)} 筆（{trip.describe()}）：\n")
    for i, h in enumerate(hotels, 1):
        stars = f"⭐{h.rating}" if h.rating else "⭐—"
        print(f"{i}. {h.name}  {stars}  每晚 {fmt_price(h.price_per_night, cur)}")
        print(f"   token = \"{h.key}\"\n")
    h = hotels[0]
    print("把要監控的房源加進 config.toml，例如：\n")
    print(f"[[property]]\nname = \"{h.name}\"\ntoken = \"{h.key}\"\n# target_price = 0")


# --------------------------------------------------------------------- web UI


def dashboard_data(config_path: str) -> dict:
    cfg, props, _ = load_config(config_path)
    settings = cfg["settings"]
    store = Store(settings.get("db_path", "hotel_monitor.db"))
    items = []
    for pw in props:
        items.append({
            "name": pw.name,
            "token": pw.token,
            "trip": pw.trip.describe(),
            "target_price": pw.target_price or None,
            "series": store.property_series(pw.token, pw.trip.key),
            "offers": store.offer_series(pw.token, pw.trip.key),
        })
    return {"currency": settings.get("currency", "TWD"), "properties": items,
            "interval_minutes": settings.get("interval_minutes", 60)}


def export_site(config_path: str, out_dir: str) -> None:
    """輸出靜態網站（index.html + data.json），給 GitHub Pages 之類的靜態主機用。"""
    import shutil
    os.makedirs(out_dir, exist_ok=True)
    data = dashboard_data(config_path)
    data["static"] = True
    if repo := os.environ.get("GITHUB_REPOSITORY"):  # 在 GitHub Actions 上：附上手動執行的連結
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        data["run_url"] = f"{server}/{repo}/actions/workflows/monitor.yml"
    with open(os.path.join(out_dir, "data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    here = os.path.dirname(os.path.abspath(__file__))
    shutil.copy(os.path.join(here, "dashboard.html"), os.path.join(out_dir, "index.html"))
    print(f"已輸出靜態網站到 {out_dir}/")


def serve(config_path: str, host: str, port: int, auto: bool) -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    page = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
    lock = threading.Lock()
    state = {"running": False, "last_error": None}

    def do_check() -> None:
        if not lock.acquire(blocking=False):
            return  # 已經有檢查在跑
        state["running"] = True
        try:
            cfg, props, watches = load_config(config_path)  # 每次重讀，改 config 不用重開
            run_check(cfg, props, watches, Store(cfg["settings"].get("db_path", "hotel_monitor.db")))
            state["last_error"] = None
        except BaseException as e:  # SystemExit（例如缺 API key）也要接住
            state["last_error"] = str(e)
            print(f"檢查失敗：{e}")
        finally:
            state["running"] = False
            lock.release()

    def auto_loop() -> None:
        while True:
            do_check()
            try:
                interval = load_config(config_path)[0]["settings"].get("interval_minutes", 60)
            except Exception:
                interval = 60
            time.sleep(interval * 60)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: dict, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def do_GET(self) -> None:
            if self.path in ("/", "/index.html"):
                with open(page, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif self.path == "/api/data":
                try:
                    data = dashboard_data(config_path)
                except Exception as e:
                    return self._json({"error": str(e)}, 500)
                self._json({**data, "running": state["running"], "last_error": state["last_error"]})
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:
            if self.path == "/api/check":
                threading.Thread(target=do_check, daemon=True).start()
                self._json({"started": True})
            else:
                self._send(404, b"not found", "text/plain")

        def log_message(self, *args) -> None:  # 不要每個請求都印 log
            pass

    if auto:
        threading.Thread(target=auto_loop, daemon=True).start()
    print(f"網頁已啟動：http://{host}:{port}（Ctrl+C 停止）"
          + ("，並依 interval_minutes 自動檢查" if auto else ""))
    ThreadingHTTPServer((host, port), Handler).serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser(description="訂房資訊監控工具")
    ap.add_argument("-c", "--config", default="config.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("find", help="搜尋房源並取得 token")
    f.add_argument("query", help="飯店名稱，建議加上城市，例如「東京 京王廣場飯店」")
    f.add_argument("--check-in", help="預設用 config 的 [defaults]，沒有則為 30 天後")
    f.add_argument("--check-out")
    sub.add_parser("check", help="檢查一次")
    sub.add_parser("watch", help="持續監控")
    h = sub.add_parser("history", help="查看價格歷史")
    h.add_argument("keyword")
    s = sub.add_parser("serve", help="開啟網頁（價格走勢圖），並自動定時檢查")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--no-auto", action="store_true", help="只開網頁，不自動檢查")
    e = sub.add_parser("export", help="輸出靜態網站（雲端部署用）")
    e.add_argument("--out", default="site")
    args = ap.parse_args()

    with open(args.config, "rb") as fp:
        raw = tomllib.load(fp)
    db_path = raw["settings"].get("db_path", "hotel_monitor.db")

    if args.cmd == "history":
        for row in Store(db_path).history(args.keyword):
            print("  ".join(str(x) for x in row))
        return

    if args.cmd == "find":
        d = dict(raw.get("defaults", {}))
        d.setdefault("check_in", (date.today() + timedelta(days=30)).isoformat())
        d.setdefault("check_out", (date.fromisoformat(d["check_in"]) + timedelta(days=1)).isoformat())
        if args.check_in:
            d["check_in"] = args.check_in
        if args.check_out:
            d["check_out"] = args.check_out
        trip, _ = _split_trip({}, d)
        cmd_find(raw, args.query, trip)
        return

    if args.cmd == "export":
        export_site(args.config, args.out)
        return

    if args.cmd == "serve":
        load_config(args.config)  # 先檢查 config 有沒有錯
        serve(args.config, args.host, args.port, auto=not args.no_auto)
        return

    cfg, props, watches = load_config(args.config)
    store = Store(db_path)
    if args.cmd == "check":
        run_check(cfg, props, watches, store)
    else:
        interval = cfg["settings"].get("interval_minutes", 60)
        while True:
            run_check(cfg, props, watches, store)
            print(f"\n下次檢查：{interval} 分鐘後（Ctrl+C 停止）")
            time.sleep(interval * 60)


if __name__ == "__main__":
    main()
