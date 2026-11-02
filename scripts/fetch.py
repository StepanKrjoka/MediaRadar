#!/usr/bin/env python3
"""Radar: збирає RSS-стрічки, класифікує за темами, перекладає заголовки
і зберігає data/news.json та data/status.json для сторінки."""

import hashlib
import html
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
import os
CONFIG = Path(os.environ.get("RADAR_CONFIG", ROOT / "config" / "sources.yaml"))
DATA = ROOT / "data"
NEWS = DATA / "news.json"
STATUS = DATA / "status.json"
TRCACHE = DATA / "translations.json"

UA = "Mozilla/5.0 (compatible; RadarBot/1.0; +https://github.com/)"
TIMEOUT = 20


# ---------- допоміжне ----------

def now_utc():
    return datetime.now(timezone.utc)


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def clean_text(s):
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def compile_keywords(words):
    """«нафт*» → корінь; «oil» → ціле слово; фрази допускаються."""
    parts = []
    for w in words or []:
        w = str(w).lower().strip()
        if not w:
            continue
        if w.endswith("*"):
            parts.append(r"(?<!\w)" + re.escape(w[:-1]).replace(r"\*", r"\w*").replace(r"\ ", r"\s+") + r"\w*")
        else:
            parts.append(r"(?<!\w)" + re.escape(w).replace(r"\*", r"\w*").replace(r"\ ", r"\s+") + r"(?!\w)")
    return re.compile("|".join(parts), re.IGNORECASE) if parts else None


def norm_title(t):
    t = t.lower()
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()[:90]


def split_google_title(title, entry):
    """Google News дає «Заголовок - Видання». Відокремлюємо видання."""
    publisher = None
    src = entry.get("source")
    if isinstance(src, dict):
        publisher = src.get("title")
    if publisher and title.endswith(" - " + publisher):
        title = title[: -len(publisher) - 3]
    elif " - " in title and not publisher:
        title, publisher = title.rsplit(" - ", 1)
    return title.strip(), publisher


def entry_time(e):
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        t = e.get(key)
        if t:
            try:
                return datetime.fromtimestamp(time.mktime(t), tz=timezone.utc)
            except Exception:
                pass
    return None


# ---------- збір ----------

def fetch_feed(feed):
    started = time.time()
    url = feed["url"]
    try:
        if url.startswith(("http://", "https://")):
            r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
            r.raise_for_status()
            parsed = feedparser.parse(r.content)
        else:  # локальний файл (для тестів)
            parsed = feedparser.parse(url)
        if parsed.bozo and not parsed.entries:
            raise ValueError(f"не вдалося розібрати RSS: {parsed.bozo_exception}")
        return feed, parsed.entries, None, round(time.time() - started, 1)
    except Exception as ex:
        return feed, [], str(ex)[:200], round(time.time() - started, 1)


def build_items(feed, entries, topic_rx, cutoff):
    items = []
    is_google = "news.google.com" in feed["url"] or feed["name"].startswith("Google News")
    for e in entries:
        title = clean_text(e.get("title"))
        link = e.get("link") or ""
        if not title or not link:
            continue
        via = None
        source = feed["name"]
        if is_google:
            title, publisher = split_google_title(title, e)
            if publisher:
                via = feed["name"]
                source = publisher
        published = entry_time(e) or now_utc()
        if published > now_utc() + timedelta(minutes=10):
            published = now_utc()
        if published < cutoff:
            continue
        summary = clean_text(e.get("summary"))[:400]
        hay = f"{title} {summary if not is_google else ''}"
        topics = [tid for tid, rx in topic_rx.items() if rx.search(hay)]
        if feed.get("filter") and not topics:
            continue
        items.append({
            "id": hashlib.sha1(link.encode()).hexdigest()[:14],
            "title_orig": title,
            "url": link,
            "source": source,
            "via": via,
            "tab": feed.get("tab", "world"),
            "lang": feed.get("lang", "en"),
            "published": published.isoformat(timespec="seconds"),
            "topics": topics,
        })
    return items


# ---------- переклад ----------

