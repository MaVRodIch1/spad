"""
Дёргает GET /api/v1/user/lookup?id=<id> и печатает весь JSON-ответ.
Нужен, чтобы увидеть точную схему — под какими ключами Stickerdom-бэк
держит username и имя юзера.

Запуск:
    python inspect_user.py 298242542
    python inspect_user.py 856282296
    python inspect_user.py 7720579758
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

from monitor_launches import API_BASE, TIMEOUT, TOKENS, build_headers


async def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python inspect_user.py <user_id>")
        sys.exit(2)
    uid = sys.argv[1]

    TOKENS.load()
    if not TOKENS.token and not TOKENS.locked_to_env:
        print("[*] auth.json пуст, обновляю токен …")
        await TOKENS.refresh()

    url = f"{API_BASE}/api/v1/user/lookup"
    print(f"[*] GET {url}?id={uid}")
    async with httpx.AsyncClient() as client:
        r = await client.get(url, params={"id": uid}, headers=build_headers(), timeout=TIMEOUT)
        if r.status_code in (401, 403) and not TOKENS.locked_to_env:
            print(f"[*] {r.status_code}, обновляю токен …")
            if await TOKENS.refresh():
                r = await client.get(url, params={"id": uid}, headers=build_headers(), timeout=TIMEOUT)
        print(f"[+] {r.status_code}\n")
        print("-- Headers --")
        for k, v in r.headers.items():
            if k.lower() in {"content-type", "content-length", "x-request-id"}:
                print(f"  {k}: {v}")
        print("-- Body --")
        try:
            print(json.dumps(r.json(), ensure_ascii=False, indent=2))
        except Exception:
            print(r.text)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
