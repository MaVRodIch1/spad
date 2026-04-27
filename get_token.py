"""
Получает access-токен api.stickerdom.store, обменивая Telegram WebApp initData
через юзер-бот Telethon.

Шаги:
  1. Через Telethon вызывает messages.requestWebView для @sticker_bot и
     https://stickerdom.store/ — получает URL с tgWebAppData во фрагменте.
  2. Достаёт initData (raw query string из фрагмента).
  3. Пробует несколько вариантов auth-эндпоинта на api.stickerdom.store
     (чтобы не угадывать схему руками): POST /api/v1/auth с разными полями,
     а также с заголовком X-Telegram-Init-Data.
  4. Если получает токен — печатает готовую строку AUTH_HEADER и кладёт её
     в auth.json (initData + token + timestamp).

Запуск:
    python get_token.py

Готовый токен потом скармливаешь monitor_launches.py:
    AUTH_HEADER="Authorization: Bearer <token>" python monitor_launches.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.functions.messages import (
    RequestWebViewRequest,
    RequestSimpleWebViewRequest,
)

load_dotenv()

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
PHONE = os.environ.get("TG_PHONE")
SESSION = os.environ.get("TG_SESSION", "userbot")
TARGET = os.environ.get("TG_TARGET", "sticker_bot")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://stickerdom.store/")
API_BASE = os.environ.get("API_BASE_URL", "https://api.stickerdom.store")
PLATFORM = os.environ.get("WEBAPP_PLATFORM", "android")  # android / ios / tdesktop
OUT_FILE = Path(os.environ.get("AUTH_OUT", "auth.json"))

UA = (
    "Mozilla/5.0 (Linux; Android 13; SM-G998B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36 Telegram-Android/10.13"
)


def _extract_tgwebappdata(url: str) -> str:
    """Достаёт raw URL-encoded tgWebAppData из URL мини-аппа."""
    parsed = urlparse(url)
    frag = parsed.fragment or parsed.query
    # parse_qs декодирует один уровень → user=%7B...%7D остаётся как есть.
    # Эту форму бэк HMAC'ит и валидирует — лишний unquote ломает подпись.
    qs = parse_qs(frag, keep_blank_values=True)
    return qs.get("tgWebAppData", [""])[0]


async def fetch_init_data_variants() -> list[tuple[str, str]]:
    """Возвращает список (label, init_data) — initData, полученные разными методами.
    У @sticker_bot мини-апп может быть привязан к menu/main-menu, и Telegram
    подписывает initData по-разному в зависимости от способа открытия."""
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.start(phone=PHONE)
    bot = await client.get_entity(TARGET)

    variants: list[tuple[str, str]] = []

    async def try_method(label: str, coro):
        try:
            res = await coro
            init = _extract_tgwebappdata(res.url)
            if init:
                variants.append((label, init))
                print(f"  [+] {label}: init_data len={len(init)}")
            else:
                print(f"  [-] {label}: tgWebAppData не найден ({res.url[:120]})")
        except Exception as e:
            print(f"  [-] {label}: {type(e).__name__}: {e}")

    # 1. RequestWebView — соответствует клику по inline-кнопке KeyboardButtonWebView.
    await try_method(
        "WebView (from_bot_menu=False)",
        client(RequestWebViewRequest(peer=bot, bot=bot, platform=PLATFORM, from_bot_menu=False, url=WEBAPP_URL)),
    )
    await try_method(
        "WebView (from_bot_menu=True)",
        client(RequestWebViewRequest(peer=bot, bot=bot, platform=PLATFORM, from_bot_menu=True, url=WEBAPP_URL)),
    )

    # 2. RequestSimpleWebView — для menu-button и side-menu, без peer.
    await try_method(
        "SimpleWebView (default)",
        client(RequestSimpleWebViewRequest(bot=bot, platform=PLATFORM, url=WEBAPP_URL)),
    )
    await try_method(
        "SimpleWebView (from_side_menu=True)",
        client(RequestSimpleWebViewRequest(bot=bot, platform=PLATFORM, url=WEBAPP_URL, from_side_menu=True)),
    )
    await try_method(
        "SimpleWebView (from_switch_webview=True)",
        client(RequestSimpleWebViewRequest(bot=bot, platform=PLATFORM, url=WEBAPP_URL, from_switch_webview=True)),
    )

    await client.disconnect()
    return variants


def looks_like_token(value: str) -> bool:
    return isinstance(value, str) and len(value) > 20 and "." in value or len(value) > 32


def extract_token(payload: dict) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("access_token", "accessToken", "token", "jwt"):
        v = payload.get(key)
        if isinstance(v, str) and v:
            return v
    data = payload.get("data")
    if isinstance(data, dict):
        return extract_token(data)
    return None


async def try_auth(client: httpx.AsyncClient, init_data: str) -> dict | None:
    """Бэк (см. бандл) ждёт POST /api/v1/auth с multipart/form-data полем initData."""
    headers_base = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Origin": "https://stickerdom.store",
        "Referer": "https://stickerdom.store/",
    }
    path = "/api/v1/auth"
    url = f"{API_BASE}{path}"

    # Главный кандидат — то, что реально шлёт фронт.
    candidates = [
        ("multipart:initData", lambda: client.post(url, headers=headers_base, files={"initData": (None, init_data)}, timeout=20)),
        ("form:initData", lambda: client.post(url, headers=headers_base, data={"initData": init_data}, timeout=20)),
        ("multipart:init_data", lambda: client.post(url, headers=headers_base, files={"init_data": (None, init_data)}, timeout=20)),
        ("form:init_data", lambda: client.post(url, headers=headers_base, data={"init_data": init_data}, timeout=20)),
    ]

    for label, do_request in candidates:
        try:
            r = await do_request()
        except httpx.HTTPError as e:
            print(f"  - {path}  [{label}]  → {e}")
            continue
        snippet = r.text[:200].replace("\n", " ")
        print(f"  - {path}  [{label}]  → {r.status_code}  {snippet!r}")
        if r.status_code != 200:
            continue
        try:
            data = r.json()
        except Exception:
            continue
        if isinstance(data, dict) and data.get("ok") is False:
            continue
        token = extract_token(data)
        if token:
            return {"path": path, "label": label, "response": data, "token": token}
    return None


async def main() -> None:
    print(f"[*] Получаю initData через Telethon (peer={TARGET}, url={WEBAPP_URL})…")
    variants = await fetch_init_data_variants()
    if not variants:
        print("[!] Не удалось получить ни одного initData.", file=sys.stderr)
        sys.exit(1)

    print(f"\n[*] Пробую auth на {API_BASE} для {len(variants)} вариантов initData…")
    last_init = ""
    async with httpx.AsyncClient() as http:
        for label, init_data in variants:
            print(f"\n=== initData: {label} ===")
            last_init = init_data
            result = await try_auth(http, init_data)
            if result:
                OUT_FILE.write_text(
                    json.dumps(
                        {
                            "saved_at": datetime.now(timezone.utc).isoformat(),
                            "init_data_method": label,
                            "auth_path": result["path"],
                            "auth_label": result["label"],
                            "init_data": init_data,
                            "token": result["token"],
                            "response": result["response"],
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                print("\n[+] Получили токен!")
                print(f"    initData получен через: {label}")
                print(f"    auth: {result['path']}  [{result['label']}]")
                print(f"    Сохранил в {OUT_FILE}")
                print("\nЗапусти monitor так:")
                print(
                    f"  AUTH_HEADER='Authorization: Bearer {result['token']}' "
                    "python monitor_launches.py"
                )
                return

    print("\n[!] Ни один из вариантов auth не сработал.", file=sys.stderr)
    print(
        "    Сохраню последний initData в auth.json для отладки. "
        "Открой DevTools мини-аппа и сравни запрос /api/v1/auth — "
        "поле и формат initData, заголовки, метод.",
        file=sys.stderr,
    )
    OUT_FILE.write_text(
        json.dumps(
            {
                "init_data": last_init,
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "tried_methods": [v[0] for v in variants],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
