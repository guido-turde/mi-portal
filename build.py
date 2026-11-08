#!/usr/bin/env python3
"""Mi portal: lee los feeds RSS de cada diario y genera site/index.html.

Uso local:  pip install -r requirements.txt && python build.py
Resultado:  site/index.html (abrilo con el navegador)
"""
import html
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests
import trafilatura

ROOT = Path(__file__).parent
MAX_ITEMS = 24          # notas por diario
MAX_SUMMARY = 520       # caracteres del resumen
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "es,en;q=0.8,de;q=0.6",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

# Texto completo de las notas: solo si el workflow lo pide (PORTAL_FULLTEXT=1).
# Activalo únicamente si la página publicada es privada (Cloudflare Access o contraseña).
FULLTEXT = os.environ.get("PORTAL_FULLTEXT") == "1"
FULLTEXT_PER_SOURCE = 12   # cuántas notas por diario (las primeras del feed)
MAX_PARAGRAPHS = 80

# ---------------------------------------------------------------------------
# DIARIOS. Para agregar, quitar o reordenar pestañas, editá esta lista.
# "feeds" puede tener varias URLs (se juntan en una sola pestaña).
# "id" tiene que coincidir con un tema en template.html (objeto THEMES).
# ---------------------------------------------------------------------------
SOURCES = [
    {"id": "lanacion", "lang": "es", "name": "La Nación", "tagline": "Buenos Aires",
     "home": "https://www.lanacion.com.ar",
     "feeds": ["https://www.lanacion.com.ar/arc/outboundfeeds/rss/?outputType=xml"]},
    {"id": "pagina12", "lang": "es", "name": "Página/12", "tagline": "Buenos Aires",
     "home": "https://www.pagina12.com.ar",
     "feeds": ["https://www.pagina12.com.ar/rss/portada"]},
    {"id": "guardian", "lang": "en", "name": "The Guardian", "tagline": "International edition",
     "home": "https://www.theguardian.com/international",
     "feeds": ["https://www.theguardian.com/international/rss"]},
    {"id": "bbc", "lang": "en", "name": "BBC News", "tagline": "Home",
     "home": "https://www.bbc.com/news",
     "feeds": ["https://feeds.bbci.co.uk/news/rss.xml"]},
    {"id": "dw", "lang": "en", "name": "DW", "tagline": "Top stories",
     "home": "https://www.dw.com",
     "feeds": ["https://rss.dw.com/xml/rss-en-top"]},
    {"id": "bz", "lang": "de", "name": "Berliner Zeitung", "tagline": "Berlin",
     "home": "https://www.berliner-zeitung.de",
     "feeds": ["https://www.berliner-zeitung.de/feed.xml"]},
    {"id": "spiegel", "lang": "de", "name": "Der Spiegel", "tagline": "Schlagzeilen",
     "home": "https://www.spiegel.de",
     "feeds": ["https://www.spiegel.de/schlagzeilen/index.rss"]},
]

TAG_RE = re.compile(r"<[^>]+>")
IMG_RE = re.compile(r"""<img[^>]+src=["']([^"']+)""", re.I)


