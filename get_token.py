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
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.functions.messages import RequestWebViewRequest

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


async def fetch_init_data() -> str:
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.start(phone=PHONE)
    bot = await client.get_entity(TARGET)
    res = await client(
        RequestWebViewRequest(
            peer=bot,
            bot=bot,
            platform=PLATFORM,
            from_bot_menu=False,
            url=WEBAPP_URL,
        )
    )
    await client.disconnect()

    parsed = urlparse(res.url)
    # Telegram кладёт параметры во fragment: ...#tgWebAppData=...&tgWebAppVersion=...
    frag = parsed.fragment or parsed.query
    qs = parse_qs(frag, keep_blank_values=True)
    raw = qs.get("tgWebAppData", [""])[0]
    if not raw:
        raise RuntimeError(f"tgWebAppData не найден в URL: {res.url}")
    # tgWebAppData приходит уже urlencoded — это "query_id=...&user=...&auth_date=...&hash=..."
    return unquote(raw)


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
    headers_base = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Origin": "https://stickerdom.store",
        "Referer": "https://stickerdom.store/",
    }
    candidates = [
        # path, json body, extra headers
        ("/api/v1/auth", {"init_data": init_data}, {}),
        ("/api/v1/auth", {"initData": init_data}, {}),
        ("/api/v1/auth", {"data": init_data}, {}),
        ("/api/v1/auth", None, {"X-Telegram-Init-Data": init_data, "Telegram-Init-Data": init_data}),
        ("/api/v1/auth/login", {"init_data": init_data}, {}),
        ("/api/v1/auth/telegram", {"init_data": init_data}, {}),
    ]
    for path, body, extra in candidates:
        url = f"{API_BASE}{path}"
        headers = {**headers_base, **extra}
        try:
            if body is None:
                r = await client.post(url, headers=headers, timeout=20)
            else:
                r = await client.post(url, json=body, headers=headers, timeout=20)
        except httpx.HTTPError as e:
            print(f"  - {path}  body={list(body) if body else 'header'}  → {e}")
            continue
        snippet = r.text[:200].replace("\n", " ")
        print(f"  - {path}  body={list(body) if body else 'header'}  → {r.status_code}  {snippet!r}")
        if r.status_code != 200:
            continue
        try:
            data = r.json()
        except Exception:
            continue
        token = extract_token(data)
        if token:
            return {"path": path, "body_keys": list(body) if body else None, "headers": list(extra), "response": data, "token": token}
    return None


async def main() -> None:
    print(f"[*] Получаю initData через Telethon (peer={TARGET}, url={WEBAPP_URL})…")
    init_data = await fetch_init_data()
    print(f"[+] initData: {init_data[:80]}…  (len={len(init_data)})")

    print(f"[*] Пробую auth на {API_BASE}…")
    async with httpx.AsyncClient() as http:
        result = await try_auth(http, init_data)

    if not result:
        print("\n[!] Ни один из вариантов auth не сработал.", file=sys.stderr)
        print(
            "    Открой DevTools мини-аппа https://stickerdom.store, найди в Network "
            "запрос, который отдаёт access_token, и скажи мне его путь/формат — "
            "добавлю.",
            file=sys.stderr,
        )
        OUT_FILE.write_text(
            json.dumps({"init_data": init_data, "saved_at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        sys.exit(1)

    OUT_FILE.write_text(
        json.dumps(
            {
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "auth_path": result["path"],
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
    print(f"    Сохранил в {OUT_FILE}")
    print("\nЗапусти monitor так:")
    print(f"  AUTH_HEADER='Authorization: Bearer {result['token']}' python monitor_launches.py")


if __name__ == "__main__":
    asyncio.run(main())
