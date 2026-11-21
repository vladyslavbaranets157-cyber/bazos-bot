#!/usr/bin/env python3
"""Bazos.sk resale assistant -> Telegram.

One invocation = one cycle (designed for GitHub Actions cron every 5 min):
  1. process Telegram commands
  2. scan local MacBook listings (Bratislava +25 km) -> instant alerts
  3. scan one market term nationwide -> price/liquidity statistics + deal alerts
  4. check whether old listings disappeared (= probably sold)
  5. daily report at 08:00 Bratislava time
State lives in a SQLite file (DB_PATH). Only stdlib is used.
"""
import html as H
import json
import os
import re
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Bratislava")
except Exception:  # pragma: no cover
    TZ = timezone.utc

DB_PATH = os.environ.get("DB_PATH", "data/bazos.db")
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ENV = os.environ.get("TG_CHAT", "")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15"
BASE = "https://www.bazos.sk/search.php"
LOCAL_ZIP, LOCAL_KM = "81101", 25
NOW = int(time.time())

DEFAULTS = {
    "budget": "500",        # max purchase price for normal alerts
    "min_profit": "25",     # min expected profit for normal alerts (EUR)
    "big_profit": "100",    # min profit for "big deal" alerts above budget
    "goal": "500",          # monthly profit goal
    "ignore": "",           # comma separated words
}

RISKY = re.compile(r"poškod|poskod|na diely|pokazen|pokaz|nefunk|icloud|zamknut|blokovan|defekt|rozbit|prasknut|zablokovan|bez dispej|nejde", re.I)
WANTED = re.compile(r"^\s*(kúpim|kupim|hľadám|hladam|zoženiem|zozenem|vykupujem|výkup|vykup)\b", re.I)


# ---------- market terms ----------
def _mac(t):
    t = t.lower()
    chip = re.search(r"\bm([1-4])\b(?:\s*(pro|max|ultra))?", t)
    kind = "Pro" if "pro" in t else "Air" if "air" in t else ""
    size = re.search(r"\b(11|13|14|15|16)\b", t)
    k = "MacBook " + kind
    k += (" M" + chip.group(1)) if chip else (" Intel" if re.search(r"intel|i5|i7|i9|201\d", t) else "")
    return k.strip() + (f" {size.group(1)}\"" if size else "")


def _iphone(t):
    m = re.search(r"iphone\s*(x[rs]?|se|\d{1,2})\s*(pro max|pro|plus|mini|max)?", t.lower())
    g = re.search(r"\b(64|128|256|512|1000|1024)\s*gb", t.lower())
    if not m:
        return "iPhone"
    return "iPhone " + m.group(1).upper() + (" " + m.group(2).title() if m.group(2) else "") + (f" {g.group(1)}GB" if g else "")


def _re_key(label, pat):
    def f(t):
        m = re.search(pat, t.lower())
        return f"{label} {m.group(1).upper() if m else ''}".strip()
    return f


def _fixed(label):
    return lambda t: label


# (search term, min price, key function)
TERMS = [
    ("macbook", 150, _mac),
    ("iphone", 60, _iphone),
    ("ipad", 60, _re_key("iPad", r"(pro|air|mini|\d{1,2})")),
    ("apple watch", 50, _re_key("Apple Watch", r"(ultra|se|series\s*\d+|\b\d\b)")),
    ("airpods", 30, _re_key("AirPods", r"(pro 2|pro|max|\b[234]\b)")),
    ("ps5", 150, lambda t: "PS5 Digital" if "digital" in t.lower() else "PS5"),
    ("xbox series", 120, _re_key("Xbox Series", r"\b([sx])\b")),
    ("nintendo switch", 80, lambda t: "Switch OLED" if "oled" in t.lower() else "Switch Lite" if "lite" in t.lower() else "Switch"),
    ("steam deck", 200, _fixed("Steam Deck")),
    ("rtx", 80, _re_key("GPU RTX", r"rtx\s*(\d{4}\s*(?:ti|super)?)")),
    ("dji", 60, _re_key("DJI", r"(mini\s*\d?|mavic\s*\w*|osmo\s*\w*|air\s*\d|pocket\s*\d)")),
    ("gopro", 60, _re_key("GoPro", r"hero\s*(\d+)")),
    ("samsung galaxy s", 80, _re_key("Galaxy S", r"\bs\s?(\d{2})")),
]


# ---------- tiny helpers ----------
def log(*a):
    print(*a, file=sys.stderr, flush=True)


def http(url, data=None, tries=3, timeout=25):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, "Accept-Language": "sk"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace"), r.geturl()
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return e.code, "", url
            time.sleep(2 * (i + 1))
        except Exception as e:  # noqa
            log("http error", e)
            time.sleep(2 * (i + 1))
    return 0, "", url


