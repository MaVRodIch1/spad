"""
Динамический обход SPA с моком window.Telegram.WebApp.

Запускает headless Chromium через Playwright, имитирует Telegram WebApp,
ходит по обнаруженным внутренним ссылкам, собирает все XHR/fetch-запросы.

Подготовка (один раз):
    pip install -r requirements.txt
    playwright install chromium
    # на чистом VPS может понадобиться:
    playwright install-deps

Запуск:
    python crawl_webapp.py
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

from playwright.async_api import Page, Request, async_playwright

BASE_URL = os.environ.get("WEBAPP_URL", "https://stickerdom.store/")
OUT = Path(os.environ.get("CRAWL_REPORT", "webapp_routes.json"))
MAX_ROUTES = int(os.environ.get("MAX_ROUTES", "30"))
NAV_TIMEOUT_MS = int(os.environ.get("NAV_TIMEOUT_MS", "20000"))
WAIT_AFTER_LOAD_MS = int(os.environ.get("WAIT_AFTER_LOAD_MS", "1500"))

INIT_SCRIPT = r"""
(() => {
  const noop = () => {};
  const button = {
    isVisible: false, isActive: false, isProgressVisible: false,
    text: '', color: '', textColor: '',
    show: noop, hide: noop, enable: noop, disable: noop,
    showProgress: noop, hideProgress: noop,
    setText: noop, onClick: noop, offClick: noop, setParams: noop,
  };
  const back = {
    isVisible: false, show: noop, hide: noop, onClick: noop, offClick: noop,
  };
  window.Telegram = {
    WebApp: {
      initData: '',
      initDataUnsafe: {
        user: { id: 1, first_name: 'Tester', username: 'tester', language_code: 'en' },
        start_param: '',
        auth_date: Math.floor(Date.now() / 1000),
        hash: '',
      },
      version: '7.6',
      platform: 'tdesktop',
      colorScheme: 'dark',
      themeParams: {
        bg_color: '#000000', text_color: '#ffffff', hint_color: '#999999',
        link_color: '#5288c1', button_color: '#5288c1', button_text_color: '#ffffff',
        secondary_bg_color: '#0f0f0f',
      },
      isExpanded: true,
      viewportHeight: 800, viewportStableHeight: 800,
      headerColor: '#000000', backgroundColor: '#000000',
      isClosingConfirmationEnabled: false,
      BackButton: back, MainButton: button, SettingsButton: back,
      HapticFeedback: {
        impactOccurred: noop, notificationOccurred: noop, selectionChanged: noop,
      },
      CloudStorage: {
        getItem: (k, cb) => cb && cb(null, null),
        setItem: (k, v, cb) => cb && cb(null, true),
        removeItem: (k, cb) => cb && cb(null, true),
        getKeys: (cb) => cb && cb(null, []),
      },
      ready: noop, expand: noop, close: noop,
      onEvent: noop, offEvent: noop, sendData: noop,
      switchInlineQuery: noop, openLink: noop, openTelegramLink: noop,
      openInvoice: noop, showPopup: noop, showAlert: noop, showConfirm: noop,
      enableClosingConfirmation: noop, disableClosingConfirmation: noop,
      setHeaderColor: noop, setBackgroundColor: noop,
      requestWriteAccess: noop, requestContact: noop,
    },
  };
})();
"""


def normalize(u: str) -> str:
    return urldefrag(u)[0].rstrip("/") or "/"


def is_internal(url: str, base_host: str) -> bool:
    host = urlparse(url).netloc
    return (not host) or host == base_host


async def snapshot(page: Page) -> dict:
    title = await page.title()
    try:
        anchors = await page.eval_on_selector_all(
            "a[href]", "els => els.map(e => e.getAttribute('href'))"
        )
    except Exception:
        anchors = []
    return {"title": title, "anchors": [a for a in anchors if a]}


async def main() -> None:
    base_host = urlparse(BASE_URL).netloc
    visited: set[str] = set()
    queue: list[str] = [normalize(BASE_URL)]
    results: dict[str, dict] = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                "Telegram-Android/10.13"
            ),
            viewport={"width": 390, "height": 844},
            locale="en-US",
        )
        await context.add_init_script(INIT_SCRIPT)

        while queue and len(visited) < MAX_ROUTES:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)

            page = await context.new_page()
            requests: list[dict] = []

            def on_request(req: Request) -> None:
                requests.append(
                    {
                        "url": req.url,
                        "method": req.method,
                        "resource_type": req.resource_type,
                        "headers": {
                            k: v
                            for k, v in (req.headers or {}).items()
                            if k.lower() in {"content-type", "authorization", "x-tg-init-data"}
                        },
                    }
                )

            page.on("request", on_request)
            print(f"[>] {url}")
            try:
                await page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
                try:
                    await page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
                except Exception:
                    pass
                await page.wait_for_timeout(WAIT_AFTER_LOAD_MS)
                snap = await snapshot(page)
            except Exception as e:
                results[url] = {"error": str(e)}
                await page.close()
                continue

            anchors = snap["anchors"]
            internal_links = sorted(
                {
                    normalize(urljoin(url, a))
                    for a in anchors
                    if a and not a.startswith(("mailto:", "tel:", "javascript:"))
                }
            )
            internal_links = [u for u in internal_links if is_internal(u, base_host)]

            api_calls = [
                r for r in requests
                if r["resource_type"] in ("xhr", "fetch")
                or "/api" in r["url"]
                or "/v1/" in r["url"]
                or "/v2/" in r["url"]
            ]

            results[url] = {
                "title": snap["title"],
                "links": internal_links,
                "api_calls": api_calls,
                "request_count": len(requests),
            }
            print(f"    title={snap['title']!r}  links={len(internal_links)}  api={len(api_calls)}")

            for link in internal_links:
                if link not in visited and link not in queue:
                    queue.append(link)

            await page.close()

        await browser.close()

    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[*] Visited {len(visited)} routes, saved to {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
