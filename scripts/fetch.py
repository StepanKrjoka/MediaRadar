#!/usr/bin/env python3
"""MediaRadar: збирає новини з RSS (або Google News, якщо RSS немає),
перекладає заголовки, розкладає по вкладках і зберігає data/*.json для сторінки."""

import hashlib
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus

import feedparser
import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = Path(os.environ.get("RADAR_CONFIG", ROOT / "config" / "sources.yaml"))
DATA = ROOT / "data"
NEWS, STATUS, TRCACHE, STATE = (DATA / n for n in ("news.json", "status.json", "translations.json", "state.json"))

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
TIMEOUT = 15
GN_EVERY_MIN = 30            # як часто питати Google News для одного видання (захист від блокування)
GN_LOCALE = {"en": ("en-US", "US", "US:en"), "uk": ("uk", "UA", "UA:uk"), "de": ("de", "DE", "DE:de"),
             "fr": ("fr", "FR", "FR:fr"), "it": ("it", "IT", "IT:it"), "pl": ("pl", "PL", "PL:pl"),
             "hu": ("hu", "HU", "HU:hu"), "sk": ("sk", "SK", "SK:sk"), "ro": ("ro", "RO", "RO:ro")}
TABS = [
    {"id": "ukraine", "title": "Усі новини Україна"},
    {"id": "important", "title": "Важливе"},
    {"id": "google", "title": "В тренді Google"},
    {"id": "europe", "title": "Європа"},
    {"id": "world", "title": "Світ"},
]
RU_CHARS = re.compile(r"[ыэъё]", re.I)


# ---------- допоміжне ----------

def now():
    return datetime.now(timezone.utc)


def iso(d):
    return d.isoformat(timespec="seconds")


