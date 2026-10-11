#!/usr/bin/env python3
"""
PokéBot SG — Telegram reminders that @tag your group for Pokémon 30th-anniversary drops,
plus a watcher that ONLY tracks the official Pokémon Center Singapore store on Lazada
("Pokémon Store Online Singapore").

Commands
  python pokebot.py run        keep running: reminders + store watcher + Telegram commands
  python pokebot.py once       do one round, then exit (for GitHub Actions / cron)
  python pokebot.py schedule   list every reminder and exactly when it will fire
  python pokebot.py test       send a test message (with tags) to your group
  python pokebot.py chatid     show chat IDs / user IDs from recent messages to the bot
  python pokebot.py check      check the official Lazada store right now and print what was
                               found (sends nothing, changes nothing)

Options
  --dry-run                    print messages instead of sending them
  --now "YYYY-MM-DD HH:MM"     pretend it is this time (Singapore time) — use with --dry-run
  --config PATH                config file (default: config.yaml next to this script)
  --state PATH                 memory file (default: state.json next to this script)
"""
from __future__ import annotations

import argparse
import html
import json
import os
import random
import re
import sys
import time
import traceback
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

try:
    import requests
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("Missing packages. Run:  pip install -r requirements.txt")

HERE = Path(__file__).resolve().parent
OFFICIAL_STORE_URL = "https://www.lazada.sg/shop/pokemon-store-online-singapore/"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
BLOCK_MARKERS = ("rgv587", "punish", "x5sec", "_____tmd_____", "captcha", "slide to verify",
                 "unusual traffic", "baxia")
TZ_LABELS = {"Asia/Singapore": "SGT"}


def log(*parts):
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "|", *parts, flush=True)


# ─────────────────────────────── helpers ────────────────────────────────

def esc(s) -> str:
    return html.escape(str(s), quote=False)


def norm(s) -> str:
    """lower-case, accents removed (Pokémon → pokemon), single spaces."""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()


def slug(s) -> str:
    return re.sub(r"[^a-z0-9]+", "-", norm(s)).strip("-")[:60] or "event"


def parse_price(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(v))
    return float(m.group(0).replace(",", "")) if m else None


def money(p) -> str:
    return f"S${p:,.2f}" if isinstance(p, (int, float)) else "price n/a"


def html_to_text(s: str) -> str:
    s = re.sub(r'<a href="([^"]+)">(.*?)</a>', r"\2 [\1]", s)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s)


