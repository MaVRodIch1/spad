"""
Статический разбор SPA https://stickerdom.store/.

Что делает:
  1. Качает index.html
  2. Собирает все <script src> и <link rel=modulepreload|preload as=script>
  3. Скачивает все JS-чанки в bundle/
  4. Грепает по ним:
       - API-эндпоинты (https://api... и относительные пути /api/...)
       - SPA-роуты (path: "/...", react-router / vue-router паттерны)
       - Все встречающиеся хосты
  5. Кладёт сводку в bundle_report.json

Запуск:
    python fetch_bundle.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

BASE_URL = os.environ.get("WEBAPP_URL", "https://stickerdom.store/")
OUT_DIR = Path(os.environ.get("BUNDLE_DIR", "bundle"))
REPORT = Path(os.environ.get("BUNDLE_REPORT", "bundle_report.json"))
TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "30"))

UA = (
    "Mozilla/5.0 (Linux; Android 13; SM-G998B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
)

URL_RE = re.compile(r'https?://[a-zA-Z0-9.\-]+(?:/[a-zA-Z0-9._\-/?=&%:#]*)?')
PATH_RE = re.compile(r'["\'`](/[a-zA-Z0-9_\-][a-zA-Z0-9._\-/{}:?]*)["\'`]')
ROUTE_KEY_RE = re.compile(r'(?:path|route)\s*:\s*["\'`]([^"\'`]+)["\'`]')

SKIP_EXT = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif",
            ".svg", ".webp", ".ico", ".woff", ".woff2", ".ttf",
            ".map", ".mp3", ".mp4", ".json", ".txt")


def is_resource(path: str) -> bool:
    p = path.split("?", 1)[0].split("#", 1)[0].lower()
    return p.endswith(SKIP_EXT)


async def fetch(client: httpx.AsyncClient, url: str) -> httpx.Response:
    r = await client.get(url, timeout=TIMEOUT, follow_redirects=True)
    r.raise_for_status()
    return r


def collect_chunks(index_html: str, base: str) -> list[str]:
    soup = BeautifulSoup(index_html, "html.parser")
    refs: set[str] = set()
    for tag in soup.find_all("script", src=True):
        refs.add(tag["src"])
    for tag in soup.find_all("link"):
        rel = tag.get("rel") or []
        href = tag.get("href")
        if not href:
            continue
        if "modulepreload" in rel:
            refs.add(href)
        elif "preload" in rel and tag.get("as") == "script":
            refs.add(href)
    return sorted({urljoin(base, r) for r in refs})


async def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    base_host = urlparse(BASE_URL).netloc

    async with httpx.AsyncClient(headers={"User-Agent": UA}) as client:
        print(f"[*] GET {BASE_URL}")
        r = await fetch(client, BASE_URL)
        index_html = r.text
        (OUT_DIR / "index.html").write_text(index_html, encoding="utf-8")

        chunks = collect_chunks(index_html, BASE_URL)
        print(f"[*] {len(chunks)} chunks/scripts найдены")

        artifacts = []
        for url in chunks:
            host = urlparse(url).netloc
            if host and host != base_host:
                # Внешние CDN-ы пропускаем — нам интересен только сам SPA
                continue
            try:
                rr = await fetch(client, url)
            except Exception as e:
                print(f"  ! {url}: {e}")
                continue
            name = url.rsplit("/", 1)[-1].split("?", 1)[0] or "index.js"
            (OUT_DIR / name).write_text(rr.text, encoding="utf-8")
            artifacts.append({"url": url, "size": len(rr.text), "file": name})
            print(f"  [+] {name}  {len(rr.text):>9} b")

    # Анализ скачанного
    all_urls: Counter[str] = Counter()
    all_paths: Counter[str] = Counter()
    all_routes: set[str] = set()
    hosts: Counter[str] = Counter()

    for a in artifacts:
        text = (OUT_DIR / a["file"]).read_text(encoding="utf-8")
        for u in URL_RE.findall(text):
            all_urls[u] += 1
            host = urlparse(u).netloc
            if host:
                hosts[host] += 1
        for p in PATH_RE.findall(text):
            all_paths[p] += 1
        for r in ROUTE_KEY_RE.findall(text):
            all_routes.add(r)

    api_paths = sorted(p for p in all_paths if not is_resource(p))
    external_urls = sorted(u for u in all_urls if not is_resource(u))

    report = {
        "base_url": BASE_URL,
        "chunks": artifacts,
        "hosts": hosts.most_common(),
        "external_urls": external_urls,
        "internal_paths": api_paths,
        "routes": sorted(all_routes),
        "stats": {
            "chunks": len(artifacts),
            "unique_urls": len(external_urls),
            "unique_paths": len(api_paths),
            "routes": len(all_routes),
        },
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[*] Сохранили отчёт в {REPORT}")
    for k, v in report["stats"].items():
        print(f"    {k}: {v}")
    if hosts:
        print("[*] Топ хостов:")
        for h, c in hosts.most_common(10):
            print(f"    {c:>4}  {h}")


if __name__ == "__main__":
    asyncio.run(main())