def tg(method, **params):
    if not TG_TOKEN:
        log("[no TG_TOKEN] would call", method, json.dumps(params, ensure_ascii=False)[:300])
        return None
    data = urllib.parse.urlencode(params).encode()
    st, body, _ = http(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data=data,
                       timeout=int(params.get("timeout", 0)) + 20)
    try:
        return json.loads(body)
    except Exception:
        log("tg fail", st, body[:200])
        return None


def esc(s):
    return H.escape(str(s), quote=False)


# ---------- db ----------
def db_open():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE IF NOT EXISTS listings(
      id INTEGER PRIMARY KEY, title TEXT, price INTEGER, url TEXT, term TEXT, mkey TEXT,
      loc TEXT, zip TEXT, local INTEGER DEFAULT 0, risky INTEGER DEFAULT 0, img TEXT, descr TEXT,
      views INTEGER, first_seen INTEGER, last_seen INTEGER, active INTEGER DEFAULT 1,
      gone_at INTEGER, price_first INTEGER, notified_price INTEGER);
    CREATE INDEX IF NOT EXISTS ix_key ON listings(mkey, active);
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS trades(
      id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, buy INTEGER, sell INTEGER,
      bought_at INTEGER, sold_at INTEGER);
    """)
    for ddl in ("ALTER TABLE listings ADD COLUMN hidden INTEGER DEFAULT 0",
                "ALTER TABLE trades ADD COLUMN listing_id INTEGER"):
        try:
            c.execute(ddl)
        except sqlite3.OperationalError:
            pass
    c.execute("""CREATE TABLE IF NOT EXISTS saved(
      id INTEGER PRIMARY KEY, saved_at INTEGER, last_price INTEGER, gone_notified INTEGER DEFAULT 0)""")
    return c


def meta_get(c, k, default=None):
    r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def meta_set(c, k, v):
    c.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


def cfg(c, k):
    return meta_get(c, "cfg_" + k, DEFAULTS[k])


# ---------- scraping ----------
def parse_page(page):
    out = []
    for blk in page.split('<div class="inzeraty inzeratyflex">')[1:]:
        m = re.search(r'href="(https://[a-z]+\.bazos\.sk/inzerat/(\d+)/[^"]*)"', blk)
        if not m:
            continue
        url, lid = m.group(1), int(m.group(2))
        t = re.search(r"<h2 class=nadpis><a [^>]*>(.*?)</a>", blk, re.S)
        title = H.unescape(re.sub("<[^>]+>", "", t.group(1))).strip() if t else ""
        d = re.search(r"class=popis>(.*?)</div>", blk, re.S)
        descr = H.unescape(re.sub(r"\s+", " ", re.sub("<[^>]+>", " ", d.group(1)))).strip() if d else ""
        p = re.search(r'class="inzeratycena"><b>(?:<span[^>]*>)?(.*?)</', blk, re.S)
        digits = re.sub(r"[^\d]", "", p.group(1)) if p and "€" in p.group(1) else ""
        price = int(digits) if digits else None
        loc = re.search(r'class="inzeratylok">(.*?)</div>', blk, re.S)
        loc_s = H.unescape(re.sub(r"<br\s*/?>", " ", loc.group(1))).strip() if loc else ""
        zp = re.search(r"\b(\d{3}\s?\d{2})\b", loc_s)
        img = re.search(r'<img src="(https://www\.bazos\.sk/img/[^"]+)"', blk)
        v = re.search(r'class="inzeratyview">\s*(\d+)', blk)
        out.append(dict(id=lid, url=url, title=title, descr=descr[:300], price=price, loc=loc_s,
                        zip=zp.group(1).replace(" ", "") if zp else "", img=img.group(1) if img else "",
                        views=int(v.group(1)) if v else 0))
    return out


def search(term, pages=1, local=False, max_price=None):
    res = []
    for p in range(pages):
        q = {"hledat": term, "cenaod": "", "cenado": str(max_price or "")}
        if local:
            q.update(hlokalita=LOCAL_ZIP, humkreis=str(LOCAL_KM))
        if p:
            q["crz"] = str(p * 20)
        st, page, _ = http(BASE + "?" + urllib.parse.urlencode(q))
        if st != 200:
            break
        items = parse_page(page)
        res += items
        time.sleep(1)
        if len(items) < 20:
            break
    return res


def near_bratislava(zp):
    return zp[:2] in ("81", "82", "83", "84", "85") or zp[:3] in ("900", "901", "902", "903", "931")


# ---------- valuation ----------
def upsert(c, it, term, keyfn, minp, local):
    """Insert/update listing. Returns (row, is_new, price_dropped)."""
    key = keyfn(it["title"])
    risky = 1 if RISKY.search(it["title"] + " " + it["descr"]) else 0
    row = c.execute("SELECT * FROM listings WHERE id=?", (it["id"],)).fetchone()
    loc_flag = 1 if (local or near_bratislava(it["zip"])) else 0
    if row is None:
        c.execute("""INSERT INTO listings(id,title,price,url,term,mkey,loc,zip,local,risky,img,descr,views,
                     first_seen,last_seen,price_first) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (it["id"], it["title"], it["price"], it["url"], term, key, it["loc"], it["zip"], loc_flag, risky,
                   it["img"], it["descr"], it["views"], NOW, NOW, it["price"]))
        return c.execute("SELECT * FROM listings WHERE id=?", (it["id"],)).fetchone(), True, False
    dropped = bool(row["price"] and it["price"] and it["price"] <= row["price"] * 0.9)
    c.execute("UPDATE listings SET price=?, last_seen=?, views=?, active=1, gone_at=NULL, local=MAX(local,?), mkey=?, risky=? WHERE id=?",
              (it["price"], NOW, it["views"], loc_flag, key, risky, it["id"]))
    return c.execute("SELECT * FROM listings WHERE id=?", (it["id"],)).fetchone(), False, dropped