def load_dotenv(path: Path):
    """Tiny .env reader: KEY=VALUE per line. Real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ─────────────────────────────── time ───────────────────────────────────

class Clock:
    """Real clock, or a simulated one that starts at --now and keeps ticking."""

    def __init__(self, tz: ZoneInfo, start: datetime | None = None):
        self.tz = tz
        self.shift = (start - datetime.now(tz)) if start else timedelta(0)

    def now(self) -> datetime:
        return datetime.now(self.tz) + self.shift


def parse_when(value, tz: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=tz)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=tz)
    s = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=tz)
        except ValueError:
            pass
    raise ValueError(f'Can\'t read the date/time "{value}". Use "YYYY-MM-DD HH:MM" (24-hour, Singapore time).')


_DUR = re.compile(r"(\d+(?:\.\d+)?)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m)", re.I)


def parse_offset(value) -> timedelta:
    """'1d' '3h' '30m' '1h30m' → that long BEFORE the event. '+9h' → 9 h AFTER it. '0m' → at it."""
    s = str(value).strip().lower()
    after = s.startswith("+")
    s = s.lstrip("+-").strip()
    if s in ("0", "now", "0m", "at"):
        return timedelta(0)
    matches = list(_DUR.finditer(s))
    if not matches or "".join(m.group(0) for m in matches).replace(" ", "") != s.replace(" ", ""):
        raise ValueError(f'Can\'t read reminder offset "{value}". Examples: "1d", "3h", "30m", "1h30m", "+9h".')
    minutes = 0.0
    for m in matches:
        n, unit = float(m.group(1)), m.group(2)[0]
        minutes += n * {"d": 1440, "h": 60, "m": 1}[unit]
    td = timedelta(minutes=minutes)
    return -td if after else td


def fmt_dt(dt: datetime) -> str:
    h = dt.hour % 12 or 12
    label = TZ_LABELS.get(str(dt.tzinfo), dt.tzname() or "")
    return f"{dt:%a} {dt.day} {dt:%b}, {h}:{dt:%M} {'AM' if dt.hour < 12 else 'PM'} {label}".strip()


def fmt_time(dt: datetime) -> str:
    h = dt.hour % 12 or 12
    return f"{h}:{dt:%M} {'AM' if dt.hour < 12 else 'PM'}"


def human_delta(td: timedelta) -> str:
    mins = int(round(abs(td).total_seconds() / 60))
    d, rem = divmod(mins, 1440)
    h, m = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d} day{'s' if d > 1 else ''}")
    if h:
        parts.append(f"{h} h")
    if m and not d:
        parts.append(f"{m} min")
    return " ".join(parts) or "under a minute"


# ─────────────────────────────── telegram ───────────────────────────────

class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token: str | None, chat_id=None, thread_id=None, dry_run=False):
        self.token = (token or "").strip()
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.dry_run = dry_run
        self.http = requests.Session()
        self.username = None

    def _clean(self, s: str) -> str:
        return s.replace(self.token, "***") if self.token else s  # never leak the token into logs

    def call(self, method: str, payload: dict | None = None, timeout: int = 35):
        if not self.token:
            raise TelegramError("No bot token. Set TELEGRAM_BOT_TOKEN in .env (or GitHub secret).")
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        last = "unknown error"
        for attempt in range(3):
            try:
                r = self.http.post(url, json=payload or {}, timeout=timeout)
            except requests.RequestException as e:
                last = self._clean(str(e))
                time.sleep(2 * (attempt + 1))
                continue
            try:
                data = r.json()
            except ValueError:
                last = f"HTTP {r.status_code} (not JSON)"
                time.sleep(2)
                continue
            if data.get("ok"):
                return data["result"]
            params = data.get("parameters") or {}
            if r.status_code == 429 and params.get("retry_after"):
                time.sleep(min(int(params["retry_after"]), 60) + 1)
                continue
            if params.get("migrate_to_chat_id"):
                raise TelegramError(f"This group became a supergroup. Change chat_id to {params['migrate_to_chat_id']}.")
            raise TelegramError(self._clean(f"{method}: {data.get('description')} (HTTP {r.status_code})"))
        raise TelegramError(f"{method} failed after 3 tries: {last}")

    def send(self, text: str, button: tuple[str, str] | None = None, silent=False,
             chat_id=None, thread_id=None) -> int:
        chat = chat_id if chat_id is not None else self.chat_id
        if self.dry_run:
            bar = "─" * 64
            print(f"\n{bar}\n[DRY RUN → chat {chat}{', silent' if silent else ''}]\n{html_to_text(text)}")
            if button:
                print(f"[button] {button[0]} → {button[1]}")
            print(bar)
            return 0
        if not chat:
            raise TelegramError("No chat_id. Send /id@YourBot in your group to get it, then set TELEGRAM_CHAT_ID.")
        payload = {"chat_id": chat, "text": text, "parse_mode": "HTML",
                   "link_preview_options": {"is_disabled": True}, "disable_notification": bool(silent)}
        thread = thread_id if thread_id is not None else (self.thread_id if chat == self.chat_id else None)
        if thread:
            payload["message_thread_id"] = int(thread)
        if button:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": button[0], "url": button[1]}]]}
        return (self.call("sendMessage", payload) or {}).get("message_id", 0)

    def pin(self, message_id: int):
        if self.dry_run or not message_id:
            return
        try:
            self.call("pinChatMessage", {"chat_id": self.chat_id, "message_id": message_id,
                                         "disable_notification": True})
        except TelegramError as e:
            log("Could not pin (make the bot an admin with 'Pin messages' permission):", e)


def render_tags(tags) -> str:
    """'@alice' → @alice ; '123456789:Bob' or {id:123456789, name:Bob} → a mention that pings Bob
    even if he has no @username."""
    out = []
    for t in tags or []:
        if isinstance(t, dict):
            uname, uid = t.get("username"), t.get("id")
            if uname:
                out.append("@" + str(uname).lstrip("@"))
            elif uid:
                out.append(f'<a href="tg://user?id={int(uid)}">{esc(t.get("name") or "📣")}</a>')
            continue
        s = str(t).strip()
        if not s:
            continue
        m = re.fullmatch(r"(\d{5,})(?::(.+))?", s)
        if m:
            out.append(f'<a href="tg://user?id={m.group(1)}">{esc(m.group(2) or "📣")}</a>')
        else:
            out.append("@" + s.lstrip("@"))
    return " ".join(out)


# ─────────────────────────────── lazada watcher ─────────────────────────

def normalize_item(o: dict):
    iid = str(o.get("itemId") or "").strip()
    if not iid.isdigit():
        return None
    name = html.unescape(str(o.get("name") or o.get("title") or "")).strip()
    url = str(o.get("itemUrl") or o.get("productUrl") or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = "https://www.lazada.sg" + url
    if not url.startswith("http"):
        url = f"https://www.lazada.sg/products/pdp-i{iid}.html"
    stock = o.get("inStock")
    if isinstance(stock, str):
        stock = stock.strip().lower() in ("true", "1", "yes")
    elif not isinstance(stock, bool):
        stock = None
    return {
        "id": iid,
        "name": name,
        "url": url.split("?")[0],
        "price": parse_price(o.get("price") or o.get("priceShow") or o.get("salePrice")),
        "in_stock": stock,
        "seller": str(o.get("sellerName") or "").strip(),
        "seller_id": str(o.get("sellerId") or "").strip(),
    }


def extract_items(data) -> list[dict]:
    """Find every product-like object ({itemId, name, ...}) anywhere in Lazada's JSON."""
    found: dict[str, dict] = {}

    def walk(o, depth=0):
        if depth > 14:
            return
        if isinstance(o, dict):
            if "itemId" in o and ("name" in o or "title" in o):
                it = normalize_item(o)
                if it:
                    found.setdefault(it["id"], it)
                return
            for v in o.values():
                walk(v, depth + 1)
        elif isinstance(o, list):
            for v in o:
                walk(v, depth + 1)

    walk(data)
    return list(found.values())


