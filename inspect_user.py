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

    base_v1 = f"{API_BASE}/api/v1"
    print(f"[*] base={base_v1}  id={uid}")

    if not TOKENS.locked_to_env and TOKENS.is_expiring():
        await TOKENS.refresh()

    lookup = f"{base_v1}/user/lookup"
    profile_get = f"{base_v1}/user/{uid}/profile"
    profile_post = f"{base_v1}/user/{uid}/profile"

    candidates = [
        # /user/{id}/profile — самый вероятный путь для лукапа по Telegram id
        ("GET /user/{id}/profile", lambda c: c.get(profile_get, headers=build_headers(), timeout=TIMEOUT)),
        ("POST /user/{id}/profile (no body)", lambda c: c.post(profile_post, headers=build_headers(), timeout=TIMEOUT)),
        # /user/lookup — это поиск для transfer'а, видимо по username/handle
        ("POST /user/lookup json {id: int}", lambda c: c.post(lookup, json={"id": int(uid)}, headers=build_headers(), timeout=TIMEOUT)),
        ("POST /user/lookup form id=...", lambda c: c.post(lookup, data={"id": uid}, headers=build_headers(), timeout=TIMEOUT)),
    ]

    async with httpx.AsyncClient() as client:
        for label, do in candidates:
            try:
                r = await do(client)
            except Exception as e:
                print(f"\n=== {label} ===\n  exception: {e}")
                continue
            if r.status_code in (401, 403) and not TOKENS.locked_to_env:
                if await TOKENS.refresh():
                    r = await do(client)
            print(f"\n=== {label} ===")
            print(f"  status: {r.status_code}")
            ct = r.headers.get("content-type", "")
            print(f"  content-type: {ct}")
            body = r.text
            if "json" in ct:
                try:
                    body = json.dumps(r.json(), ensure_ascii=False, indent=2)
                except Exception:
                    pass
            print("  body:")
            for line in body.splitlines()[:30]:
                print(f"    {line}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