def load_json(p, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def clean(s):
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def kw_rx(words):
    """«нафт*» → корінь, «oil» → ціле слово, фрази дозволені."""
    parts = []
    for w in words or []:
        w = str(w).lower().strip()
        if not w:
            continue
        stem = w.endswith("*")
        body = re.escape(w.rstrip("*")).replace(r"\*", r"\w*").replace(r"\ ", r"\s+")
        parts.append(r"(?<!\w)" + body + (r"\w*" if stem else r"(?!\w)"))
    return re.compile("|".join(parts), re.I) if parts else None


def norm_title(t):
    t = re.sub(r"[^\w\s]", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()[:90]


def entry_time(e):
    for k in ("published_parsed", "updated_parsed"):
        t = e.get(k)
        if t:
            try:
                return datetime.fromtimestamp(time.mktime(t), tz=timezone.utc)
            except Exception:
                pass
    return None


def http_get(url):
    if not url.startswith(("http://", "https://")):      # локальний файл (для тестів)
        return Path(url).read_bytes()
    r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "uk,en;q=0.8"}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.content


def gn_url(query, lang):
    hl, gl, ceid = GN_LOCALE.get(lang, GN_LOCALE["en"])
    return f"https://news.google.com/rss/search?q={quote_plus(query)}&hl={hl}&gl={gl}&ceid={ceid}"


def strip_publisher(title, entry):
    src = entry.get("source")
    pub = src.get("title") if isinstance(src, dict) else None
    if pub and title.endswith(" - " + pub):
        return title[: -len(pub) - 3].strip(), pub
    if " - " in title:
        t, p = title.rsplit(" - ", 1)
        return t.strip(), pub or p
    return title, pub


# ---------- збір ----------

def fetch_feed(job):
    feed, cfg, state = job
    started = time.time()
    errors = []

    if feed.get("rss"):
        try:
            entries = feedparser.parse(http_get(feed["rss"])).entries
            if entries:
                return feed, entries, "rss", None, round(time.time() - started, 1)
            errors.append("RSS порожній")
        except Exception as ex:
            errors.append(f"RSS: {str(ex)[:120]}")

    if not feed.get("site"):
        return feed, [], None, "; ".join(errors) or "немає ні RSS, ні домену", round(time.time() - started, 1)

    last = state.get(feed["name"], {}).get("gn_at")
    if last and now() - datetime.fromisoformat(last) < timedelta(minutes=GN_EVERY_MIN - 1):
        return feed, None, "google-skip", None, 0          # ще рано, лишаємо старі новини

    q = f"site:{feed['site']} when:1d"
    if feed.get("gn_terms"):
        q += " " + cfg.get("gn_terms", {}).get(feed["gn_terms"], "")
    try:
        entries = feedparser.parse(http_get(gn_url(q, feed.get("lang", "en")))).entries
        state.setdefault(feed["name"], {})["gn_at"] = iso(now())
        return feed, entries, "google", ("; ".join(errors) or None), round(time.time() - started, 1)
    except Exception as ex:
        errors.append(f"Google News: {str(ex)[:120]}")
        return feed, [], None, "; ".join(errors), round(time.time() - started, 1)


def raw_items(feed, entries, method, cutoff):
    out = []
    for e in entries:
        title, link = clean(e.get("title")), e.get("link") or ""
        if not title or not link:
            continue
        if method == "google":
            title, _ = strip_publisher(title, e)
        pub = entry_time(e) or now()
        pub = min(pub, now())
        if pub < cutoff:
            continue
        lang = feed.get("lang", "en")
        if lang == "uk" and RU_CHARS.search(title):
            lang = "ru"
        out.append({
            "id": hashlib.sha1(link.encode()).hexdigest()[:14],
            "orig": title, "url": link, "source": feed["name"], "region": feed.get("region", "intl"),
            "lang": lang, "filter": bool(feed.get("filter")), "published": iso(pub),
        })
    return out


# ---------- переклад ----------

def translate_all(texts, cache):
    todo = [t for t in dict.fromkeys(texts) if t not in cache]
    if not todo:
        return
    try:
        from deep_translator import GoogleTranslator
        tr = GoogleTranslator(source="auto", target="uk")
    except Exception as ex:
        print(f"[переклад] недоступний: {ex}", file=sys.stderr)
        return

    def run(chunk):
        if not chunk:
            return
        try:
            res = tr.translate("\n".join(chunk))
            lines = [l.strip() for l in (res or "").split("\n")]
            if len(lines) == len(chunk):
                for s, d in zip(chunk, lines):
                    if d:
                        cache[s] = d
                return
        except Exception:
            pass
        if len(chunk) == 1:
            return
        mid = len(chunk) // 2
        run(chunk[:mid]); run(chunk[mid:])

    chunk, size, deadline = [], 0, time.time() + 240
    for t in todo:
        if time.time() > deadline:
            print("[переклад] не встиг усе, решта в наступному прогоні", file=sys.stderr)
            break
        if size + len(t) > 4000 or len(chunk) >= 50:
            run(chunk); chunk, size = [], 0
        chunk.append(t); size += len(t) + 1
    if time.time() <= deadline:
        run(chunk)


def match_topics(rx, text):
    if rx.get("exclude") and rx["exclude"].search(text):
        return []
    return [tid for tid, r in rx["topics"].items() if r.search(text)]


# ---------- Google: тренди й топ новин ----------

def fetch_google(gcfg, rx, cache, prev, history_days, cutoff):
    status = []
    # Тренди
    trends = {t["query"]: t for t in prev.get("trends", [])
              if now() - datetime.fromisoformat(t["last_seen"]) < timedelta(days=history_days)}
    try:
        root = ET.fromstring(http_get(gcfg["trends_rss"]))
        fresh = []
        for it in root.iter("item"):
            q = clean(it.findtext("title"))
            traffic = clean(it.findtext("{*}approx_traffic"))
            news = [{"title": clean(n.findtext("{*}news_item_title")), "url": n.findtext("{*}news_item_url") or "",
                     "source": clean(n.findtext("{*}news_item_source"))} for n in it.findall("{*}news_item")]
            fresh.append((q, traffic, news))
        translate_all([q for q, _, _ in fresh] + [n["title"] for _, _, ns in fresh for n in ns], cache)
        for q, traffic, news in fresh:
            for n in news:
                n["title"] = cache.get(n["title"], n["title"])
            text = " ".join([q, cache.get(q, q)] + [n["title"] for n in news])
            topics = match_topics(rx, text)
            if not topics:
                continue
            old = trends.get(q, {})
            trends[q] = {"query": q, "query_uk": cache.get(q, q), "traffic": traffic, "topics": topics, "news": news[:3],
                         "first_seen": old.get("first_seen", iso(now())), "last_seen": iso(now())}
        status.append({"name": "Google Trends", "ok": True, "method": "rss", "items": len(trends)})
    except Exception as ex:
        status.append({"name": "Google Trends", "ok": False, "error": str(ex)[:150], "items": len(trends)})

    # Топ Google News для України
    news = {n["id"]: n for n in prev.get("news", []) if n["published"] >= iso(cutoff)}
    for url in gcfg.get("news_rss", []):
        try:
            for e in feedparser.parse(http_get(url)).entries:
                title, pub = strip_publisher(clean(e.get("title")), e)
                topics = match_topics(rx, title)
                if not topics or not e.get("link"):
                    continue
                nid = hashlib.sha1(e["link"].encode()).hexdigest()[:14]
                news[nid] = {"id": nid, "title": title, "url": e["link"], "source": pub or "Google News",
                             "published": iso(min(entry_time(e) or now(), now())), "topics": topics}
            status.append({"name": "Google News: топ України", "ok": True, "method": "rss", "items": len(news)})
        except Exception as ex:
            status.append({"name": "Google News: топ України", "ok": False, "error": str(ex)[:150], "items": 0})
    trend_list = sorted(trends.values(), key=lambda t: t["last_seen"], reverse=True)
    news_list = sorted(news.values(), key=lambda n: n["published"], reverse=True)
    return {"trends": trend_list, "news": news_list}, status


# ---------- головне ----------

def main():
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    cutoff = now() - timedelta(hours=cfg.get("retention_hours", 72))
    rx = {
        "ukraine": kw_rx(cfg["ukraine"]["keywords"]),
        "europe": kw_rx(cfg["europe"]["keywords"]),
        "foreign": kw_rx(cfg["foreign"]["keywords"]),
        "important": kw_rx(cfg["important"].get("keywords")),
        "topics": {t["id"]: kw_rx(t["keywords"]) for t in cfg["topics"]},
        "exclude": kw_rx(cfg.get("exclude")),
    }
    min_sources = int(cfg["important"].get("min_sources", 2))
    prev = load_json(NEWS, {})
    state = load_json(STATE, {})
    cache = load_json(TRCACHE, {})

    feeds = [f for f in cfg["feeds"] if f.get("enabled", True)]
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(fetch_feed, [(f, cfg, state) for f in feeds]))

    # Старі новини лишаються, поки не вийде термін, навіть якщо джерело зараз лежить
    by_id = {i["id"]: i for i in prev.get("items", []) if "orig" in i and i.get("published", "") >= iso(cutoff)}
    status, fresh = [], []
    for feed, entries, method, err, secs in results:
        if method == "google-skip":
            kept = sum(1 for i in by_id.values() if i["source"] == feed["name"])
            status.append({"name": feed["name"], "ok": True, "method": "google", "items": kept, "note": "кеш"})
            continue
        got = raw_items(feed, entries or [], method, cutoff) if method else []
        fresh.extend(got)
        status.append({"name": feed["name"], "ok": bool(method), "method": method, "items": len(got),
                       "error": err if not method else None, "note": err if method == "google" and err else None,
                       "seconds": secs})

    # Переклад: англійські — лише ті, що пройшли тематичний фільтр; інші мови — усі (щоб визначити тему)
    def topics_of(text):
        return match_topics(rx, text)
    need = []
    for i in fresh:
        if i["lang"] == "uk":
            continue
        if i["lang"] == "en" and i["filter"] and not topics_of(i["orig"]):
            continue
        need.append(i["orig"])
    translate_all(need, cache)

    for i in fresh:
        old = by_id.get(i["id"])
        i["first_seen"] = old.get("first_seen") if old else iso(now())
        i["title"] = i["orig"] if i["lang"] == "uk" else cache.get(i["orig"], i["orig"])
        text = f"{i['title']} {i['orig']}"
        i["topics"] = topics_of(text)
        if i["filter"] and not i["topics"]:
            continue
        by_id[i["id"]] = i

    # Склеювання дублікатів
    groups = {}
    for i in by_id.values():
        groups.setdefault(norm_title(i["orig"]), []).append(i)
    items = []
    for g in groups.values():
        g.sort(key=lambda x: x["published"])
        head = dict(g[0])
        head["sources"] = sorted({x["source"] for x in g})
        head["topics"] = sorted({t for x in g for t in x.get("topics", [])})
        items.append(head)

    # Вкладки
    for i in items:
        text = f"{i['title']} {i['orig']}"
        ua_kw = bool(rx["ukraine"].search(text))
        foreign = bool(rx["foreign"].search(text)) or bool(rx["europe"].search(text))
        i["ua"] = ua_kw or (i["region"] == "ua" and not foreign)
        i["eu"] = bool(rx["europe"].search(text))
        hot = len(i["sources"]) >= min_sources or bool(rx["important"] and rx["important"].search(text))
        i["important"] = hot and i["ua"]
        tabs = []
        if i["ua"]:
            tabs.append("ukraine")
        if i["important"]:
            tabs.append("important")
        if i["eu"] and i["topics"]:
            tabs.append("europe")
        if not i["ua"]:
            tabs.append("world")
        i["tabs"] = tabs
        i["orig_shown"] = i["orig"] if i["title"] != i["orig"] else None
        for k in ("filter",):
            i.pop(k, None)
    items.sort(key=lambda x: x["published"], reverse=True)

    google, gstatus = fetch_google(cfg["google"], rx, cache, prev.get("google", {}),
                                   cfg.get("trends_history_days", 7), now() - timedelta(hours=48))

    # Кеш перекладів: лише те, що ще трапляється
    alive = {i["orig"] for i in fresh} | {i["orig"] for i in items}
    alive |= {t["query"] for t in google["trends"]} | {n["title"] for t in google["trends"] for n in t["news"]}
    cache = {k: v for k, v in cache.items() if k in alive}

    out = {
        "updated": iso(now()),
        "tabs": TABS,
        "topics": [{"id": t["id"], "title": t["title"]} for t in cfg["topics"]],
        "items": items,
        "google": google,
    }
    save_json(NEWS, out)
    save_json(STATUS, {"updated": out["updated"], "feeds": status + gstatus})
    save_json(TRCACHE, cache)
    save_json(STATE, state)

    ok = sum(1 for s in status if s["ok"])
    via_g = sum(1 for s in status if s.get("method") == "google")
    print(f"Джерел: {ok}/{len(status)} працюють (з них через Google News: {via_g}) · новин: {len(items)} · "
          f"трендів: {len(google['trends'])} · топ Google: {len(google['news'])}")
    for s in status + gstatus:
        if not s["ok"]:
            print(f"  ✗ {s['name']}: {s.get('error')}")


if __name__ == "__main__":
    main()
