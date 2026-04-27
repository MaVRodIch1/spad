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
    print(f"[*] target: {url}  id={uid}")

    if not TOKENS.locked_to_env and TOKENS.is_expiring():
        await TOKENS.refresh()

    candidates = [
        ("POST query ?id=", lambda c: c.post(url, params={"id": uid}, headers=build_headers(), timeout=TIMEOUT)),
        ("POST json {id: int}", lambda c: c.post(url, json={"id": int(uid)}, headers=build_headers(), timeout=TIMEOUT)),
        ("POST json {id: str}", lambda c: c.post(url, json={"id": str(uid)}, headers=build_headers(), timeout=TIMEOUT)),
        ("POST json {user_id: int}", lambda c: c.post(url, json={"user_id": int(uid)}, headers=build_headers(), timeout=TIMEOUT)),
        ("POST form id=...", lambda c: c.post(url, data={"id": uid}, headers=build_headers(), timeout=TIMEOUT)),
        ("POST raw body 'id=...'", lambda c: c.post(url, content=f"id={uid}", headers={**build_headers(), "Content-Type": "application/x-www-form-urlencoded"}, timeout=TIMEOUT)),
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