def valuation(c, row, minp):
    comps = [r["price"] for r in c.execute(
        "SELECT price FROM listings WHERE mkey=? AND active=1 AND risky=0 AND price>=? AND id!=?",
        (row["mkey"], minp, row["id"]))]
    gone = [r for r in c.execute(
        "SELECT first_seen, gone_at FROM listings WHERE mkey=? AND active=0 AND gone_at>? AND price>=?",
        (row["mkey"], NOW - 45 * 86400, minp))]
    days = [(g["gone_at"] - g["first_seen"]) / 86400 for g in gone if (g["gone_at"] - g["first_seen"]) < 28 * 86400]
    v = {"n_active": len(comps), "n_sold": len(days), "enough": len(comps) >= 5}
    if v["enough"]:
        v["median"] = int(statistics.median(comps))
        v["sell"] = int(v["median"] * 0.93)  # asking prices are usually negotiated down
        v["profit"] = v["sell"] - (row["price"] or 0)
        v["margin"] = v["profit"] / max(row["price"] or 1, 1)
    if len(days) >= 3:
        md = statistics.median(days)
        v["days"] = md
        v["liq"] = "🔥 высокая" if md <= 5 else "🟡 средняя" if md <= 14 else "🧊 низкая"
    else:
        v["liq"] = "❔ мало данных (бот копит статистику)"
    return v


def card(row, v, tag):
    lines = [f"{tag} <b>{esc(row['title'])}</b>",
             f"💶 <b>{row['price']} €</b>  📍 {esc(row['loc'])}" + ("  (район Братиславы)" if row["local"] else ""),
             f"🏷 Модель: {esc(row['mkey'])}"]
    if row["risky"]:
        lines.append("⚠️ В тексте есть слова вроде «повреждён / на запчасти / iCloud» — проверь внимательно!")
    if v.get("enough"):
        lines.append(f"📊 Медиана на Bazos: {v['median']} € ({v['n_active']} активных) → продажа ≈ {v['sell']} €")
        lines.append(f"💰 Ожидаемая прибыль ≈ <b>{v['profit']} €</b> ({int(v['margin'] * 100)} %)")
    else:
        lines.append(f"📊 Мало похожих объявлений ({v['n_active']}) — цена не проверена")
    lines.append(f"⏱ Ликвидность: {v['liq']}" + (f", продаётся ~{v['days']:.0f} дн. ({v['n_sold']} продаж)" if "days" in v else ""))
    if row["descr"]:
        lines.append(f"📝 {esc(row['descr'][:220])}")
    return "\n".join(lines)


def card_kb(row):
    return json.dumps({"inline_keyboard": [
        [{"text": "🔗 Открыть объявление", "url": row["url"]}],
        [{"text": "✅ Купил", "callback_data": f"buy:{row['id']}"},
         {"text": "⭐ Сохранить", "callback_data": f"save:{row['id']}"}],
        [{"text": "🚫 Не показывать", "callback_data": f"ign:{row['id']}"}]]})


def send_card(chat, row, v, tag):
    text = card(row, v, tag)
    kb = card_kb(row)
    if row["img"] and "empty.gif" not in row["img"]:
        r = tg("sendPhoto", chat_id=chat, photo=row["img"], caption=text[:1000], parse_mode="HTML", reply_markup=kb)
        if r and r.get("ok"):
            return
    tg("sendMessage", chat_id=chat, text=text, parse_mode="HTML", reply_markup=kb)


def ignored(c, row):
    if row["hidden"]:
        return True
    title = row["title"]
    words = [w.strip().lower() for w in cfg(c, "ignore").split(",") if w.strip()]
    t = title.lower()
    return any(w in t for w in words)