def translate_titles(items, cache):
    todo = sorted({i["title_orig"] for i in items if i["lang"] != "uk" and i["title_orig"] not in cache})
    if todo:
        try:
            from deep_translator import GoogleTranslator
            tr = GoogleTranslator(source="auto", target="uk")
            for k in range(0, len(todo), 40):
                chunk = todo[k:k + 40]
                try:
                    out = tr.translate_batch(chunk)
                except Exception:
                    out = []
                    for t in chunk:
                        try:
                            out.append(tr.translate(t))
                        except Exception:
                            out.append(None)
                for src, dst in zip(chunk, out):
                    if dst:
                        cache[src] = dst
        except Exception as ex:
            print(f"[переклад] пропущено: {ex}", file=sys.stderr)
    for i in items:
        if i["lang"] == "uk":
            i["title"] = i["title_orig"]
            i.pop("title_orig")
        else:
            i["title"] = cache.get(i["title_orig"], i["title_orig"])
            if i["title"] == i["title_orig"]:
                i.pop("title_orig")
    return cache


# ---------- головне ----------

def main():
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    retention = timedelta(hours=cfg.get("retention_hours", 72))
    cutoff = now_utc() - retention

    topic_rx = {t["id"]: compile_keywords(t["keywords"]) for t in cfg.get("topics", [])}
    topic_rx = {k: v for k, v in topic_rx.items() if v}
    imp_cfg = cfg.get("important", {}) or {}
    imp_rx = compile_keywords(imp_cfg.get("keywords"))
    min_sources = int(imp_cfg.get("min_sources", 2))

    feeds = [f for f in cfg.get("feeds", []) if f.get("enabled", True)]
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(fetch_feed, feeds))

    status, fresh = [], []
    for feed, entries, err, secs in results:
        got = build_items(feed, entries, topic_rx, cutoff) if not err else []
        fresh.extend(got)
        status.append({"name": feed["name"], "tab": feed.get("tab"), "ok": err is None,
                       "error": err, "items": len(got), "seconds": secs})

    # Об'єднуємо зі старими новинами: стрічка не «провалюється», якщо джерело тимчасово лежить
    old = load_json(NEWS, {}).get("items", [])
    by_id = {i["id"]: i for i in old if i.get("published", "") >= cutoff.isoformat()}
    for i in fresh:
        prev = by_id.get(i["id"])
        if prev:
            i["first_seen"] = prev.get("first_seen", prev["published"])
        else:
            i["first_seen"] = now_utc().isoformat(timespec="seconds")
        by_id[i["id"]] = i
    items = list(by_id.values())

    # Переклад тільки нових заголовків (кеш)
    cache = load_json(TRCACHE, {})
    cache = translate_titles([i for i in items if "title" not in i], cache)
    live = {i.get("title_orig") for i in items}
    cache = {k: v for k, v in cache.items() if k in live}

    # Дублікати: одна подія з кількох джерел → лишаємо найранішу, рахуємо джерела
    groups = {}
    for i in items:
        key = norm_title(i.get("title_orig") or i["title"])
        groups.setdefault(key, []).append(i)
    merged = []
    for group in groups.values():
        group.sort(key=lambda x: x["published"])
        head = group[0]
        head["sources"] = sorted({g["source"] for g in group})
        merged.append(head)

    for i in merged:
        text = f"{i['title']} {i.get('title_orig', '')}"
        i["important"] = len(i["sources"]) >= min_sources or bool(imp_rx and imp_rx.search(text))

    merged.sort(key=lambda x: x["published"], reverse=True)

    out = {
        "updated": now_utc().isoformat(timespec="seconds"),
        "tabs": cfg.get("tabs", []),
        "topics": [{"id": t["id"], "title": t["title"]} for t in cfg.get("topics", [])],
        "items": merged,
    }
    save_json(NEWS, out)
    save_json(STATUS, {"updated": out["updated"], "feeds": status})
    save_json(TRCACHE, cache)

    ok = sum(1 for s in status if s["ok"])
    print(f"Джерел: {ok}/{len(status)} працюють · новин у стрічці: {len(merged)} · нових за прогін: {len(fresh)}")
    for s in status:
        if not s["ok"]:
            print(f"  ✗ {s['name']}: {s['error']}")


if __name__ == "__main__":
    main()