def clean(text):
    """HTML -> texto plano en una sola línea."""
    text = html.unescape(TAG_RE.sub(" ", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def shorten(text, limit):
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:.- ")
    return cut + "…"


def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def pick_image(entry, raw_html):
    """Busca la foto principal en los campos habituales de un feed."""
    groups = [
        [(to_int(m.get("width")), m.get("url")) for m in entry.get("media_content", [])
         if m.get("url") and m.get("medium", "image") == "image"
         and "video" not in (m.get("type") or "")],
        [(to_int(m.get("width")), m.get("url")) for m in entry.get("media_thumbnail", [])
         if m.get("url")],
        [(0, e.get("href")) for e in entry.get("enclosures", [])
         if e.get("href") and "image" in (e.get("type") or "")],
        [(0, m) for m in IMG_RE.findall(raw_html or "")[:1]],
    ]
    for group in groups:
        group = [g for g in group if g[1]]
        if group:
            url = max(group, key=lambda g: g[0])[1]
            break
    else:
        return None
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith("http"):
        return None
    url = url.replace("http://", "https://", 1)
    # BBC entrega miniaturas de 240 px: pedimos una versión más grande.
    url = re.sub(r"/(standard|news)/240/", r"/\1/800/", url)
    return url


def to_iso(entry):
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if not t:
        return None
    return datetime(*t[:6], tzinfo=timezone.utc).isoformat()


def extract_body(url, summary, title):
    """Baja la nota y devuelve sus párrafos, o None si no se pudo (bloqueo, paywall)."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
            return None
        text = trafilatura.extract(r.content, include_comments=False, include_tables=False,
                                   favor_precision=True, output_format="txt")
    except Exception:
        return None
    if not text:
        return None
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    if paras and (paras[0] in title or title in paras[0]):
        paras = paras[1:]   # el extractor repite el titular como primer párrafo
    # Si casi no hay más texto que el resumen, es un bloqueo o un paywall: no sirve.
    if sum(len(p) for p in paras) < len(summary) + 300:
        return None
    return paras[:MAX_PARAGRAPHS]


def add_full_text(items):
    targets = items[:FULLTEXT_PER_SOURCE]
    with ThreadPoolExecutor(max_workers=4) as pool:
        bodies = list(pool.map(lambda it: extract_body(it["link"], it["summary"], it["title"]), targets))
    for item, body in zip(targets, bodies):
        if body:
            item["body"] = body
    return sum(1 for b in bodies if b)


def fetch_source(src):
    items, seen, errors = [], set(), []
    for url in src["feeds"]:
        try:
            feed = feedparser.parse(url, agent=UA, request_headers={"Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8"})
            status = feed.get("status")
            if status and status >= 400:
                raise RuntimeError(f"HTTP {status}")
            if not feed.entries:
                ctype = (feed.get("headers") or {}).get("content-type")
                raise RuntimeError(f"{feed.get('bozo_exception') or 'feed vacío'} [HTTP {status}, {ctype}]")
        except Exception as exc:  # un diario caído no debe frenar a los demás
            errors.append(f"{url}: {exc}")
            continue
        for e in feed.entries:
            title = clean(e.get("title"))
            link = e.get("link")
            if not title or not link or link in seen:
                continue
            seen.add(link)
            raw = e.get("summary") or e.get("description") or ""
            summary = re.sub(r"\s*Continue reading\.\.\.\s*$", "", clean(raw))
            items.append({
                "title": title,
                "summary": shorten(summary, MAX_SUMMARY),
                "link": link,
                "image": pick_image(e, raw),
                "published": to_iso(e),
            })
            if len(items) >= MAX_ITEMS:
                break
    return {
        "id": src["id"], "lang": src.get("lang", "es"), "name": src["name"], "tagline": src.get("tagline", ""),
        "home": src["home"], "items": items,
        "error": "; ".join(errors) if errors and not items else None,
    }


def main():
    template = (ROOT / "template.html").read_text(encoding="utf-8")
    sources = []
    for src in SOURCES:
        result = fetch_source(src)
        sources.append(result)
        status = "OK " if result["items"] else "ERR"
        extra = f"  ({result['error']})" if result["error"] else ""
        full = ""
        if FULLTEXT and result["items"]:
            n = add_full_text(result["items"])
            full = f", {n} con texto completo"
        print(f"{status} {result['name']:<18} {len(result['items']):>2} notas{full}{extra}")

    if not any(s["items"] for s in sources):
        sys.exit("Ningún feed respondió; no se genera la página.")

    payload = {"generated": datetime.now(timezone.utc).isoformat(), "sources": sources}
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    out = ROOT / "site"
    out.mkdir(exist_ok=True)
    (out / "index.html").write_text(
        template.replace("/*__DATA__*/null", data), encoding="utf-8")
    print(f"Listo: {out / 'index.html'}")


if __name__ == "__main__":
    main()
