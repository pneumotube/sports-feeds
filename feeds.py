#!/usr/bin/env python3
"""Собирает RSS-ленты из страниц новостей турниров sports.ru.

Запуск: python3 feeds.py            — скачать страницы и обновить docs/*.xml
        python3 feeds.py --from DIR — взять HTML из DIR/<id>.html (для проверки)

Только стандартная библиотека Python, без зависимостей.
"""
import html
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

FEEDS = [
    {
        "id": "nhl",
        "title": "НХЛ — новости Sports.ru",
        "url": "https://www.sports.ru/hockey/tournament/nhl/news/",
    },
    {
        "id": "apl",
        "title": "АПЛ — новости Sports.ru",
        "url": "https://www.sports.ru/football/tournament/premier-league/news/",
    },
]

ROOT = Path(__file__).resolve().parent      # feeds.py лежит в корне репозитория
OUT_DIR = ROOT / "docs"
STATE_DIR = ROOT / "state"
KEEP_IN_STATE = 400      # сколько последних новостей помнить
KEEP_IN_FEED = 200       # сколько отдавать в ленте
MIN_ITEMS = 10           # меньше — считаем, что страница сломалась или заблокирована
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


def fetch(url: str) -> str:
    """Скачивает страницу с меткой времени в адресе, чтобы обойти кэш сайта."""
    sep = "&" if "?" in url else "?"
    last_err = None
    for attempt in range(3):
        full = f"{url}{sep}t={int(time.time())}{attempt}"
        req = urllib.request.Request(full, headers={
            "User-Agent": UA,
            "Accept-Language": "ru-RU,ru;q=0.9",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"не удалось скачать {url}: {last_err}")


def parse_date(s: str) -> datetime:
    s = s.strip().replace(" ", "T", 1)
    if re.search(r"[+-]\d\d$", s):          # "+03" -> "+03:00"
        s += ":00"
    d = datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"«\s+", "«", re.sub(r"\s+»", "»", s))


ALLOWED_HOSTS = ("https://www.sports.ru/", "https://video.sports.ru/")


def allowed(url: str) -> bool:
    """Отсекает промо-игры и викторины (sirena.sports.ru и т. п.), оставляя новости и видео."""
    return url.startswith(ALLOWED_HOSTS)


def news_id(url: str) -> str:
    m = re.search(r"/(\d{7,})", url)
    return m.group(1) if m else url


def parse_jsonld(page: str) -> list[dict]:
    """Новая вёрстка: список новостей в JSON-LD (ItemList из NewsArticle)."""
    items = []
    for block in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', page, re.S):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        stack = [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack.extend(d)
            elif isinstance(d, dict):
                if d.get("@type") == "ItemList":
                    for el in d.get("itemListElement", []):
                        art = el.get("item", el) if isinstance(el, dict) else None
                        if not isinstance(art, dict) or "NewsArticle" not in str(art.get("@type")):
                            continue
                        url = art.get("url") or (art.get("mainEntityOfPage") or {}).get("@id", "")
                        url = url.split("#")[0]
                        if not url or not art.get("headline") or not art.get("datePublished"):
                            continue
                        title = clean(art["headline"])
                        desc = clean(art.get("description", ""))
                        if desc == title:
                            desc = ""
                        if url.startswith("https://video.sports.ru/"):
                            title = "▶ " + title
                        items.append({
                            "id": news_id(url),
                            "url": url,
                            "title": title,
                            "description": desc,
                            "date": parse_date(art["datePublished"]).isoformat(),
                        })
                else:
                    stack.extend(d.values())
    return items


def parse_one_news(page: str) -> list[dict]:
    """Старая вёрстка: <p class="one_news" data-news-dtime=... data-news-id=...>."""
    items = []
    for m in re.finditer(r'<p class="one_news"([^>]*)>(.*?)</p>', page, re.S):
        attrs, body = m.group(1), m.group(2)
        dt = re.search(r'data-news-dtime="([^"]+)"', attrs)
        a = re.search(r'<a class="short-text" href="([^"]+)"(?:[^>]*?title="([^"]*)")?[^>]*>(.*?)</a>', body, re.S)
        if not dt or not a:
            continue
        url = html.unescape(a.group(1))
        if url.startswith("/"):
            url = "https://www.sports.ru" + url
        title = clean(a.group(3))
        if not title:
            continue
        if re.search(r'class="video"', body):
            title = "▶ " + title
        items.append({
            "id": news_id(url),
            "url": url,
            "title": title,
            "description": clean(a.group(2) or ""),
            "date": parse_date(dt.group(1)).isoformat(),
        })
    return items


def build_rss(feed: dict, items: list[dict]) -> str:
    newest = items[0]["date"] if items else datetime.now(timezone.utc).isoformat()
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "<channel>",
        f"<title>{escape(feed['title'])}</title>",
        f"<link>{escape(feed['url'])}</link>",
        f"<description>{escape(feed['title'])}</description>",
        "<language>ru</language>",
        # дата самой свежей новости, а не время запуска: файл меняется только при новых новостях
        f"<lastBuildDate>{format_datetime(datetime.fromisoformat(newest))}</lastBuildDate>",
    ]
    for it in items:
        out += [
            "<item>",
            f"<title>{escape(it['title'])}</title>",
            f"<link>{escape(it['url'])}</link>",
            f'<guid isPermaLink="true">{escape(it["url"])}</guid>',
            f"<pubDate>{format_datetime(datetime.fromisoformat(it['date']))}</pubDate>",
        ]
        if it.get("description"):
            out.append(f"<description>{escape(it['description'])}</description>")
        out.append("</item>")
    out += ["</channel>", "</rss>", ""]
    return "\n".join(out)


def process(feed: dict, source_dir: Path | None) -> int:
    if source_dir:
        page = (source_dir / f"{feed['id']}.html").read_text(encoding="utf-8")
    else:
        page = fetch(feed["url"])

    fresh = [it for it in (parse_jsonld(page) or parse_one_news(page)) if allowed(it["url"])]
    if len(fresh) < MIN_ITEMS:
        raise RuntimeError(
            f"{feed['id']}: найдено только {len(fresh)} новостей "
            f"(страница {len(page)} байт) — сайт поменял вёрстку или блокирует запросы")

    state_file = STATE_DIR / f"{feed['id']}.json"
    known = {}
    if state_file.exists():
        known = {it["id"]: it for it in json.loads(state_file.read_text(encoding="utf-8"))}
    new_count = sum(1 for it in fresh if it["id"] not in known)
    for it in fresh:
        known[it["id"]] = it        # обновляем заголовок, если редакция его поправила

    items = sorted((it for it in known.values() if allowed(it["url"])), key=lambda x: (x["date"], x["id"]), reverse=True)[:KEEP_IN_STATE]
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(items, ensure_ascii=False, indent=0), encoding="utf-8")
    (OUT_DIR / f"{feed['id']}.xml").write_text(build_rss(feed, items[:KEEP_IN_FEED]), encoding="utf-8")
    print(f"{feed['id']}: на странице {len(fresh)}, новых {new_count}, в ленте {min(len(items), KEEP_IN_FEED)}")
    return new_count


def main() -> int:
    source_dir = None
    if len(sys.argv) == 3 and sys.argv[1] == "--from":
        source_dir = Path(sys.argv[2])
    failed = False
    for feed in FEEDS:
        try:
            process(feed, source_dir)
        except Exception as e:  # noqa: BLE001
            print(f"::error::{e}")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