# ---------- scanning ----------
def scan_local_macs(c, chat):
    budget = int(cfg(c, "budget"))
    term, minp, keyfn = TERMS[0]
    quiet = meta_get(c, "seed_local") is None  # first pass: just remember what exists
    for it in search("macbook", pages=2, local=True, max_price=budget):
        if it["price"] is None or it["price"] < minp or WANTED.search(it["title"]):
            continue
        row, new, dropped = upsert(c, it, term, keyfn, minp, True)
        if ignored(c, row):
            continue
        if (new or dropped) and (row["notified_price"] is None or dropped):
            if not re.search(r"m[1-4]|air|pro", row["title"], re.I):
                continue
            v = valuation(c, row, minp)
            if chat and not quiet:
                send_card(chat, row, v, "🆕 MacBook в Братиславе" if new else "📉 Цена снижена")
            if chat:
                c.execute("UPDATE listings SET notified_price=? WHERE id=?", (row["price"], row["id"]))
            c.commit()
    if chat:
        meta_set(c, "seed_local", 1)


def scan_market_term(c, chat):
    idx = int(meta_get(c, "term_idx", 0)) % len(TERMS)
    meta_set(c, "term_idx", idx + 1)
    term, minp, keyfn = TERMS[idx]
    budget, minprofit, bigprofit = int(cfg(c, "budget")), int(cfg(c, "min_profit")), int(cfg(c, "big_profit"))
    log("market scan:", term)
    for it in search(term, pages=3, max_price=2500):
        if it["price"] is None or it["price"] < minp or WANTED.search(it["title"]):
            continue
        row, new, dropped = upsert(c, it, term, keyfn, minp, False)
        if row["risky"] or ignored(c, row):
            continue
        if not ((new or dropped) and (row["notified_price"] is None or dropped)):
            continue
        v = valuation(c, row, minp)
        if not v.get("enough"):
            continue
        tag = None
        if row["price"] <= budget and v["profit"] >= minprofit and v["margin"] >= 0.15:
            tag = "💸 Выгодное предложение"
        elif row["price"] > budget and v["profit"] >= bigprofit and v["margin"] >= 0.2:
            tag = "💎 Крупная сделка"
        if tag and chat:
            send_card(chat, row, v, tag)
            c.execute("UPDATE listings SET notified_price=? WHERE id=?", (row["price"], row["id"]))
    c.commit()


def check_gone(c, limit=20):
    rows = c.execute("SELECT id,url FROM listings WHERE active=1 AND last_seen<? ORDER BY last_seen LIMIT ?",
                     (NOW - 86400, limit)).fetchall()
    for r in rows:
        st, body, final = http(r["url"], tries=2)
        if st in (404, 410) or (st == 200 and "/inzerat/" not in final):
            c.execute("UPDATE listings SET active=0, gone_at=? WHERE id=?", (NOW, r["id"]))
        elif st == 200:
            c.execute("UPDATE listings SET last_seen=? WHERE id=?", (NOW, r["id"]))
        time.sleep(0.7)
    c.commit()


# ---------- reports ----------
def top_liquid(c, n=5):
    return c.execute("""SELECT mkey, COUNT(*) n, AVG(gone_at-first_seen)/86400.0 d, AVG(price) p
                        FROM listings WHERE active=0 AND gone_at>? AND gone_at-first_seen<?
                        GROUP BY mkey HAVING n>=3 ORDER BY d ASC LIMIT ?""",
                     (NOW - 30 * 86400, 28 * 86400, n)).fetchall()


def top_text(c, n=8):
    tops = top_liquid(c, n)
    if not tops:
        return "🔥 <b>Топ ликвидных моделей</b>\n\nПока мало данных о продажах — бот копит статистику (нужно несколько дней)."
    return "\n".join(["🔥 <b>Самые ликвидные модели (30 дней)</b>"] + [
        f"• {esc(r['mkey'])} — ~{r['d']:.1f} дн., {r['n']} продаж, ср. цена {int(r['p'])} €" for r in tops])


