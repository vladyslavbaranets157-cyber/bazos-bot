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
import subprocess
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

RISKY = re.compile(r"poškod|poskod|na diely|na náhradn|pokazen|pokaz|nefunk|\bicloud\b|\bmdm\b|zamknut|zamknuť|blokovan|defekt|rozbit|prasknut|bez dispej|nejde|nezapn|nezapín|nenabíja|locked", re.I)
# soft red flags: shown as warnings, listing is still considered
SOFT_RED = [
    (re.compile(r"bez nab[ií]ja[čc]k|bez adapt[ée]r|bez k[áa]bl", re.I), "без зарядки/кабеля"),
    (re.compile(r"z[áa]loh|predplat|platb[ua] vopred|preddavok|vopred|len po[šs]tou|iba po[šs]tou|zasielam|kuri[ée]r|bez osobn", re.I),
     "просит предоплату / только доставка — риск мошенничества"),
]
GREEN = [
    (re.compile(r"fakt[úu]r|doklad|[úu]čtenk|pokladni[čc]n|z[áa]ruk", re.I), "есть чек/гарантия"),
    (re.compile(r"krabic|krabic[ou]|origin[áa]lne balen|v balen[ií]|\bbox\b", re.I), "есть коробка"),
    (re.compile(r"prv[ýy] majite|1\.?\s*majite|jeden majite|prv[ée]ho majite|od prv[ée]ho", re.I), "первый владелец"),
]
SEALED = re.compile(r"zape[čc]at|neotvoren|nerozbalen|sealed|nepou[žz]it|[úu]plne nov|nov[ýáé]\s*(?:,|v)?\s*(?:v\s*)?balen|nov[ýáé]\s+nerozbal|\bnov[ýáé]\b\s*[,.]?\s*z[áa]ruk", re.I)
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
    f.base = label
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
    ("steam deck", 200, lambda t: "Steam Deck OLED" if "oled" in t.lower() else "Steam Deck"),
    ("rtx", 80, _re_key("GPU RTX", r"rtx\s*(\d{4}\s*(?:ti|super)?)")),
    ("dji", 60, _re_key("DJI", r"(mini\s*\d?|mavic\s*\w*|osmo\s*\w*|air\s*\d|pocket\s*\d)")),
    ("gopro", 60, _re_key("GoPro", r"hero\s*(\d+)")),
    ("samsung galaxy s", 80, _re_key("Galaxy S", r"\bs\s?(\d{2})")),
    ("mac mini", 150, _re_key("Mac mini", r"(m[1-4]|intel)")),
    ("imac", 200, _re_key("iMac", r"(m[1-4]|\d{2}\")")),
    ("thinkpad", 100, _re_key("ThinkPad", r"(t\d{2,3}\w?|x1 carbon|x\d{3}|p\d{2})")),
    ("radeon rx", 60, _re_key("GPU RX", r"rx\s*(\d{4}\s*(?:xt)?)")),
    ("rog ally", 250, _fixed("ROG Ally")),
    ("meta quest", 100, _re_key("Meta Quest", r"quest\s*(3s|2|3|pro)")),
    ("sony wh-1000xm", 60, _re_key("Sony WH-1000XM", r"xm\s*([3-6])")),
    ("sony alpha", 250, _re_key("Sony Alpha", r"(a7\w*|a6\d{3}|a9\w*|zv-?e?\d+)")),
    ("canon eos", 150, _re_key("Canon EOS", r"(r\d+\w*|\d{2,4}d|rp)")),
    ("garmin", 80, _re_key("Garmin", r"(fenix\s*\d+\w*|forerunner\s*\d+|venu\s*\d*)")),
]
APPLE_TERMS = {"macbook", "iphone", "ipad", "apple watch", "airpods", "mac mini", "imac"}


def specs(term, text):
    """Memory/storage suffix so that 8/256 is compared with 8/256, not with 16/512."""
    if term not in ("macbook", "ipad", "samsung galaxy s", "meta quest", "steam deck"):
        return ""
    t = text.lower()
    ram = sto = None
    pair = re.search(r"\b(4|8|16|24|32|64)\s*(?:gb)?\s*/\s*(128|256|512|1024|1|2)\s*(gb|tb)?\b", t) if term == "macbook" else None
    if pair:
        ram = int(pair.group(1))
        sto = int(pair.group(2)) * (1024 if (pair.group(3) == "tb" or int(pair.group(2)) <= 2) else 1)
        return f" {ram}GB/{sto}GB"
    for n, u in re.findall(r"(\d{1,4})\s*(gb|tb|g)\b", t):
        n = int(n)
        if u == "tb":
            sto = sto or n * 1024
        elif term == "macbook" and n in (4, 8, 16, 24, 32, 64) and ram is None:
            ram = n
        elif n in (32, 64, 128, 256, 512, 1000, 1024, 2048) and sto is None:
            sto = 1024 if n == 1000 else n
    if term == "macbook":
        return (f" {ram}GB" if ram else "") + (f"/{sto}GB" if sto else "")
    return f" {sto}GB" if sto else ""


