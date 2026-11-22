#!/usr/bin/env python3
"""Collect NEW prices (cheapest in NAY.sk) for every model the bot has seen, write newprices.json.

Runs on your Mac (NAY blocks GitHub servers). Usage:  python3 update_prices.py
Needs STATE_KEY in .env to read the bot's encrypted state from the `data` branch.
"""
import json, os, subprocess, sys, tempfile, time, statistics, sqlite3, re

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("DB_PATH", os.path.join(tempfile.gettempdir(), "bazos_tmp.db"))
import bot  # noqa: E402


def load_key():
    for line in open(os.path.join(HERE, ".env")):
        if line.startswith("STATE_KEY="):
            return line.split("=", 1)[1].strip()
    sys.exit("STATE_KEY not found in .env")


def fetch_state():
    d = tempfile.mkdtemp()
    remote = subprocess.check_output(["git", "-C", HERE, "remote", "get-url", "origin"], text=True).strip()
    subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", "data", remote, d], check=True)
    out = os.path.join(d, "state.db")
    subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-in", os.path.join(d, "bazos.db.enc"),
                    "-out", out, "-pass", "env:K"], check=True, env={**os.environ, "K": load_key()})
    return out


def main():
    db = sqlite3.connect(fetch_state())
    keys = {}
    for col in ("skey", "mkey"):
        for r in db.execute(f"SELECT {col} k, MIN(mkey) m, COUNT(*) n FROM listings WHERE active=1 AND risky=0 AND {col} IS NOT NULL GROUP BY {col} HAVING n>=3"):
            if not bot.is_generic(r[1]):
                keys[r[0]] = (r[1], r[2])
    path = os.path.join(HERE, "newprices.json")
    try:
        old = json.load(open(path)).get("prices", {})
    except Exception:
        old = {}
    prices, found = dict(old), 0
    print(f"{len(keys)} models to check")
    for k, (m, n) in sorted(keys.items(), key=lambda x: -x[1][1]):
        used = [r[0] for r in db.execute("SELECT price FROM listings WHERE (skey=? OR mkey=?) AND active=1 AND risky=0", (k, k))]
        floor = 0.6 * statistics.median(used) if used else 0
        res = bot.nay_lookup(bot.newprice_query(k), floor=floor)
        if res:
            prices[k] = {"price": res[0], "name": res[1], "url": res[2]}
            found += 1
            print(f"  ✓ {k:32} {res[0]:>5} €  {res[1][:50]}")
        else:
            print(f"  · {k:32} not sold new in NAY")
        time.sleep(1.5)
    json.dump({"updated": time.strftime("%Y-%m-%d"), "source": "nay.sk", "prices": prices}, open(path, "w"), ensure_ascii=False, indent=1)
    print(f"done: {found}/{len(keys)} found, {len(prices)} total saved to newprices.json")


if __name__ == "__main__":
    main()