def stats_text(c):
    month_start = int(datetime.now(TZ).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
    closed = c.execute("SELECT COALESCE(SUM(sell-buy),0) p, COUNT(*) n FROM trades WHERE sold_at>=?", (month_start,)).fetchone()
    openi = c.execute("SELECT COUNT(*) n, COALESCE(SUM(buy),0) s FROM trades WHERE sold_at IS NULL").fetchone()
    goal = int(cfg(c, "goal"))
    pct = int(closed["p"] / goal * 100) if goal else 0
    return (f"🎯 <b>Цель месяца:</b> {closed['p']} / {goal} € ({pct} %)\n"
            f"✅ Закрытых сделок: {closed['n']}\n"
            f"📦 На складе: {openi['n']} шт. на {openi['s']} €")


def report(c, chat):
    tg("sendMessage", chat_id=chat, text="☀️ <b>Утренний отчёт</b>\n\n" + top_text(c, 5) + "\n\n" + stats_text(c),
       parse_mode="HTML", reply_markup=MAIN_KB)


# ---------- UI ----------
MAIN_KB = json.dumps({"keyboard": [["📊 Статистика", "🔥 Топ моделей"], ["💰 Бюджет", "🎯 Цель"],
                                    ["🧾 Мои сделки", "⭐ Избранное"], ["⚙️ Настройки"]], "resize_keyboard": True})
NUM = {  # key: (title, presets, unit)
    "budget": ("💰 Бюджет на покупку", [100, 200, 300, 500, 700, 1000, 1500], "€"),
    "goal": ("🎯 Цель на месяц (прибыль)", [300, 500, 800, 1000, 1500], "€"),
    "min_profit": ("📈 Мин. прибыль для оповещения", [15, 25, 40, 60, 80], "€"),
    "big_profit": ("💎 Мин. прибыль для крупных сделок", [80, 100, 150, 250, 400], "€"),
}


def num_menu(c, key):
    title, presets, unit = NUM[key]
    rows, row = [], []
    for p in presets:
        row.append({"text": f"{p} €", "callback_data": f"set:{key}:{p}"})
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "➖ 50", "callback_data": f"adj:{key}:-50"}, {"text": "➕ 50", "callback_data": f"adj:{key}:50"}])
    rows.append([{"text": "✍️ Своё число", "callback_data": f"ask:{key}"}, {"text": "⬅️ Настройки", "callback_data": "menu:settings"}])
    return f"{title}\nСейчас: <b>{cfg(c, key)} {unit}</b>\nВыбери значение:", json.dumps({"inline_keyboard": rows})


def settings_menu(c):
    ig = cfg(c, "ignore") or "—"
    text = (f"⚙️ <b>Настройки</b>\n\n💰 Бюджет: <b>{cfg(c, 'budget')} €</b>\n📈 Мин. прибыль: <b>{cfg(c, 'min_profit')} €</b>\n"
            f"💎 Крупные сделки от: <b>{cfg(c, 'big_profit')} €</b>\n🎯 Цель месяца: <b>{cfg(c, 'goal')} €</b>\n🚫 Игнор-слова: {esc(ig)}")
    kb = {"inline_keyboard": [
        [{"text": "💰 Бюджет", "callback_data": "menu:budget"}, {"text": "🎯 Цель", "callback_data": "menu:goal"}],
        [{"text": "📈 Мин. прибыль", "callback_data": "menu:min_profit"}, {"text": "💎 Крупные сделки", "callback_data": "menu:big_profit"}],
        [{"text": "🚫 Добавить игнор-слово", "callback_data": "ask:ignore"}, {"text": "🗑 Очистить игнор", "callback_data": "clear:ignore"}],
        [{"text": "ℹ️ Статус бота", "callback_data": "menu:status"}]]}
    return text, json.dumps(kb)