GENERIC_KEYS = {"MacBook", "MacBook Air", "MacBook Pro", "iPhone"}  # model not recognised -> mixed products


def is_generic(mkey):
    if mkey.startswith("MacBook") and not re.search(r"\bM[1-9]\b|Intel", mkey):
        return True  # MacBook without a chip: M1 and Intel Macs differ by hundreds of euros
    return mkey in GENERIC_KEYS or any(getattr(t[2], "base", None) == mkey for t in TERMS)


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
                "ALTER TABLE trades ADD COLUMN listing_id INTEGER",
                "ALTER TABLE listings ADD COLUMN skey TEXT"):
        try:
            c.execute(ddl)
        except sqlite3.OperationalError:
            pass
    c.execute("""CREATE TABLE IF NOT EXISTS newprice(
      skey TEXT PRIMARY KEY, price INTEGER, name TEXT, url TEXT, fetched_at INTEGER)""")
    c.execute("""CREATE TABLE IF NOT EXISTS saved(
      id INTEGER PRIMARY KEY, saved_at INTEGER, last_price INTEGER, gone_notified INTEGER DEFAULT 0)""")
    if meta_get(c, "skey_v") != "2":
        fns = {t[0]: t[2] for t in TERMS}
        for r in c.execute("SELECT id, term, mkey, title, descr FROM listings").fetchall():
            fn = fns.get(r["term"])
            mk = fn(r["title"]) if fn else r["mkey"]
            c.execute("UPDATE listings SET mkey=?, skey=? WHERE id=?",
                      (mk, mk + specs(r["term"], f"{r['title']} {r['descr']}"), r["id"]))
        meta_set(c, "skey_v", "2")
        c.commit()
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
        dm = re.search(r"\[\s*(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})\s*\]", blk)
        try:
            posted = datetime(int(dm.group(3)), int(dm.group(2)), int(dm.group(1)), tzinfo=TZ).date() if dm else None
        except ValueError:
            posted = None
        out.append(dict(id=lid, url=url, title=title, posted=posted, descr=descr[:300], price=price, loc=loc_s,
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


# ---------- new price (NAY.sk) ----------
ACCESSORY = re.compile(r"^(puzdro|obal|kryt|sklo|ochrann|dr[žz]iak|nab[ií]ja[čc]|k[áa]bel|ta[šs]ka|remienok|adapt[ée]r|dock|stojan|fólia|folia|pr[ií]slu[šs]|n[áa]hradn|bat[ée]ri|hlavov[ýy] p[áa]s|strap|controller|ovl[áa]da[čc]|dot)", re.I)
USED_NEW = re.compile(r"kozmetick|repasovan|pou[žz]it|renovovan|obnoven|bazár|vystaven", re.I)


ACCESSORY_ANY = re.compile(r"ovl[áa]da[čc]|dia[ľl]kov|nab[ií]ja[čc]|k[áa]bel|puzdro|dr[žz]iak|bat[ée]ri|\bkryt|\bobal|ta[šs]ka|remienok|p[áa]sik|filter|stoj[áa]n|adapt[ée]r|z[áa]ruk|roz[šs][ií]ren|care refresh|demo|vezre|v[ýy]stavn", re.I)
# a name with one of these words is a DIFFERENT product unless the query itself has the word
VARIANTS = {"pro", "ti", "fe", "plus", "max", "ultra", "mini", "air", "se", "lite", "oled", "xt", "super", "classic",
            "digital", "neo", "xbox", "x20", "z1", "z2", "extreme", "edge", "flip", "fold", "mavic", "avata", "neo2"}


def norm_tokens(text):
    t = text.lower().replace('"', " ").replace("/", " ").replace("″", " ")
    t = re.sub(r"(\d)\s+(gb|tb)\b", r"\1\2", t)
    return [w for w in re.findall(r"[a-z0-9\u00e0-\u024f]+", t)]


def nay_lookup(query, floor=0):
    """Cheapest NEW price for a model on NAY.sk (plain HTML, product data in data-gtm-data-product)."""
    st, page, _ = http("https://www.nay.sk/vyhladavanie?q=" + urllib.parse.quote_plus(query), tries=2)
    if st != 200:
        return None
    want = set(norm_tokens(query)) - {"gb", "tb"}
    # retailers rarely put RAM in the title: relax it if a storage token is present
    relaxed = {w for w in want if re.fullmatch(r"(4|8|16|24|32|64)gb", w)} if any(re.fullmatch(r"(128|256|512|1024|2048)gb|\dtb", w) for w in want) else set()
    best = None
    for m in re.finditer(r"data-gtm-data-product='(\{.*?\})'", page):
        raw = m.group(1)
        try:
            d = json.loads(raw)
        except Exception:
            try:
                d = json.loads(re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", raw))
            except Exception:
                continue
        name, price = d.get("item_name", ""), d.get("price")
        if not name or not isinstance(price, (int, float)) or price <= 0:
            continue
        if ACCESSORY.search(name) or ACCESSORY_ANY.search(name) or USED_NEW.search(name) or price < floor:
            continue
        have = set(norm_tokens(name))
        if not (want - relaxed) <= have:
            continue
        if (have & VARIANTS) - want:  # e.g. query "RTX 5060" must not match "RTX 5060 Ti"
            continue
        ml = re.search(r'href="(/[^"]+)" tabindex="-1" class="product-box-link-box"', page[m.start():m.start() + 1500])
        if best is None or price < best[0]:
            best = (int(price), name, "https://www.nay.sk" + ml.group(1) if ml else "https://www.nay.sk/")
    return best


def newprice_query(skey):
    q = skey.replace("GB/", "GB ").replace('"', "")
    q = re.sub(r"^PS5\b", "PlayStation 5", q)
    q = re.sub(r"^GPU ", "", q)
    q = re.sub(r"^Galaxy S (\d)", r"Samsung Galaxy S\1", q)
    q = re.sub(r"^Switch\b", "Nintendo Switch", q)
    q = re.sub(r"^GoPro (\d)", r"GoPro Hero \1", q)
    return q.strip()


def load_newprices(c, path=None):
    """Load new-price reference collected locally (update_prices.py) into the DB."""
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "newprices.json")
    try:
        data = json.load(open(path, encoding="utf-8"))
    except Exception:
        return 0
    c.execute("DELETE FROM newprice")
    for k, d in data.get("prices", {}).items():
        c.execute("INSERT OR REPLACE INTO newprice(skey,price,name,url,fetched_at) VALUES(?,?,?,?,?)",
                  (k, d.get("price"), d.get("name"), d.get("url"), NOW))
    c.commit()
    return len(data.get("prices", {}))


def refresh_newprices(c, limit=5):
    """Look up new prices (cached 7 days, misses 3 days) for models that matter: those seen locally or with many comps."""
    rows = c.execute("""SELECT skey, COUNT(*) n, MAX(local) loc FROM listings WHERE active=1 AND skey IS NOT NULL
                        GROUP BY skey HAVING n>=3 OR loc=1 ORDER BY loc DESC, n DESC""").fetchall()
    done = 0
    for r in rows:
        if done >= limit:
            break
        cur = c.execute("SELECT * FROM newprice WHERE skey=?", (r["skey"],)).fetchone()
        ttl = 7 * 86400 if cur and cur["price"] else 3 * 86400
        if cur and cur["fetched_at"] > NOW - ttl:
            continue
        used = [x["price"] for x in c.execute("SELECT price FROM listings WHERE skey=? AND active=1 AND risky=0", (r["skey"],))]
        if len(used) < 3:
            used = [x["price"] for x in c.execute("SELECT price FROM listings WHERE mkey=? AND active=1 AND risky=0", (r["skey"].split(" ")[0],))]
        if len(used) < 3:
            continue
        # a "new price" far below used prices is an accessory, not the product
        res = nay_lookup(newprice_query(r["skey"]), floor=0.6 * statistics.median(used))
        c.execute("INSERT OR REPLACE INTO newprice(skey,price,name,url,fetched_at) VALUES(?,?,?,?,?)",
                  (r["skey"], res[0] if res else None, res[1] if res else None, res[2] if res else None, NOW))
        done += 1
        time.sleep(1.5)
    c.commit()


# ---------- valuation ----------
def upsert(c, it, term, keyfn, minp, local):
    """Insert/update listing. Returns (row, is_new, price_dropped)."""
    key = keyfn(it["title"])
    skey = key + specs(term, f"{it['title']} {it['descr']}")
    risky = 1 if RISKY.search(it["title"] + " " + it["descr"]) else 0
    row = c.execute("SELECT * FROM listings WHERE id=?", (it["id"],)).fetchone()
    loc_flag = 1 if (local or near_bratislava(it["zip"])) else 0
    if row is None:
        c.execute("""INSERT INTO listings(id,title,price,url,term,mkey,loc,zip,local,risky,img,descr,views,
                     first_seen,last_seen,price_first,skey) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (it["id"], it["title"], it["price"], it["url"], term, key, it["loc"], it["zip"], loc_flag, risky,
                   it["img"], it["descr"], it["views"], NOW, NOW, it["price"], skey))
        return c.execute("SELECT * FROM listings WHERE id=?", (it["id"],)).fetchone(), True, False
    dropped = bool(row["price"] and it["price"] and it["price"] <= row["price"] * 0.9)
    c.execute("UPDATE listings SET price=?, last_seen=?, views=?, active=1, gone_at=NULL, local=MAX(local,?), mkey=?, risky=?, skey=? WHERE id=?",
              (it["price"], NOW, it["views"], loc_flag, key, risky, skey, it["id"]))
    return c.execute("SELECT * FROM listings WHERE id=?", (it["id"],)).fetchone(), False, dropped


def valuation(c, row, minp):
    """Price estimate from comparable listings (same model AND memory/storage), cleaned of stale and outlier ads."""
    sql = ("SELECT id,title,descr,url,price,first_seen FROM listings WHERE {col}=? AND active=1 AND risky=0 "
           "AND hidden=0 AND price>=? AND id!=?")
    scope, col, key = "модель + память", "skey", row["skey"]
    comps = c.execute(sql.format(col="skey"), (row["skey"], minp, row["id"])).fetchall() if row["skey"] and row["skey"] != row["mkey"] else []
    if len(comps) < 5:
        scope, col, key = "модель (память не указана)", "mkey", row["mkey"]
        comps = c.execute(sql.format(col="mkey"), (row["mkey"], minp, row["id"])).fetchall()
    comps = [r for r in comps if not SEALED.search(f"{r['title']} {r['descr']}")]  # new/sealed ads are not "used" prices
    fresh = [r for r in comps if r["first_seen"] > NOW - 30 * 86400]  # old unsold ads are overpriced
    if len(fresh) >= 5:
        comps = fresh
    v = {"n_active": len(comps), "enough": False, "scope": scope, "liq": "❔ мало данных (бот копит статистику)"}
    if is_generic(row["mkey"]):
        v["generic"] = True  # title does not say which exact model: comparing would mix different products
        return v
    if len(comps) >= 5:
        med0 = statistics.median(r["price"] for r in comps)
        comps = [r for r in comps if 0.5 * med0 <= r["price"] <= 1.6 * med0]  # drop outliers
    sold = [r for r in c.execute(
        f"SELECT price, first_seen, gone_at FROM listings WHERE {col}=? AND active=0 AND gone_at>? AND price>=?",
        (key, NOW - 45 * 86400, minp)) if (r["gone_at"] - r["first_seen"]) < 28 * 86400]
    days = [(r["gone_at"] - r["first_seen"]) / 86400 for r in sold]
    if len(comps) >= 5:
        prices = sorted(r["price"] for r in comps)
        med = int(statistics.median(prices))
        p30 = prices[int(0.3 * (len(prices) - 1))]
        if len(sold) >= 3:  # prices of ads that actually disappeared are the most realistic
            ref, basis = min(int(statistics.median(r["price"] for r in sold)), med), "по ценам реально исчезнувших объявлений"
        else:
            ref, basis = p30, "по нижним 30% предложений (дешевле всех продаётся быстрее)"
        npr = c.execute("SELECT price,name,url FROM newprice WHERE skey=? AND price IS NOT NULL", (row["skey"],)).fetchone()
        if not npr and row["skey"] != row["mkey"]:
            npr = c.execute("SELECT price,name,url FROM newprice WHERE skey=? AND price IS NOT NULL", (row["mkey"],)).fetchone()
        v.update(enough=True, n_active=len(comps), median=med, lo=prices[0], hi=prices[-1],
                 closest=sorted(comps, key=lambda r: abs(r["price"] - ref))[:3], new=None)
        if npr:
            v["new"] = dict(price=npr["price"], name=npr["name"], url=npr["url"])
            cap = int(npr["price"] * 0.85)  # a used item rarely sells above ~85% of the new price
            if ref > cap:
                ref, basis = cap, f"ограничено 85% от цены нового ({npr['price']} €)"
            v["polluted"] = med > npr["price"] * 0.92  # used median ~ new price: sample likely contaminated
        v.update(ref=ref, basis=basis, sell=int(ref * 0.95))
        v["profit"] = v["sell"] - (row["price"] or 0)
        v["margin"] = v["profit"] / max(row["price"] or 1, 1)
    v["n_sold"] = len(days)
    if len(days) >= 3:
        md = statistics.median(days)
        v["days"] = md
        v["liq"] = "🔥 высокая" if md <= 5 else "🟡 средняя" if md <= 14 else "🧊 низкая"
    else:
        v["liq"] = "❔ мало данных (бот копит статистику)"
    return v


def flags(row, v):
    text = (row["title"] or "") + " " + (row["descr"] or "")
    reds, greens = [], []
    if row["risky"]:
        reds.append("в тексте: повреждён / на запчасти / iCloud / не включается — проверь!")
    if v.get("enough") and row["price"] and row["price"] <= v["median"] * 0.5:
        reds.append("слишком дёшево (−50% и больше от медианы) — часто мошенник или краденое")
    if v.get("polluted"):
        reds.append("медиана б/у почти равна цене нового — выборка могла загрязниться, проверь цену вручную")
    for rx, label in SOFT_RED:
        if rx.search(text):
            reds.append(label)
    for rx, label in GREEN:
        if rx.search(text):
            greens.append(label)
    if len(row["descr"] or "") >= 150:
        greens.append("подробное описание")
    return reds, greens


def card(row, v, tag):
    lines = [f"{tag} <b>{esc(row['title'])}</b>",
             f"💶 <b>{row['price']} €</b>  📍 {esc(row['loc'])}" + ("  (район Братиславы)" if row["local"] else ""),
             f"🏷 Модель: {esc(row['mkey'])}"]
    if v.get("enough"):
        lines.append(f"📊 Сравнил с {v['n_active']} похожими ({v['scope']}): {v['lo']}–{v['hi']} €, медиана {v['median']} €")
        lines.append(f"🎯 Реалистичная цена продажи ≈ <b>{v['sell']} €</b> ({v['basis']}, −5% на торг)")
        if v.get("new"):
            n = v["new"]
            lines.append(f'🆕 Новый: <a href="{n["url"]}">{n["price"]} €</a> ({esc((n["name"] or "")[:55])}) — б/у ≈ {int(v["sell"] / n["price"] * 100)} % от нового')
        else:
            lines.append("🆕 Цена нового товара не найдена — оценка без потолка по новой цене")
        lines.append("🔎 Ближайшие: " + " · ".join(f'<a href="{r["url"]}">{r["price"]} €</a>' for r in v["closest"]))
        lines.append(f"💰 Ожидаемая прибыль ≈ <b>{v['profit']} €</b> ({int(v['margin'] * 100)} %)")
    else:
        lines.append("📊 Точная модель в объявлении не указана — цену по аналогам не оцениваю" if v.get("generic")
                     else f"📊 Мало похожих объявлений ({v['n_active']}) — цена не проверена")
    lines.append(f"⏱ Ликвидность: {v['liq']}" + (f", продаётся ~{v['days']:.0f} дн. ({v['n_sold']} продаж)" if "days" in v else ""))
    reds, greens = flags(row, v)
    for r in reds:
        lines.append(f"🚩 {r}")
    if greens:
        lines.append("✅ " + ", ".join(greens))
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
def relevant(term, title):
    """Bazos search is fuzzy: keep only ads whose title contains every word of the search term."""
    t = title.lower().replace("  ", " ")
    return all(w in t for w in term.split())


def is_fresh(it):
    """Bazos shows only the date: accept ads posted today or yesterday."""
    if not it.get("posted"):
        return True
    return (datetime.now(TZ).date() - it["posted"]).days <= 1


def alert_tag(row, v, budget, minprofit, bigprofit):
    if v.get("enough"):
        if row["price"] <= budget and v["profit"] >= minprofit and v["margin"] >= 0.15:
            return "💸 Свежее предложение"
        if row["price"] > budget and v["profit"] >= bigprofit and v["margin"] >= 0.2:
            return "💎 Крупная сделка"
        return None
    if row["term"] in APPLE_TERMS and row["price"] <= budget:
        return "🍎 Свежее (цена пока не проверена)"
    return None


def scan_local_all(c, chat, max_alerts=8):
    """Newest ads in Bratislava +25 km for every tech term -> instant Telegram alerts (only fresh ones)."""
    budget, minprofit, bigprofit = int(cfg(c, "budget")), int(cfg(c, "min_profit")), int(cfg(c, "big_profit"))
    sent = 0
    for term, minp, keyfn in TERMS:
        quiet = meta_get(c, "seed_" + term) is None  # first pass per term: only remember what exists
        for it in search(term, pages=1, local=True, max_price=2500):
            if it["price"] is None or it["price"] < minp or WANTED.search(it["title"]) or not relevant(term, it["title"]):
                continue
            row, new, dropped = upsert(c, it, term, keyfn, minp, True)
            if row["risky"] or ignored(c, row):
                continue
            if not ((new and is_fresh(it)) or dropped) or (row["notified_price"] is not None and not dropped):
                continue
            v = valuation(c, row, minp)
            tag = alert_tag(row, v, budget, minprofit, bigprofit) if not v.get("polluted") else None
            if not tag:
                continue
            if dropped and not new:
                tag = "📉 Цена снижена · " + tag
            if chat and not quiet and sent < max_alerts:
                send_card(chat, row, v, tag)
                sent += 1
            if chat:
                c.execute("UPDATE listings SET notified_price=? WHERE id=?", (row["price"], row["id"]))
        if chat:
            meta_set(c, "seed_" + term, 1)
        c.commit()


def scan_market_learn(c, terms_per_cycle=2):
    """Nationwide background scan: only collects prices/liquidity statistics, never sends alerts."""
    for _ in range(terms_per_cycle):
        idx = int(meta_get(c, "term_idx", 0)) % len(TERMS)
        meta_set(c, "term_idx", idx + 1)
        term, minp, keyfn = TERMS[idx]
        log("market learn:", term)
        for it in search(term, pages=3, max_price=2500):
            if it["price"] is None or it["price"] < minp or WANTED.search(it["title"]) or not relevant(term, it["title"]):
                continue
            upsert(c, it, term, keyfn, minp, False)
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
    t, kb = top10_text(c, NOW - 86400, "Топ-10 за сутки (Братислава)")
    send(chat, t, kb)


# ---------- Bratislava top deals ----------
LIQ_W = {"🔥": 1.0, "🟡": 0.8, "🧊": 0.6}


def best_local(c, since=None, n=10):
    """Rank active Bratislava listings seen so far by expected profit. Returns (items, checked_total, skipped)."""
    q = "SELECT * FROM listings WHERE local=1"
    args = []
    if since:
        q += " AND first_seen>=?"
        args.append(since)
    rows = c.execute(q, args).fetchall()
    items, skipped = [], 0
    for row in rows:
        if not row["active"] or row["hidden"] or not row["price"]:
            continue
        term = next((t for t in TERMS if t[0] == row["term"]), None)
        if not term or row["risky"] or WANTED.search(row["title"]) or ignored(c, row):
            skipped += 1
            continue
        v = valuation(c, row, term[1])
        if not v.get("enough") or v["profit"] < 10 or v["margin"] < 0.1 or v.get("polluted"):
            continue
        reds, greens = flags(row, v)
        if row["price"] <= v["median"] * 0.5:  # suspiciously cheap: not ranked
            skipped += 1
            continue
        w = LIQ_W.get(v["liq"][:1], 0.8)
        score = v["profit"] * w - 15 * len(reds) + 5 * len(greens)
        items.append((score, row, v, reds, greens))
    items.sort(key=lambda x: -x[0])
    return items[:n], len(rows), skipped


def top10_text(c, since, title):
    items, checked, skipped = best_local(c, since)
    head = f"🏆 <b>{title}</b>\nПроверено объявлений в Братиславе: {checked}" + (f" (отсеяно подозрительных: {skipped})" if skipped else "")
    if not items:
        return head + "\n\nПока нет объявлений с понятной прибылью. Нужно больше данных — бот копит статистику.", None
    lines, kb, row_btns = [head, ""], [], []
    for i, (score, row, v, reds, greens) in enumerate(items, 1):
        lines.append(f"<b>{i}. <a href=\"{row['url']}\">{esc(row['title'][:60])}</a></b>")
        lines.append(f"   💶 {row['price']} € → продажа ≈ {v['sell']} € · 💰 <b>+{v['profit']} €</b> ({int(v['margin'] * 100)} %) · {v['liq'].split(' (')[0]}")
        if reds:
            lines.append("   🚩 " + "; ".join(reds)[:140])
        if greens:
            lines.append("   ✅ " + ", ".join(greens))
        row_btns.append({"text": f"⭐ {i}", "callback_data": f"save:{row['id']}"})
        if len(row_btns) == 5:
            kb.append(row_btns)
            row_btns = []
    if row_btns:
        kb.append(row_btns)
    lines.append("\n⭐ N — сохранить в избранное. Цена продажи — по похожим объявлениям (модель + память), без устаревших и выбросов, −5% на торг.")
    return "\n".join(lines)[:4000], json.dumps({"inline_keyboard": kb})


def top10_menu():
    return ("🏆 <b>Топ-10 Братислава</b>\nВыбери период:",
            json.dumps({"inline_keyboard": [[{"text": "📅 За сутки", "callback_data": "top10:day"},
                                             {"text": "🗂 За всё время", "callback_data": "top10:all"}]]}))


# ---------- UI ----------
MAIN_KB = json.dumps({"keyboard": [["📊 Статистика", "🔥 Топ моделей"], ["🏆 Топ-10 Братислава"],
                                    ["💰 Бюджет", "🎯 Цель"], ["🧾 Мои сделки", "⭐ Избранное"], ["⚙️ Настройки"]],
                      "resize_keyboard": True})
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


WELCOME = ("👋 Привет! Я слежу за Bazos.sk круглосуточно.\n\n"
           "🧠 В фоне изучаю цены и ликвидность всей техники по Словакии (Apple, консоли, видеокарты, камеры, ноутбуки и др.).\n"
           "📩 Тебе присылаю только свежие выгодные предложения из Братиславы и +25 км, с расчётом прибыли и флагами риска.\n"
           "🏆 Кнопка «Топ-10 Братислава» покажет лучшее из всего, что я уже видел.")


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
               "🧾 Мои сделки": "trades", "⭐ Избранное": "fav", "⚙️ Настройки": "settings", "🏆 Топ-10 Братислава": "top10"}
    if text in buttons:
        meta_set(c, "await", "")
        b = buttons[text]
        if b == "stats":
            send(chat, stats_text(c), MAIN_KB)
        elif b == "top":
            send(chat, top_text(c), MAIN_KB)
        elif b == "top10":
            t, kb = top10_menu()
            send(chat, t, kb)
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
        meta_set(c, "kb_v", "4")
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
    elif act == "top10":
        t, kb = top10_text(c, NOW - 86400 if rest == "day" else None,
                           "Топ-10 за сутки (Братислава)" if rest == "day" else "Топ-10 за всё время (Братислава)")
        send(chat, t, kb)
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
    if chat and meta_get(c, "kb_v") != "4":
        meta_set(c, "kb_v", "4")
        send(chat, "🔄 Бот обновлён: теперь он учится на всём рынке Bazos в фоне, а тебе шлёт только свежие предложения из Братиславы +25 км. Добавлено ещё 10 категорий техники 👇", MAIN_KB)
    return chat


# ---------- public dashboard (GitHub Pages; market data only, no trades/chat) ----------
def model_rows(c, limit=60):
    out = []
    for r in c.execute("SELECT skey, COUNT(*) n FROM listings WHERE active=1 AND risky=0 AND hidden=0 AND skey IS NOT NULL GROUP BY skey HAVING n>=5 ORDER BY n DESC LIMIT 150"):
        prices = [x["price"] for x in c.execute("SELECT price,title,descr FROM listings WHERE skey=? AND active=1 AND risky=0 AND price>0", (r["skey"],))
                  if not SEALED.search(f"{x['title']} {x['descr']}")]
        if len(prices) < 5:
            continue
        med = int(statistics.median(prices))
        sold = c.execute("SELECT first_seen, gone_at FROM listings WHERE skey=? AND active=0 AND gone_at>? AND gone_at-first_seen<?",
                         (r["skey"], NOW - 45 * 86400, 28 * 86400)).fetchall()
        days = statistics.median([(x["gone_at"] - x["first_seen"]) / 86400 for x in sold]) if len(sold) >= 3 else None
        npr = c.execute("SELECT price,url FROM newprice WHERE skey=? AND price IS NOT NULL", (r["skey"],)).fetchone()
        out.append(dict(key=r["skey"], n=len(prices), median=med, new=npr["price"] if npr else None, url=npr["url"] if npr else None,
                        days=days, sold=len(sold)))
    out.sort(key=lambda x: (x["days"] is None, x["days"] or 0, -x["n"]))
    return out[:limit]


def render_dashboard(c):
    now = datetime.now(TZ)
    tot = c.execute("SELECT COUNT(*) n, SUM(active) a, SUM(CASE WHEN local=1 AND active=1 THEN 1 ELSE 0 END) l FROM listings").fetchone()
    gone = c.execute("SELECT COUNT(*) FROM listings WHERE active=0").fetchone()[0]
    newp = c.execute("SELECT COUNT(*) FROM newprice WHERE price IS NOT NULL").fetchone()[0]

    def top_rows(since):
        items, checked, skipped = best_local(c, since)
        rows = ""
        for i, (score, row, v, reds, greens) in enumerate(items, 1):
            flags_s = "".join(f'<span class="f r">🚩 {esc(x[:60])}</span>' for x in reds) + "".join(f'<span class="f g">✅ {esc(x)}</span>' for x in greens)
            new = f'{v["new"]["price"]} €' if v.get("new") else "—"
            rows += (f'<tr><td>{i}</td><td><a href="{esc(row["url"])}" target="_blank" rel="noopener">{esc(row["title"][:70])}</a>'
                     f'<div class="sub">{esc(row["loc"])} · {esc(row["mkey"])}</div>{flags_s}</td>'
                     f'<td class="n">{row["price"]} €</td><td class="n">{v["sell"]} €</td><td class="n"><b>+{v["profit"]} €</b></td>'
                     f'<td class="n">{new}</td><td>{esc(v["liq"].split(" (")[0])}</td></tr>')
        return rows or '<tr><td colspan="7" class="sub">Пока нет объявлений с понятной прибылью — бот копит статистику.</td></tr>', checked

    day_rows, day_checked = top_rows(NOW - 86400)
    all_rows, all_checked = top_rows(None)
    mrows = ""
    for m in model_rows(c):
        liq = "—" if m["days"] is None else f'{m["days"]:.1f} дн. ({m["sold"]})'
        new = f'<a href="{esc(m["url"])}" target="_blank" rel="noopener">{m["new"]} €</a>' if m["new"] else "—"
        mrows += f'<tr><td>{esc(m["key"])}</td><td class="n">{m["n"]}</td><td class="n">{m["median"]} €</td><td class="n">{new}</td><td class="n">{liq}</td></tr>'
    head = "<tr><th>#</th><th>Объявление</th><th>Цена</th><th>Продажа ≈</th><th>Прибыль</th><th>Новый</th><th>Ликвидность</th></tr>"
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="120"><title>Bazos Radar</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--tx:#1b1f24;--mut:#6b7280;--bd:#e5e7eb;--ac:#2563eb;--g:#047857;--r:#b91c1c}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f1216;--card:#171b21;--tx:#e8eaed;--mut:#9aa3ad;--bd:#262c34;--ac:#6ea0ff;--g:#34d399;--r:#f87171}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--tx);font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}}
main{{max-width:1100px;margin:0 auto;padding:16px}}h1{{font-size:22px;margin:8px 0}}h2{{font-size:17px;margin:26px 0 8px}}
.kpi{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:12px 0}}
.k{{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:12px}}.k b{{font-size:22px;display:block}}.k span{{color:var(--mut);font-size:13px}}
.card{{background:var(--card);border:1px solid var(--bd);border-radius:12px;overflow-x:auto}}table{{border-collapse:collapse;width:100%;min-width:640px}}
th,td{{padding:9px 10px;border-bottom:1px solid var(--bd);text-align:left;vertical-align:top}}th{{font-size:12px;color:var(--mut);font-weight:600}}
td.n{{text-align:right;white-space:nowrap}}a{{color:var(--ac);text-decoration:none}}.sub{{color:var(--mut);font-size:12px}}
.f{{display:block;font-size:12px}}.f.r{{color:var(--r)}}.f.g{{color:var(--g)}}p.note{{color:var(--mut);font-size:13px}}
</style></head><body><main>
<h1>📡 Bazos Radar</h1>
<p class="sub">Обновлено: {now.strftime("%d.%m.%Y %H:%M")} (Братислава) · страница обновляется сама каждые 2 минуты, данные — примерно раз в 10 минут</p>
<div class="kpi"><div class="k"><b>{tot["n"] or 0}</b><span>объявлений изучено</span></div><div class="k"><b>{tot["a"] or 0}</b><span>активных сейчас</span></div>
<div class="k"><b>{tot["l"] or 0}</b><span>активных в Братиславе +25 км</span></div><div class="k"><b>{gone}</b><span>исчезли (≈ проданы)</span></div>
<div class="k"><b>{newp}</b><span>моделей с ценой нового</span></div></div>
<h2>🏆 Топ-10 Братислава за сутки <span class="sub">(проверено {day_checked})</span></h2><div class="card"><table>{head}{day_rows}</table></div>
<h2>🗂 Топ-10 Братислава за всё время <span class="sub">(проверено {all_checked})</span></h2><div class="card"><table>{head}{all_rows}</table></div>
<h2>📊 Модели: цены и ликвидность по всему Bazos</h2>
<div class="card"><table><tr><th>Модель</th><th>Объявл.</th><th>Медиана б/у</th><th>Новый (NAY.sk)</th><th>Продаётся за</th></tr>{mrows}</table></div>
<p class="note">«Продажа ≈» — по похожим объявлениям (модель + память), без устаревших, запечатанных и выбросов, с потолком 85% от цены нового и −5% на торг. Ликвидность — по срокам исчезновения объявлений. Оценки справочные, не гарантия прибыли.</p>
</main></body></html>"""


def publish_site(c):
    url, d = os.environ.get("SITE_PUSH_URL", ""), os.environ.get("SITE_DIR", "site")
    if not url:
        return
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
        f.write(render_dashboard(c))
    run = lambda *a: subprocess.run(a, cwd=d, check=True, capture_output=True, text=True)
    try:
        subprocess.run(["rm", "-rf", os.path.join(d, ".git")], check=True)
        run("git", "init", "-q", "-b", "gh-pages")
        run("git", "config", "user.name", "bazos-bot")
        run("git", "config", "user.email", "bot@users.noreply.github.com")
        run("git", "add", "-A")
        run("git", "commit", "-q", "-m", "dashboard")
        run("git", "push", "-q", "-f", url, "gh-pages:gh-pages")
        meta_set(c, "site_pub", int(time.time()))
    except subprocess.CalledProcessError as e:
        log("publish_site failed:", (e.stderr or "")[:200].replace(url, "***"))


def scan_cycle(c, chat):
    scan_local_all(c, chat)
    scan_market_learn(c)
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
    log("new prices loaded:", load_newprices(c))
    last_scan = 0.0
    last_site = 0.0
    try:
        while True:
            NOW = int(time.time())
            chat = process_updates(c, wait=25 if run_seconds else 0)
            if not chat:
                log("no chat yet: send /start to the bot")
            if time.time() - last_scan >= 180:
                try:
                    scan_cycle(c, chat)
                except Exception as e:  # keep the listener alive
                    log("scan error", repr(e))
                last_scan = time.time()
            c.commit()
            if os.environ.get("SITE_PUSH_URL") and (time.time() - last_site >= 600 or time.time() >= end):
                try:
                    publish_site(c)
                except Exception as e:
                    log("site error", repr(e))
                last_site = time.time()
                c.commit()
            if time.time() >= end:
                break
            if not TG_TOKEN:
                time.sleep(5)
    finally:
        c.close()


if __name__ == "__main__":
    main()
