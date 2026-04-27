"""
Дёргает GET /stickerpad/v1/launch/{id} и печатает весь JSON-ответ.
Используется для разовой инспекции схемы — увидеть, под каким именем
бэк держит creator, full_name, username и пр.

Запуск:
    python inspect_launch.py 019dd0ae-3e43-7c5f-9ba5-253b5b77ea15
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

# Импортируем уже готовое управление токеном из монитора, чтобы не дублировать.
from monitor_launches import API_BASE, STICKERPAD, TIMEOUT, TOKENS, build_headers


async def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python inspect_launch.py <launch_id>")
        sys.exit(2)
    launch_id = sys.argv[1]

    TOKENS.load()
    if not TOKENS.token and not TOKENS.locked_to_env:
        print("[*] auth.json пуст, обновляю токен …")
        await TOKENS.refresh()

    url = f"{API_BASE}{STICKERPAD}/launch/{launch_id}"
    print(f"[*] GET {url}")
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=build_headers(), timeout=TIMEOUT)
        if r.status_code in (401, 403) and not TOKENS.locked_to_env:
            print(f"[*] {r.status_code}, обновляю токен …")
            if await TOKENS.refresh():
                r = await client.get(url, headers=build_headers(), timeout=TIMEOUT)
        print(f"[+] {r.status_code}\n")
        try:
            print(json.dumps(r.json(), ensure_ascii=False, indent=2))
        except Exception:
            print(r.text)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