def fmt_day(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m")


def trades_menu(c):
    rows = c.execute("""SELECT * FROM trades ORDER BY (sold_at IS NOT NULL), COALESCE(sold_at, bought_at) DESC LIMIT 15""").fetchall()
    text = "🧾 <b>Мои сделки</b>\n\n" + stats_text(c)
    kb = []
    for r in rows:
        if r["sold_at"]:
            label = f"✅ {r['name'][:26]} (+{r['sell'] - r['buy']} €)"
        else:
            label = f"📦 {r['name'][:26]} ({r['buy']} €)"
        kb.append([{"text": label, "callback_data": f"tr:{r['id']}"}])
    text += ("\n\nНажми на сделку, чтобы продать, изменить или удалить её." if rows
             else "\n\nПока нет сделок. Нажми «✅ Купил» под карточкой объявления.")
    return text, json.dumps({"inline_keyboard": kb}) if kb else None


def trade_detail(c, tid):
    t = c.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone()
    if not t:
        return "Сделка не найдена.", None
    text = f"🧾 <b>{esc(t['name'])}</b>\nКуплено: <b>{t['buy']} €</b> ({fmt_day(t['bought_at'])})"
    if t["sold_at"]:
        text += f"\nПродано: <b>{t['sell']} €</b> ({fmt_day(t['sold_at'])})\n💰 Прибыль: <b>{t['sell'] - t['buy']} €</b>"
        first = [{"text": "✏️ Цена продажи", "callback_data": f"esell:{tid}"}, {"text": "↩️ Вернуть на склад", "callback_data": f"reopen:{tid}"}]
    else:
        text += "\nСтатус: 📦 на складе"
        first = [{"text": "📤 Продал", "callback_data": f"sell:{tid}"}]
    kb = [first,
          [{"text": "✏️ Цена покупки", "callback_data": f"ebuy:{tid}"}, {"text": "✏️ Название", "callback_data": f"ename:{tid}"}],
          [{"text": "🗑 Удалить сделку", "callback_data": f"delq:{tid}"}],
          [{"text": "⬅️ Все сделки", "callback_data": "menu:trades"}]]
    return text, json.dumps({"inline_keyboard": kb})


def favorites_menu(c):
    rows = c.execute("""SELECT s.id, s.last_price, l.title, l.price, l.active FROM saved s
                        JOIN listings l ON l.id=s.id ORDER BY s.saved_at DESC LIMIT 20""").fetchall()
    if not rows:
        return "⭐ <b>Избранное</b>\n\nПусто. Нажми «⭐ Сохранить» под карточкой объявления.", None
    kb = []
    for r in rows:
        mark = "" if r["active"] else "❌ "
        kb.append([{"text": f"{mark}{r['title'][:30]} — {r['price']} €", "callback_data": f"fav:{r['id']}"}])
    return "⭐ <b>Избранное</b>\n\n❌ — объявление исчезло (скорее всего продано). Нажми на объявление, чтобы открыть.", json.dumps({"inline_keyboard": kb})


def fav_detail(c, lid):
    row = c.execute("SELECT * FROM listings WHERE id=?", (lid,)).fetchone()
    s = c.execute("SELECT * FROM saved WHERE id=?", (lid,)).fetchone()
    if not row or not s:
        return "Объявление не найдено.", None
    term = next((t for t in TERMS if t[0] == row["term"]), TERMS[0])
    text = card(row, valuation(c, row, term[1]), "⭐ Избранное")
    if s["last_price"] and row["price"] != s["last_price"]:
        text += f"\n💶 Цена при сохранении: {s['last_price']} € → сейчас {row['price']} €"
    if not row["active"]:
        text += "\n❌ Объявление исчезло — вероятно, продано."
    kb = [[{"text": "🔗 Открыть объявление", "url": row["url"]}],
          [{"text": "✅ Купил", "callback_data": f"buy:{lid}"}, {"text": "🗑 Убрать", "callback_data": f"unsave:{lid}"}],
          [{"text": "⬅️ Избранное", "callback_data": "menu:fav"}]]
    return text, json.dumps({"inline_keyboard": kb})


def notify_saved(c, chat):
    """Alert when a saved listing gets cheaper or disappears."""
    if not chat:
        return
    for s in c.execute("SELECT * FROM saved").fetchall():
        row = c.execute("SELECT * FROM listings WHERE id=?", (s["id"],)).fetchone()
        if not row:
            continue
        kb = json.dumps({"inline_keyboard": [[{"text": "⭐ Открыть в избранном", "callback_data": f"fav:{s['id']}"}]]})
        if not row["active"] and not s["gone_notified"]:
            send(chat, f"❌ Объявление из избранного исчезло (вероятно, продано):\n<b>{esc(row['title'])}</b>", kb)
            c.execute("UPDATE saved SET gone_notified=1 WHERE id=?", (s["id"],))
        elif row["active"] and row["price"] and s["last_price"] and row["price"] < s["last_price"]:
            send(chat, f"📉 Цена в избранном снизилась: <b>{esc(row['title'])}</b>\n{s['last_price']} € → <b>{row['price']} €</b>", kb)
            c.execute("UPDATE saved SET last_price=? WHERE id=?", (row["price"], s["id"]))
    c.commit()


def status_text(c):
    n = c.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    a = c.execute("SELECT COUNT(*) FROM listings WHERE active=1").fetchone()[0]
    g = c.execute("SELECT COUNT(*) FROM listings WHERE active=0").fetchone()[0]
    return f"🤖 Бот работает.\nОбъявлений в базе: {n} (активных {a}, исчезло {g}).\nБюджет {cfg(c, 'budget')} €, мин. прибыль {cfg(c, 'min_profit')} €."


WELCOME = ("👋 Привет! Я слежу за Bazos.sk и ищу выгодные товары для перепродажи.\n\n"
           "• Новые MacBook в Братиславе +25 км приходят сразу.\n"
           "• По всему рынку ищу товары дешевле медианы и считаю прибыль и ликвидность.\n"
           "• Всё управление — кнопками внизу и под карточками.")


def send(chat, text, kb=None):
    p = dict(chat_id=chat, text=text, parse_mode="HTML", disable_web_page_preview="true")
    if kb:
        p["reply_markup"] = kb
    return tg("sendMessage", **p)


def show(chat, msg_id, text, kb=None):
    """Edit the menu message in place if possible, otherwise send a new one."""
    if msg_id:
        p = dict(chat_id=chat, message_id=msg_id, text=text, parse_mode="HTML", disable_web_page_preview="true")
        if kb:
            p["reply_markup"] = kb
        r = tg("editMessageText", **p)
        if r and (r.get("ok") or "not modified" in str(r.get("description", ""))):
            return
    send(chat, text, kb)


def open_menu(c, chat, msg_id, name):
    if name in NUM:
        t, kb = num_menu(c, name)
    elif name == "settings":
        t, kb = settings_menu(c)
    elif name == "trades":
        t, kb = trades_menu(c)
    elif name == "fav":
        t, kb = favorites_menu(c)
    elif name == "status":
        t, kb = status_text(c), None
    else:
        return
    show(chat, msg_id, t, kb)


def set_num(c, key, val):
    meta_set(c, "cfg_" + key, max(0, int(val)))


def on_text(c, chat, text):
    text = text.strip()
    pending = meta_get(c, "await", "")
    # a menu button always cancels a pending text input
    buttons = {"📊 Статистика": "stats", "🔥 Топ моделей": "top", "💰 Бюджет": "budget", "🎯 Цель": "goal",
               "🧾 Мои сделки": "trades", "⭐ Избранное": "fav", "⚙️ Настройки": "settings"}
    if text in buttons:
        meta_set(c, "await", "")
        b = buttons[text]
        if b == "stats":
            send(chat, stats_text(c), MAIN_KB)
        elif b == "top":
            send(chat, top_text(c), MAIN_KB)
        else:
            open_menu(c, chat, None, b)
        return
    if pending:
        meta_set(c, "await", "")
        kind, _, arg = pending.partition(":")
        is_num = re.fullmatch(r"\d{1,6}", text) is not None
        if kind == "num" and is_num:
            set_num(c, arg, text)
            return open_menu(c, chat, None, arg)
        if kind == "sell" and is_num:
            t = c.execute("SELECT * FROM trades WHERE id=?", (int(arg),)).fetchone()
            if t:
                c.execute("UPDATE trades SET sell=?, sold_at=? WHERE id=?", (int(text), NOW, t["id"]))
                return send(chat, f"📤 Продано: {esc(t['name'])}\n💰 Прибыль: <b>{int(text) - t['buy']} €</b>\n\n" + stats_text(c), MAIN_KB)
        if kind in ("ebuy", "esell") and is_num:
            col = "buy" if kind == "ebuy" else "sell"
            c.execute(f"UPDATE trades SET {col}=? WHERE id=?", (int(text), int(arg)))
            t, kb = trade_detail(c, int(arg))
            return send(chat, "✅ Изменено.\n\n" + t, kb)
        if kind == "ename" and text:
            c.execute("UPDATE trades SET name=? WHERE id=?", (text[:60], int(arg)))
            t, kb = trade_detail(c, int(arg))
            return send(chat, "✅ Изменено.\n\n" + t, kb)
        if kind == "ignore" and text:
            words = [w for w in cfg(c, "ignore").split(",") if w] + [text.lower()]
            meta_set(c, "cfg_ignore", ",".join(dict.fromkeys(words)))
            return open_menu(c, chat, None, "settings")
        return send(chat, "Не понял. Введи число (или текст для названия) либо выбери кнопку.", MAIN_KB)
    if text.startswith("/start") or text.startswith("/help") or text.startswith("/menu"):
        meta_set(c, "kb_v", "2")
        return send(chat, WELCOME, MAIN_KB)
    send(chat, "Выбери действие кнопками внизу 👇", MAIN_KB)


def on_callback(c, chat, cq):
    data = cq.get("data", "")
    msg = cq.get("message") or {}
    mid = msg.get("message_id")
    act, _, rest = data.partition(":")
    toast = {"save": "⭐ Сохранено в избранное", "unsave": "Убрано из избранного", "buy": "📥 Покупка записана",
             "ign": "🚫 Скрыто"}.get(act, "")
    tg("answerCallbackQuery", callback_query_id=cq["id"], **({"text": toast} if toast else {}))
    ask = lambda kind, tid, q: (meta_set(c, "await", f"{kind}:{tid}"), send(chat, q))
    if act == "menu":
        open_menu(c, chat, mid, rest)
    elif act == "set":
        key, _, val = rest.partition(":")
        set_num(c, key, val)
        open_menu(c, chat, mid, key)
    elif act == "adj":
        key, _, d = rest.partition(":")
        set_num(c, key, int(cfg(c, key)) + int(d))
        open_menu(c, chat, mid, key)
    elif act == "ask":
        meta_set(c, "await", ("ignore" if rest == "ignore" else "num:" + rest))
        send(chat, "✍️ Напиши слово для игнора (например: «чехол»):" if rest == "ignore" else "✍️ Напиши число в евро:")
    elif act == "clear" and rest == "ignore":
        meta_set(c, "cfg_ignore", "")
        open_menu(c, chat, mid, "settings")
    elif act == "buy":
        r = c.execute("SELECT title, price FROM listings WHERE id=?", (int(rest),)).fetchone()
        if r:
            dup = c.execute("SELECT id FROM trades WHERE listing_id=?", (int(rest),)).fetchone()
            if dup:
                t, kb = trade_detail(c, dup["id"])
                send(chat, "Эта покупка уже записана.\n\n" + t, kb)
            else:
                cur = c.execute("INSERT INTO trades(name,buy,bought_at,listing_id) VALUES(?,?,?,?)",
                                (r["title"][:60], r["price"], NOW, int(rest)))
                kb = json.dumps({"inline_keyboard": [[{"text": "✏️ Изменить / 🗑 Отменить", "callback_data": f"tr:{cur.lastrowid}"}]]})
                send(chat, f"📥 Записал покупку: {esc(r['title'][:60])} за <b>{r['price']} €</b>.\nНажал случайно? Кнопка ниже отменит запись.", kb)
    elif act == "ign":
        c.execute("UPDATE listings SET hidden=1 WHERE id=?", (int(rest),))
    elif act == "save":
        r = c.execute("SELECT price FROM listings WHERE id=?", (int(rest),)).fetchone()
        if r:
            c.execute("INSERT OR IGNORE INTO saved(id,saved_at,last_price) VALUES(?,?,?)", (int(rest), NOW, r["price"]))
    elif act == "unsave":
        c.execute("DELETE FROM saved WHERE id=?", (int(rest),))
        open_menu(c, chat, mid, "fav")
    elif act == "fav":
        t, kb = fav_detail(c, int(rest))
        send(chat, t, kb)
    elif act == "tr":
        t, kb = trade_detail(c, int(rest))
        show(chat, mid, t, kb)
    elif act == "sell":
        t = c.execute("SELECT name,buy FROM trades WHERE id=?", (int(rest),)).fetchone()
        if t:
            ask("sell", rest, f"📤 За сколько продал «{esc(t['name'])}» (куплено за {t['buy']} €)? Напиши число в евро:")
    elif act == "ebuy":
        ask("ebuy", rest, "✏️ Напиши новую цену покупки (число в евро):")
    elif act == "esell":
        ask("esell", rest, "✏️ Напиши новую цену продажи (число в евро):")
    elif act == "ename":
        ask("ename", rest, "✏️ Напиши новое название:")
    elif act == "reopen":
        c.execute("UPDATE trades SET sell=NULL, sold_at=NULL WHERE id=?", (int(rest),))
        t, kb = trade_detail(c, int(rest))
        show(chat, mid, "↩️ Возвращено на склад.\n\n" + t, kb)
    elif act == "delq":
        kb = json.dumps({"inline_keyboard": [[{"text": "🗑 Да, удалить", "callback_data": f"delok:{rest}"},
                                              {"text": "Нет", "callback_data": f"tr:{rest}"}]]})
        show(chat, mid, "Точно удалить эту сделку? Она пропадёт из статистики.", kb)
    elif act == "delok":
        c.execute("DELETE FROM trades WHERE id=?", (int(rest),))
        open_menu(c, chat, mid, "trades")
    c.commit()


def process_updates(c, wait=0):
    chat = TG_CHAT_ENV or meta_get(c, "chat_id")
    r = tg("getUpdates", offset=int(meta_get(c, "tg_offset", 0)), timeout=wait)
    if r and r.get("ok"):
        for u in r["result"]:
            meta_set(c, "tg_offset", u["update_id"] + 1)
            cq = u.get("callback_query")
            msg = u.get("message") or (cq or {}).get("message") or {}
            cid = str(msg.get("chat", {}).get("id", ""))
            text = (u.get("message") or {}).get("text", "")
            if not cid or not (text or cq):
                continue
            if not chat:  # first /start claims the bot
                if text.startswith("/start"):
                    chat = cid
                    meta_set(c, "chat_id", cid)
                else:
                    continue
            if cid != chat:
                continue
            if cq:
                on_callback(c, chat, cq)
            else:
                on_text(c, chat, text)
        c.commit()
    if chat and meta_get(c, "kb_v") != "2":
        meta_set(c, "kb_v", "2")
        send(chat, "🔄 Бот обновлён: добавлены «⭐ Избранное» и редактирование сделок (нажми на сделку в «🧾 Мои сделки») 👇", MAIN_KB)
    return chat


def scan_cycle(c, chat):
    scan_local_macs(c, chat)
    scan_market_term(c, chat)
    check_gone(c)
    notify_saved(c, chat)
    local_now = datetime.now(TZ)
    today = local_now.strftime("%Y-%m-%d")
    if chat and local_now.hour >= 8 and meta_get(c, "last_report") != today:
        report(c, chat)
        meta_set(c, "last_report", today)
    c.commit()


def main():
    global NOW
    run_seconds = int(os.environ.get("RUN_SECONDS", "0") or 0)  # 0 = single pass
    end = time.time() + run_seconds
    c = db_open()
    last_scan = 0.0
    try:
        while True:
            NOW = int(time.time())
            chat = process_updates(c, wait=25 if run_seconds else 0)
            if not chat:
                log("no chat yet: send /start to the bot")
            if time.time() - last_scan >= 300:
                try:
                    scan_cycle(c, chat)
                except Exception as e:  # keep the listener alive
                    log("scan error", repr(e))
                last_scan = time.time()
            c.commit()
            if time.time() >= end:
                break
            if not TG_TOKEN:
                time.sleep(5)
    finally:
        c.close()


if __name__ == "__main__":
    main()
