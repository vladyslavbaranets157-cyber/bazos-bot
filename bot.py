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


def http(url, data=None, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, "Accept-Language": "sk"})
            with urllib.request.urlopen(req, timeout=25) as r:
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
    st, body, _ = http(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data=data)
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
        v["liq"] = "🔥 vysoká" if md <= 5 else "🟡 stredná" if md <= 14 else "🧊 nízka"
    else:
        v["liq"] = "❔ málo dát (zbieram štatistiku)"
    return v


def card(row, v, tag):
    lines = [f"{tag} <b>{esc(row['title'])}</b>",
             f"💶 <b>{row['price']} €</b>  📍 {esc(row['loc'])}" + ("  (okolie BA)" if row["local"] else ""),
             f"🏷 {esc(row['mkey'])}"]
    if row["risky"]:
        lines.append("⚠️ v texte sú slová ako poškodený/na diely/iCloud – skontroluj!")
    if v.get("enough"):
        lines.append(f"📊 medián na Bazoši: {v['median']} € ({v['n_active']} aktívnych) → predaj ~{v['sell']} €")
        lines.append(f"💰 zisk ≈ <b>{v['profit']} €</b> ({int(v['margin'] * 100)} %)")
    else:
        lines.append(f"📊 málo porovnateľných ponúk ({v['n_active']}) – cena nie je overená")
    lines.append(f"⏱ likvidita: {v['liq']}" + (f", predáva sa ~{v['days']:.0f} dní ({v['n_sold']} predajov)" if "days" in v else ""))
    if row["descr"]:
        lines.append(f"📝 {esc(row['descr'][:220])}")
    lines.append(f"🔗 {row['url']}")
    lines.append(f"/ignore_{row['id']}")
    return "\n".join(lines)


def send_card(chat, row, v, tag):
    text = card(row, v, tag)
    if row["img"] and "empty.gif" not in row["img"]:
        r = tg("sendPhoto", chat_id=chat, photo=row["img"], caption=text[:1000], parse_mode="HTML")
        if r and r.get("ok"):
            return
    tg("sendMessage", chat_id=chat, text=text, parse_mode="HTML")


def ignored(c, title):
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
        if ignored(c, row["title"]):
            continue
        if (new or dropped) and (row["notified_price"] is None or dropped):
            if not re.search(r"m[1-4]|air|pro", row["title"], re.I):
                continue
            v = valuation(c, row, minp)
            if chat and not quiet:
                send_card(chat, row, v, "🆕 MacBook BA" if new else "📉 zlacnel")
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
        if row["risky"] or ignored(c, row["title"]):
            continue
        if not ((new or dropped) and (row["notified_price"] is None or dropped)):
            continue
        v = valuation(c, row, minp)
        if not v.get("enough"):
            continue
        tag = None
        if row["price"] <= budget and v["profit"] >= minprofit and v["margin"] >= 0.15:
            tag = "💸 výhodná ponuka"
        elif row["price"] > budget and v["profit"] >= bigprofit and v["margin"] >= 0.2:
            tag = "💎 veľký obchod"
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
    rows = c.execute("""SELECT mkey, COUNT(*) n, AVG(gone_at-first_seen)/86400.0 d, AVG(price) p
                        FROM listings WHERE active=0 AND gone_at>? AND gone_at-first_seen<?
                        GROUP BY mkey HAVING n>=3 ORDER BY d ASC LIMIT ?""",
                     (NOW - 30 * 86400, 28 * 86400, n)).fetchall()
    return rows


def report(c, chat):
    lines = ["☀️ <b>Ranný report</b>", ""]
    tops = top_liquid(c)
    if tops:
        lines.append("🔥 <b>Najlikvidnejšie modely (30 dní):</b>")
        for r in tops:
            lines.append(f"• {esc(r['mkey'])} – ~{r['d']:.1f} dní, {r['n']} predajov, priem. {int(r['p'])} €")
    else:
        lines.append("Zatiaľ málo dát o predajoch – bot sa učí (potrebuje pár dní).")
    lines.append("")
    lines.append(stats_text(c))
    tg("sendMessage", chat_id=chat, text="\n".join(lines), parse_mode="HTML")