def parse_product_html(text: str) -> dict:
    """Best-effort read of a Lazada product page: stock status, price, title, seller."""
    low = text.lower()
    stocks = [int(x) for x in re.findall(r'"stock"\s*:\s*"?(\d+)', text)]
    if stocks:
        status = "in" if any(s > 0 for s in stocks) else "out"
    elif '"outofstock":true' in low.replace(" ", "") or "out of stock" in low or "sold out" in low \
            or "no longer available" in low:
        status = "out"
    else:
        status = "unknown"
    price = None
    m = re.search(r'"salePrice"\s*:\s*\{[^{}]*?"value"\s*:\s*"?([\d.]+)', text) \
        or re.search(r'property="product:price:amount"\s+content="([\d.]+)"', text)
    if m:
        price = float(m.group(1))
    t = re.search(r'property="og:title"\s+content="([^"]+)"', text)
    s = re.search(r'"sellerName"\s*:\s*"([^"]+)"', text)
    return {"status": status, "price": price,
            "title": html.unescape(t.group(1)) if t else None,
            "seller": html.unescape(s.group(1)) if s else None}


class StoreWatcher:
    """Watches ONLY the official Pokémon Center Singapore store on Lazada."""

    def __init__(self, bot: "Bot"):
        self.bot = bot
        c = bot.cfg.get("lazada") or {}
        self.enabled = bool(c.get("enabled", True))
        store = c.get("store") or {}
        self.store_name = store.get("name", "Pokémon Store Online Singapore")
        self.store_url = store.get("url", OFFICIAL_STORE_URL)
        self.slug = store.get("slug") or self.store_url.rstrip("/").split("/")[-1]
        self.listing_url = store.get("listing_url",
                                     "https://www.lazada.sg/{slug}/?ajax=true&from=wangpu&langFlag=en"
                                     "&page={page}&pageTypeId=2&q={q}")
        self.pages = max(1, int(store.get("pages", 3)))
        self.store_queries = list(store.get("queries", ["30th", "anniversary"]))
        self.seller_names = {norm(n) for n in store.get("seller_names", [self.store_name])}
        self.seller_ids = {str(i) for i in store.get("seller_ids", [])}
        self.searches = list(c.get("searches", []))
        self.match_any = [norm(k) for k in c.get("match_any", ["30th", "anniversary", "celebration"])]
        self.exclude_any = [norm(k) for k in c.get("exclude_any", [])]
        self.alert_new = bool(c.get("alert_on_new_listing", True))
        self.alert_restock = bool(c.get("alert_on_back_in_stock", True))
        self.products = list(c.get("products", []))
        self.interval = max(5, int(c.get("check_every_minutes", 10)))
        self.warn_after = max(1, int(c.get("warn_after_failures", 3)))
        self.http = requests.Session()
        self.http.headers.update({
            "User-Agent": BROWSER_UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-SG,en;q=0.9",
            "Referer": self.store_url,
        })
        self._requests_made = 0

    # ---- state ----
    @property
    def st(self) -> dict:
        s = self.bot.state.setdefault("lazada", {})
        s.setdefault("seen", {})
        s.setdefault("baselined", [])
        s.setdefault("products", {})
        s.setdefault("fail_streak", 0)
        s.setdefault("learned_sellers", [])
        return s

    def is_due(self, now: datetime) -> bool:
        nxt = self.st.get("next_check")
        return not nxt or now >= datetime.fromisoformat(nxt)

    # ---- network ----
    def _get(self, url: str, want_json=True):
        if self._requests_made:
            time.sleep(random.uniform(2.0, 5.0))  # be gentle: a few seconds between requests
        self._requests_made += 1
        try:
            r = self.http.get(url, timeout=25, allow_redirects=True)
        except requests.RequestException as e:
            return None, f"network error: {type(e).__name__}", False
        text = r.text or ""
        blocked = r.status_code in (403, 429, 503) or any(m in text[:30000].lower() for m in BLOCK_MARKERS)
        data = None
        if want_json:
            try:
                data = r.json()
            except ValueError:
                data = None
        return (data if want_json else text), f"HTTP {r.status_code}", blocked

    def store_page_url(self, q: str, page: int = 1) -> str:
        return self.listing_url.format(slug=self.slug, page=page, q=quote_plus(q))

    def fetch(self, verbose=False) -> dict:
        """Returns {source: [items]} for every source that worked, plus diagnostics."""
        results, problems = {}, []
        blocked_any = False

        def one(source: str, url: str) -> bool:
            nonlocal blocked_any
            data, status, blocked = self._get(url)
            items = extract_items(data) if data is not None else []
            if verbose:
                log(f"{source:<30} {status:<10} items={len(items):<3} {'ANTI-BOT PAGE' if blocked and not items else ''}")
            if items:
                results[source] = items
                return True
            problems.append(f"{source}: {status}{' (anti-bot page)' if blocked else ''}")
            blocked_any = blocked_any or blocked
            return False

        # 1) the store's own "All products" pages (official by definition)
        for p in range(1, self.pages + 1):
            if not one(f"store:All-Products:p{p}", self.store_page_url("All-Products", p)):
                if p > 1:
                    problems.pop()  # an empty later page just means we reached the end of the listing
                break
        # 2) searches inside the store, 3) site-wide searches (kept only if the seller is official)
        for q in self.store_queries:
            one(f"store:{q}", self.store_page_url(q))
        for q in self.searches:
            one(f"search:{q}", f"https://www.lazada.sg/catalog/?ajax=true&isFirstRequest=true&page=1&q={quote_plus(q)}")
        return {"results": results, "problems": problems, "blocked": blocked_any}

    # ---- official-store filter ----
    def accepted_sellers(self) -> tuple[set, set]:
        names = set(self.seller_names) | {norm(n) for n in self.st.get("learned_sellers", [])}
        return names, set(self.seller_ids)

    def learn_seller(self, store_items: list[dict]):
        """The store's own listing tells us its exact Lazada seller name/id; remember it."""
        counts: dict[str, int] = {}
        for it in store_items:
            if it["seller"]:
                counts[it["seller"]] = counts.get(it["seller"], 0) + 1
        if counts:
            top = max(counts, key=counts.get)
            if "pokemon" in norm(top) and counts[top] >= max(1, len(store_items) // 2):
                learned = self.st["learned_sellers"]
                if top not in learned:
                    learned.append(top)
                    log(f"Learned official seller name from the store page: {top!r}")
                ids = {it["seller_id"] for it in store_items if it["seller"] == top and it["seller_id"]}
                self.seller_ids |= ids

    def is_official(self, it: dict, source: str) -> bool:
        names, ids = self.accepted_sellers()
        if it["seller_id"] and it["seller_id"] in ids:
            return True
        if it["seller"]:
            return norm(it["seller"]) in names
        return source.startswith("store:")  # no seller info: trust only the store's own pages

    def matches(self, name: str) -> bool:
        n = norm(name)
        if any(k and k in n for k in self.exclude_any):
            return False
        return not self.match_any or any(k and k in n for k in self.match_any)

    # ---- main round ----
    def tick(self, force=False) -> str:
        if not self.enabled:
            return "watcher disabled"
        if not self.bot.tg.chat_id and not self.bot.dry_run:
            return "waiting for TELEGRAM_CHAT_ID (send /id in your group)"
        now = self.bot.clock.now()
        if not force and not self.is_due(now):
            return "not due"
        self._requests_made = 0
        st = self.st
        report = self.fetch()
        results = report["results"]
        store_ok = any(s.startswith("store:All-Products") for s in results)

        if results:
            store_items = [it for s, items in results.items() if s.startswith("store:All-Products") for it in items]
            self.learn_seller(store_items)
            self._process(results)
        if self.products:
            self._check_products()

        st["last_check"] = now.isoformat()
        if store_ok:
            self._on_success(now)
            return f"ok: {sum(len(v) for v in results.values())} listings read"
        if report["blocked"]:
            reason = "Lazada showed an anti-bot check"
        elif report["problems"] and all("network error" in p for p in report["problems"]):
            reason = "couldn't connect to Lazada (internet/network problem)"
        else:
            reason = "the store listing came back empty (Lazada may have changed its page)"
        self._on_failure(now, reason)
        return f"problem: {reason}; " + "; ".join(report["problems"][:3])

    def _process(self, results: dict):
        st, seen = self.st, self.st["seen"]
        baselined = set(st["baselined"])
        now_iso = self.bot.clock.now().isoformat()
        new_items, restocked, baseline_hits = [], [], []
        # sources seen for the first time are recorded silently (no flood of "new" alerts)
        page_key = lambda s: re.sub(r":p\d+$", "", s)  # all All-Products pages share one baseline
        order = sorted(results, key=lambda s: page_key(s) in baselined)
        for source in order:
            key = page_key(source)
            first_time = key not in baselined
            for it in results[source]:
                if not self.is_official(it, source):
                    continue
                prev = seen.get(it["id"])
                hit = self.matches(it["name"])
                if prev is None:
                    seen[it["id"]] = {"name": it["name"], "price": it["price"], "in_stock": it["in_stock"],
                                      "url": it["url"], "first_seen": now_iso}
                    if hit:
                        (baseline_hits if first_time else new_items).append(it)
                    continue
                if prev.get("in_stock") is False and it["in_stock"] is True and hit:
                    restocked.append(it)
                if it["in_stock"] is not None:
                    prev["in_stock"] = it["in_stock"]
                if it["price"] is not None:
                    prev["price"] = it["price"]
            baselined.add(key)
        st["baselined"] = sorted(baselined)

        if baseline_hits and not st.get("announced_watch"):
            st["announced_watch"] = True
            self.bot.notify_items(f"👀 Now watching <b>{esc(self.store_name)}</b> (official Pokémon Center SG on "
                                  f"Lazada). Currently listed 30th-anniversary items:", baseline_hits,
                                  tag=False, silent=True)
        if new_items and self.alert_new:
            self.bot.notify_items("🆕 <b>New on the official Pokémon store (Lazada)</b>", new_items)
        if restocked and self.alert_restock:
            self.bot.notify_items("🔁 <b>Back in stock — official Pokémon store (Lazada)</b>", restocked)

    def _check_products(self):
        pst = self.st["products"]
        for p in self.products:
            url = str(p.get("url", "")).strip()
            if not url:
                continue
            text, status, blocked = self._get(url, want_json=False)
            if text is None:
                continue
            info = parse_product_html(text)
            if info["status"] == "unknown" and blocked:
                log(f"Product check blocked by Lazada: {p.get('name') or url}")
                continue
            names, _ = self.accepted_sellers()
            if info["seller"] and norm(info["seller"]) not in names:
                log(f"Skipping {url}: seller is {info['seller']!r}, not the official store.")
                continue
            prev = pst.get(url, {})
            name = p.get("name") or info["title"] or url
            if info["status"] == "in" and prev.get("status") != "in":
                item = {"name": name, "url": url, "price": info["price"], "in_stock": True}
                self.bot.notify_items("✅ <b>In stock now — official Pokémon store (Lazada)</b>", [item])
            pst[url] = {"status": info["status"], "price": info["price"], "checked": self.bot.clock.now().isoformat()}

    def _on_success(self, now: datetime):
        st = self.st
        if st.get("warned"):
            self.bot.safe_send("✅ The Lazada store watcher is working again.", silent=True)
        st.update(fail_streak=0, warned=False)
        st["next_check"] = (now + timedelta(minutes=self.interval + random.uniform(-1, 1))).isoformat()

    def _on_failure(self, now: datetime, reason: str):
        st = self.st
        st["fail_streak"] = int(st.get("fail_streak", 0)) + 1
        wait = min(self.interval * 2 ** min(st["fail_streak"], 3), 60)
        st["next_check"] = (now + timedelta(minutes=wait)).isoformat()
        log(f"Store watcher problem #{st['fail_streak']}: {reason}. Next try in {wait} min.")
        if st["fail_streak"] >= self.warn_after and not st.get("warned"):
            st["warned"] = True
            self.bot.safe_send(
                f"⚠️ Can't read the official Lazada store right now ({esc(reason)}, {st['fail_streak']} tries in a row). "
                f"Your timed reminders still work. I'll keep retrying every ~{wait} min — check the store manually "
                f"meanwhile.", button=("🛒 Open official store", self.store_url))


# ─────────────────────────────── the bot ────────────────────────────────

class Bot:
    def __init__(self, cfg: dict, state: dict, clock: Clock, dry_run=False):
        self.cfg, self.state, self.clock, self.dry_run = cfg, state, clock, dry_run
        self.tz = clock.tz
        tg = cfg.get("telegram") or {}
        self.tg = Telegram(tg.get("token"), tg.get("chat_id"), tg.get("thread_id"), dry_run=dry_run)
        self.tags_html = render_tags(tg.get("tags"))
        self.pin_live = bool(tg.get("pin_live", False))
        self.store_url = (cfg.get("lazada") or {}).get("store", {}).get("url", OFFICIAL_STORE_URL)
        self.default_remind = cfg.get("default_remind", ["1h", "10m", "0m"])
        self.late_limit = timedelta(minutes=int(cfg.get("late_limit_minutes", 120)))
        self.events = self._load_events(cfg.get("events") or [])
        self.watcher = StoreWatcher(self)

    # ---- events ----
    def _load_events(self, raw: list) -> list[dict]:
        events = []
        for i, ev in enumerate(raw, 1):
            if not isinstance(ev, dict) or not ev.get("name") or not ev.get("when"):
                raise SystemExit(f"Event #{i} in config needs at least a name and a when.")
            if ev.get("enabled", True) is False:
                continue
            try:
                when = parse_when(ev["when"], self.tz)
                fires = {when - parse_offset(r) for r in (ev.get("remind") or self.default_remind)}
                fires |= {parse_when(a, self.tz) for a in (ev.get("remind_at") or [])}
            except ValueError as e:
                raise SystemExit(f'Event "{ev["name"]}": {e}')
            key = (ev.get("id") or slug(ev["name"])) + "@" + when.strftime("%Y%m%d%H%M")
            events.append({**ev, "_when": when, "_fires": sorted(fires), "_key": key})
        return events

    def fire_key(self, ev: dict, fire: datetime) -> str:
        return f"{ev['_key']}|{fire:%Y%m%d%H%M}"

    def tick_reminders(self):
        now = self.clock.now()
        sent = self.state.setdefault("sent", {})
        for ev in self.events:
            due = [f for f in ev["_fires"] if f <= now and self.fire_key(ev, f) not in sent]
            if not due:
                continue
            latest = max(due)  # if several piled up (bot was off), send only the most recent one
            older = [f for f in due if f != latest]
            if now - latest > self.late_limit:
                for f in due:
                    sent[self.fire_key(ev, f)] = "skipped (too late)"
                log(f"Skipped stale reminder(s) for {ev['name']}")
                continue
            try:
                self.send_reminder(ev, now)
            except TelegramError as e:
                log(f"Reminder for {ev['name']} not sent, will retry: {e}")
                continue
            sent[self.fire_key(ev, latest)] = now.isoformat()
            for f in older:
                sent[self.fire_key(ev, f)] = "skipped (superseded)"

    def reminder_text(self, ev: dict, now: datetime, test=False) -> tuple[str, bool]:
        when = ev["_when"]
        delta = when - now
        live = False
        if delta > timedelta(minutes=2):
            head = f"⏰ <b>In {human_delta(delta)}</b>"
        elif delta >= timedelta(minutes=-2):
            head, live = "🔴 <b>HAPPENING NOW</b>", True
        else:
            day = "today" if when.date() == now.date() else f"{when:%a} {when.day} {when:%b}"
            head = f"🟢 <b>Out since {fmt_time(when)} {day}</b> ({human_delta(delta)} ago)"
        lines = []
        if test:
            lines.append("🧪 <i>Test message — this is what a reminder looks like</i>")
        lines += [head, f"<b>{esc(ev['name'])}</b>", f"🗓 {fmt_dt(when)}"]
        if ev.get("note"):
            lines.append(esc(str(ev["note"]).strip()))
        if self.tags_html:
            lines += ["", self.tags_html]
        return "\n".join(lines), live

    def send_reminder(self, ev: dict, now: datetime, test=False):
        text, live = self.reminder_text(ev, now, test=test)
        button = (ev.get("button") or "🛒 Open official Pokémon store", ev.get("link") or self.store_url)
        mid = self.tg.send(text, button=button)
        log(f"Sent reminder: {ev['name']} ({'live' if live else 'heads-up'})")
        if live and (ev.get("pin", self.pin_live)):
            self.tg.pin(mid)

    # ---- store alerts ----
    def notify_items(self, header: str, items: list[dict], tag=True, silent=False):
        lines = [header]
        for it in items[:10]:
            stock = {True: "✅ in stock", False: "❌ out of stock"}.get(it.get("in_stock"), "")
            lines.append(f'• <a href="{esc(it["url"])}">{esc(it["name"][:90])}</a> — {money(it.get("price"))} {stock}'.rstrip())
        if len(items) > 10:
            lines.append(f"…and {len(items) - 10} more")
        if tag and self.tags_html:
            lines += ["", self.tags_html]
        link = items[0]["url"] if len(items) == 1 else self.store_url
        label = "🛒 Open on Lazada" if len(items) == 1 else "🛒 Open official store"
        self.safe_send("\n".join(lines), button=(label, link), silent=silent)

    def safe_send(self, text, button=None, silent=False):
        try:
            self.tg.send(text, button=button, silent=silent)
        except TelegramError as e:
            log("Telegram send failed:", e)

    # ---- telegram commands ----
    def upcoming(self, now: datetime, n=6) -> list[tuple[dict, datetime | None]]:
        sent = self.state.get("sent", {})
        rows = []
        for ev in self.events:
            if ev["_when"] + timedelta(hours=12) < now:
                continue
            nxt = next((f for f in ev["_fires"] if f > now and self.fire_key(ev, f) not in sent), None)
            rows.append((ev, nxt))
        rows.sort(key=lambda r: r[0]["_when"])
        return rows[:n]

    def handle_updates(self, poll_seconds: int):
        if self.tg.username is None:
            self.tg.username = (self.tg.call("getMe") or {}).get("username") or ""
        offset = self.state.get("tg_offset")
        payload = {"timeout": poll_seconds, "allowed_updates": ["message"]}
        if offset:
            payload["offset"] = offset
        updates = self.tg.call("getUpdates", payload, timeout=poll_seconds + 15) or []
        for u in updates:
            self.state["tg_offset"] = u["update_id"] + 1
            try:
                self._handle_message(u.get("message") or {})
            except TelegramError as e:
                log("Couldn't answer a command:", e)

    def _handle_message(self, msg: dict):
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return
        first = text.split()[0][1:]
        cmd, _, target = first.partition("@")
        if target and self.tg.username and target.lower() != self.tg.username.lower():
            return  # command meant for a different bot
        cmd = cmd.lower()
        chat, user = msg.get("chat") or {}, msg.get("from") or {}
        home = str(chat.get("id")) == str(self.tg.chat_id)
        thread = msg.get("message_thread_id") if msg.get("is_topic_message") else None

        def reply(t, button=None):
            self.tg.send(t, button=button, chat_id=chat.get("id"), thread_id=thread)

        if cmd in ("id", "start", "help"):
            uname = f"@{user['username']}" if user.get("username") else "(no @username — use your ID to tag)"
            reply(f"🤖 <b>PokéBot SG</b>\n"
                  f"This chat's ID: <code>{chat.get('id')}</code>\n"
                  f"Your user ID: <code>{user.get('id')}</code> {esc(uname)}\n\n"
                  f"Commands: /next · /status · /test · /check")
            return
        if not home:
            return  # only the configured group may trigger tags and checks
        now = self.clock.now()
        if cmd == "next":
            rows = self.upcoming(now)
            if not rows:
                reply("No upcoming events. Add some to config.yaml.")
                return
            out = ["📅 <b>Upcoming</b>"]
            for ev, nxt in rows:
                nr = f"next ping {fmt_dt(nxt)}" if nxt else "no more pings"
                out.append(f"• <b>{esc(ev['name'])}</b>\n   {fmt_dt(ev['_when'])} — {nr}")
            reply("\n".join(out))
        elif cmd == "test":
            ev = next((e for e, _ in self.upcoming(now)), None) or {
                "name": "Example drop", "_when": now + timedelta(minutes=30), "note": "Just a test."}
            self.send_reminder(ev, now, test=True)
        elif cmd == "status":
            st = self.watcher.st
            hits = sum(1 for v in st["seen"].values() if self.watcher.matches(v.get("name", "")))
            reply(f"🔎 <b>Store watcher</b> ({'on' if self.watcher.enabled else 'off'})\n"
                  f"Store: {esc(self.watcher.store_name)}\n"
                  f"Last check: {esc(st.get('last_check', 'never')[:16].replace('T', ' '))}\n"
                  f"Next check: {esc((st.get('next_check') or 'asap')[:16].replace('T', ' '))}\n"
                  f"Official listings tracked: {len(st['seen'])} ({hits} match your 30th filter)\n"
                  f"Problems in a row: {st.get('fail_streak', 0)}",
                  button=("🛒 Open official store", self.store_url))
        elif cmd == "check":
            last = self.watcher.st.get("last_check")
            if last and now - datetime.fromisoformat(last) < timedelta(minutes=2):
                reply("Checked less than 2 minutes ago — try again shortly.")
                return
            reply("🔎 Checking the official store now…")
            reply(f"Done: {esc(self.watcher.tick(force=True))}")


# ─────────────────────────────── CLI ────────────────────────────────────

def load_config(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"Config not found: {path}")
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    tg = cfg.setdefault("telegram", {}) or {}
    cfg["telegram"] = tg
    if os.getenv("TELEGRAM_BOT_TOKEN"):
        tg["token"] = os.environ["TELEGRAM_BOT_TOKEN"]
    if os.getenv("TELEGRAM_CHAT_ID"):
        tg["chat_id"] = os.environ["TELEGRAM_CHAT_ID"].strip()
    if os.getenv("TELEGRAM_THREAD_ID"):
        tg["thread_id"] = int(os.environ["TELEGRAM_THREAD_ID"])
    if os.getenv("TELEGRAM_TAGS"):
        tg["tags"] = [t.strip() for t in os.environ["TELEGRAM_TAGS"].split(",") if t.strip()]
    return cfg


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (ValueError, OSError):
        log(f"Memory file {path} was unreadable — starting fresh.")
        return {}


def save_state(path: Path, state: dict):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def cmd_schedule(bot: Bot):
    now = bot.clock.now()
    sent = bot.state.get("sent", {})
    print(f"\nNow: {fmt_dt(now)}\n")
    for ev in sorted(bot.events, key=lambda e: e["_when"]):
        print(f"■ {ev['name']}\n  event: {fmt_dt(ev['_when'])}")
        for f in ev["_fires"]:
            k = bot.fire_key(ev, f)
            status = ("sent" if not str(sent[k]).startswith("skipped") else sent[k]) if k in sent else \
                ("due now" if f <= now else "upcoming")
            print(f"    ping {fmt_dt(f):<30} {status}")
        print()


def cmd_chatid(bot: Bot):
    updates = bot.tg.call("getUpdates", {"timeout": 0}) or []
    if not updates:
        print("No recent messages. In your group send:  /id@YourBotName   (then run this again).\n"
              "If the bot is already running somewhere else, it reads the messages first — use /id in the group instead.")
        return
    chats, users = {}, {}
    for u in updates:
        m = u.get("message") or u.get("channel_post") or {}
        c, f = m.get("chat") or {}, m.get("from") or {}
        if c:
            chats[c["id"]] = f"{c.get('type')}: {c.get('title') or c.get('username') or c.get('first_name')}"
        if f:
            users[f["id"]] = f"@{f['username']}" if f.get("username") else f.get("first_name", "")
    print("Chats (use one as TELEGRAM_CHAT_ID):")
    for k, v in chats.items():
        print(f"  {k}   {v}")
    print("People (tag with @username, or ID:Name if they have no username):")
    for k, v in users.items():
        print(f"  {k}   {v}")


def cmd_check(bot: Bot):
    w = bot.watcher
    print(f"Checking {w.store_name} — {w.store_url}\n")
    report = w.fetch(verbose=True)
    store_items = [it for s, its in report["results"].items() if s.startswith("store:All-Products") for it in its]
    w.learn_seller(store_items)
    seen_ids, rows = set(), []
    for source, items in report["results"].items():
        for it in items:
            if it["id"] in seen_ids:
                continue
            seen_ids.add(it["id"])
            rows.append((w.is_official(it, source), w.matches(it["name"]), it))
    official = [r for r in rows if r[0]]
    print(f"\nOfficial-store listings read: {len(official)}  (non-official ignored: {len(rows) - len(official)})")
    for _, hit, it in sorted(official, key=lambda r: not r[1])[:40]:
        stock = {True: "in stock", False: "OUT", None: "?"}[it["in_stock"]]
        print(f"  {'★' if hit else ' '} {money(it['price']):>11}  {stock:<8}  {it['name'][:70]}")
    print("\n★ = matches your 30th-anniversary filter (these are what you'll be tagged for)")
    if not report["results"]:
        print("\nNothing could be read. Problems:\n  " + "\n  ".join(report["problems"][:6]))
        print("If it says anti-bot page, Lazada is blocking automated checks from this network/IP. "
              "Timed reminders still work; try again later or from a home connection.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="PokéBot SG — Telegram reminders for the official Pokémon store on Lazada")
    ap.add_argument("command", choices=["run", "once", "schedule", "test", "chatid", "check"])
    ap.add_argument("--config", default=None)
    ap.add_argument("--state", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--now", default=None, help='simulate a time, e.g. "2026-11-10 23:55"')
    args = ap.parse_args(argv)

    load_dotenv(HERE / ".env")
    load_dotenv(Path.cwd() / ".env")
    cfg = load_config(Path(args.config) if args.config else HERE / "config.yaml")
    tz = ZoneInfo(cfg.get("timezone", "Asia/Singapore"))
    clock = Clock(tz, parse_when(args.now, tz) if args.now else None)
    state_path = Path(args.state) if args.state else HERE / "state.json"
    # a dry run never touches your real memory file unless you point --state somewhere explicitly
    may_save = (not args.dry_run) or bool(args.state)
    state = load_state(state_path)
    bot = Bot(cfg, state, clock, dry_run=args.dry_run)

    def save():
        if may_save:
            save_state(state_path, state)

    if args.command == "schedule":
        cmd_schedule(bot)
        return
    if args.command == "chatid":
        cmd_chatid(bot)
        return
    if args.command == "check":
        cmd_check(bot)
        return
    if args.command == "test":
        now = clock.now()
        ev = next((e for e, _ in bot.upcoming(now)), None) or {
            "name": "Example drop", "_when": now + timedelta(minutes=30), "note": "Just a test."}
        bot.send_reminder(ev, now, test=True)
        print("Test message sent." if not args.dry_run else "")
        return

    if args.command == "once":
        bot.tick_reminders()
        save()
        try:
            log("Store watcher:", bot.watcher.tick())
        except Exception:  # never let the watcher stop reminders
            log("Store watcher crashed:\n" + traceback.format_exc())
        save()
        if not args.dry_run:
            try:
                bot.handle_updates(poll_seconds=0)
            except TelegramError as e:
                log("Couldn't read Telegram commands:", e)
        save()
        return

    # run forever
    if not args.dry_run:
        try:
            me = bot.tg.call("getMe")
        except TelegramError as e:
            raise SystemExit(f"Telegram didn't accept the bot token: {e}")
        bot.tg.username = me.get("username")
        log(f"Logged in as @{bot.tg.username}. Chat: {bot.tg.chat_id or 'NOT SET — send /id in your group'}")
    rows = bot.upcoming(clock.now())
    log(f"{len(bot.events)} events loaded; watcher {'on' if bot.watcher.enabled else 'off'}.")
    for ev, nxt in rows[:3]:
        log(f"  next: {ev['name']} — {fmt_dt(nxt) if nxt else 'no more pings'}")
    log("Running. Press Ctrl+C to stop.")
    while True:
        try:
            bot.tick_reminders()
            save()
            try:
                bot.watcher.tick()
            except Exception:
                log("Store watcher crashed (reminders unaffected):\n" + traceback.format_exc())
            save()
            if args.dry_run:
                time.sleep(20)
            else:
                bot.handle_updates(poll_seconds=20)
                save()
        except KeyboardInterrupt:
            save()
            log("Stopped.")
            return
        except TelegramError as e:
            log("Telegram problem:", e)
            time.sleep(15)
        except Exception:
            log("Unexpected error (will keep running):\n" + traceback.format_exc())
            time.sleep(15)


if __name__ == "__main__":
    try:
        main()
    except TelegramError as e:
        sys.exit(f"Telegram: {e}")