def stats_text(c):
    month_start = int(datetime.now(TZ).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
    closed = c.execute("SELECT COALESCE(SUM(sell-buy),0) p, COUNT(*) n FROM trades WHERE sold_at>=?", (month_start,)).fetchone()
    openi = c.execute("SELECT COUNT(*) n, COALESCE(SUM(buy),0) s FROM trades WHERE sold_at IS NULL").fetchone()
    goal = int(cfg(c, "goal"))
    pct = int(closed["p"] / goal * 100) if goal else 0
    return (f"🎯 <b>Cieľ mesiaca:</b> {closed['p']} / {goal} € ({pct} %)\n"
            f"✅ uzavreté obchody: {closed['n']}\n"
            f"📦 na sklade: {openi['n']} ks za {openi['s']} €")


HELP = """<b>Príkazy</b>
/budget 300 – max. nákupná cena
/minprofit 30 – min. očakávaný zisk (€)
/bigprofit 120 – zisk pre „veľké obchody“ nad budgetom
/goal 500 – mesačný cieľ
/bought 280 MacBook Air M1 – zapíš nákup
/sold 350 MacBook Air M1 – zapíš predaj
/stats – pokrok k cieľu
/top – najlikvidnejšie modely
/ignore slovo – ignoruj slovo v názve, /unignore slovo, /ignored
/status – stav bota"""


def handle_command(c, chat, text):
    text = text.strip()
    m = re.match(r"^/(\w+)(?:@\w+)?\s*(.*)$", text, re.S)
    if not m:
        return
    cmd, arg = m.group(1).lower(), m.group(2).strip()

    def reply(s):
        tg("sendMessage", chat_id=chat, text=s, parse_mode="HTML", disable_web_page_preview="true")

    def setnum(key, label):
        if re.fullmatch(r"\d+", arg):
            meta_set(c, "cfg_" + key, arg)
            reply(f"✅ {label}: {arg} €")
        else:
            reply(f"Aktuálne {label}: {cfg(c, key)} €. Zmena: /{cmd} 300")

    if cmd in ("start", "help"):
        reply("👋 Ahoj! Sledujem Bazoš a hľadám výhodné ponuky na prepredaj.\n\n" + HELP)
    elif cmd == "budget":
        setnum("budget", "budget")
    elif cmd == "minprofit":
        setnum("min_profit", "min. zisk")
    elif cmd == "bigprofit":
        setnum("big_profit", "zisk pre veľké obchody")
    elif cmd == "goal":
        setnum("goal", "mesačný cieľ")
    elif cmd in ("bought", "sold"):
        mm = re.match(r"(\d+)\s+(.+)", arg)
        if not mm:
            return reply(f"Použitie: /{cmd} 280 MacBook Air M1")
        price, name = int(mm.group(1)), mm.group(2).strip()
        if cmd == "bought":
            c.execute("INSERT INTO trades(name,buy,bought_at) VALUES(?,?,?)", (name, price, NOW))
            reply(f"📥 Nákup zapísaný: {esc(name)} za {price} €")
        else:
            t = c.execute("SELECT * FROM trades WHERE sold_at IS NULL AND LOWER(name) LIKE ? ORDER BY bought_at LIMIT 1",
                          ("%" + name.lower() + "%",)).fetchone()
            if not t:
                return reply("Nenašiel som taký nákup. Najprv /bought.")
            c.execute("UPDATE trades SET sell=?, sold_at=? WHERE id=?", (price, NOW, t["id"]))
            reply(f"📤 Predaj: {esc(t['name'])} → zisk <b>{price - t['buy']} €</b>\n\n" + stats_text(c))
    elif cmd == "stats":
        reply(stats_text(c))
    elif cmd == "top":
        tops = top_liquid(c, 8)
        reply("\n".join(["🔥 <b>Najlikvidnejšie modely</b>"] + [f"• {esc(r['mkey'])} – ~{r['d']:.1f} dní, {r['n']} predajov, priem. {int(r['p'])} €" for r in tops])
              if tops else "Zatiaľ málo dát.")
    elif cmd == "ignore":
        if arg:
            words = [w for w in cfg(c, "ignore").split(",") if w] + [arg.lower()]
            meta_set(c, "cfg_ignore", ",".join(dict.fromkeys(words)))
            reply(f"🚫 Ignorujem: {esc(arg)}")
    elif cmd == "unignore":
        words = [w for w in cfg(c, "ignore").split(",") if w and w != arg.lower()]
        meta_set(c, "cfg_ignore", ",".join(words))
        reply("✅ Odstránené")
    elif cmd == "ignored":
        reply("🚫 " + (cfg(c, "ignore") or "(nič)"))
    elif cmd.startswith("ignore_") or re.match(r"ignore_\d+", cmd):
        pass
    elif cmd == "status":
        n = c.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        a = c.execute("SELECT COUNT(*) FROM listings WHERE active=1").fetchone()[0]
        g = c.execute("SELECT COUNT(*) FROM listings WHERE active=0").fetchone()[0]
        reply(f"🤖 beží. Inzerátov v DB: {n} (aktívne {a}, zmizli {g}). Budget {cfg(c, 'budget')} €, min. zisk {cfg(c, 'min_profit')} €")
    else:
        reply("Neznámy príkaz.\n\n" + HELP)
    c.commit()


def process_updates(c):
    chat = TG_CHAT_ENV or meta_get(c, "chat_id")
    r = tg("getUpdates", offset=int(meta_get(c, "tg_offset", 0)), timeout=0)
    if r and r.get("ok"):
        for u in r["result"]:
            meta_set(c, "tg_offset", u["update_id"] + 1)
            msg = u.get("message") or {}
            text, cid = msg.get("text", ""), str(msg.get("chat", {}).get("id", ""))
            if not text or not cid:
                continue
            if not chat:  # first /start claims the bot
                if text.startswith("/start"):
                    chat = cid
                    meta_set(c, "chat_id", cid)
                else:
                    continue
            if cid != chat:
                continue
            mm = re.match(r"^/ignore_(\d+)", text)
            if mm:
                row = c.execute("SELECT title FROM listings WHERE id=?", (int(mm.group(1)),)).fetchone()
                c.execute("UPDATE listings SET risky=1 WHERE id=?", (int(mm.group(1)),))
                tg("sendMessage", chat_id=chat, text="🚫 Ignorované" + (": " + esc(row["title"]) if row else ""), parse_mode="HTML")
                continue
            handle_command(c, chat, text)
        c.commit()
    return chat


def main():
    c = db_open()
    chat = process_updates(c)
    if not chat:
        log("no chat yet: send /start to the bot")
    try:
        scan_local_macs(c, chat)
        scan_market_term(c, chat)
        check_gone(c)
        local_now = datetime.now(TZ)
        today = local_now.strftime("%Y-%m-%d")
        if chat and local_now.hour >= 8 and meta_get(c, "last_report") != today:
            report(c, chat)
            meta_set(c, "last_report", today)
        c.commit()
    finally:
        c.close()


if __name__ == "__main__":
    main()
