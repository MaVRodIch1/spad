"""
Мониторит новые запуски паков на Stickerdom Stickerpad.

Поллит GET /stickerpad/v1/launch?approved=true&limit=N, сохраняет уже виденные
launch_id в state-файле, для каждого нового пишет в консоль и в JSONL-лог:
  - id / название лаунча
  - коллекция (id, title)
  - создатель (id, full_name, username)
  - цены, статус, время старта
  - ссылка вида https://stickerdom.store/launch-item/<id>

Запуск:
    python monitor_launches.py
    # один проход, без цикла:
    POLL_INTERVAL=0 python monitor_launches.py

Если эндпоинт потребует авторизацию, прокинь хедер через env:
    AUTH_HEADER='Authorization: Bearer <token>'   (или X-Telegram-Init-Data: ...)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

API_BASE = os.environ.get("API_BASE_URL", "https://api.stickerdom.store")
STICKERPAD = os.environ.get("STICKERPAD_PATH", "/stickerpad/v1")
LIMIT = int(os.environ.get("LIMIT", "20"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "30"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "launches_seen.json"))
LOG_FILE = Path(os.environ.get("LOG_FILE", "launches.jsonl"))
TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "20"))
AUTH_HEADER = os.environ.get("AUTH_HEADER", "").strip()

UA = (
    "Mozilla/5.0 (Linux; Android 13; SM-G998B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36 Telegram-Android/10.13"
)


def build_headers() -> dict[str, str]:
    h = {"User-Agent": UA, "Accept": "application/json", "Origin": "https://stickerdom.store"}
    if AUTH_HEADER and ":" in AUTH_HEADER:
        name, _, value = AUTH_HEADER.partition(":")
        h[name.strip()] = value.strip()
    return h


def load_seen() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def save_seen(seen: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(seen)), encoding="utf-8")


def first(d: dict | None, *keys: str, default: Any = None) -> Any:
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def fmt_time(ts: Any) -> str:
    if ts is None:
        return "-"
    try:
        if isinstance(ts, (int, float)):
            t = float(ts)
            if t > 1e12:
                t /= 1000
            return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()
        if isinstance(ts, str):
            return ts
    except Exception:
        pass
    return str(ts)


def extract_summary(item: dict) -> dict[str, Any]:
    """Достаёт ключевые поля из элемента списка, не зная точной схемы."""
    launch = first(item, "launch", default={}) or {}
    if not isinstance(launch, dict):
        launch = {}
    # id может лежать на верхнем уровне, в launch, в character, и т.п.
    launch_id = first(launch, "id") or first(item, "launch_id", "id")
    name = first(launch, "name", "title") or first(item, "name", "title")
    status = first(launch, "status") or first(item, "status")
    start_time = first(launch, "start_time") or first(item, "start_time", "starts_at")
    start_price = first(launch, "start_price") or first(item, "start_price")
    end_price = first(launch, "end_price") or first(item, "end_price")
    contract_addr = first(launch, "contract_address") or first(item, "contract_address")

    collection = first(item, "collection", default={}) or {}
    if not isinstance(collection, dict):
        collection = {}
    character = first(item, "character", default={}) or {}
    if not isinstance(character, dict):
        character = {}
    creator = (
        first(item, "creator", default=None)
        or first(launch, "creator", default=None)
        or first(item, "owner", default=None)
        or {}
    )
    if not isinstance(creator, dict):
        creator = {}

    return {
        "launch_id": launch_id,
        "name": name,
        "status": status,
        "start_time": fmt_time(start_time),
        "start_price": start_price,
        "end_price": end_price,
        "contract_address": contract_addr,
        "collection": {
            "id": first(collection, "id"),
            "title": first(collection, "title", "name"),
            "badges": first(collection, "badges"),
        },
        "character": {
            "id": first(character, "id"),
            "name": first(character, "name"),
        },
        "creator": {
            "id": first(creator, "id", "user_id", "telegram_id"),
            "full_name": first(creator, "full_name", "name"),
            "username": first(creator, "username"),
        },
        "url": f"https://stickerdom.store/launch-item/{launch_id}" if launch_id else None,
    }


async def fetch_launches(client: httpx.AsyncClient) -> list[dict]:
    url = f"{API_BASE}{STICKERPAD}/launch"
    params = {"approved": "true", "limit": str(LIMIT)}
    r = await client.get(url, params=params, timeout=TIMEOUT)
    if r.status_code == 401 or r.status_code == 403:
        raise PermissionError(
            f"{r.status_code} {r.reason_phrase}: эндпоинт требует авторизацию. "
            "Передай AUTH_HEADER='Authorization: Bearer <token>' или "
            "'X-Telegram-Init-Data: <initData>'."
        )
    r.raise_for_status()
    data = r.json()

    # Возможные форматы: {launches: [...]}, {pages: [{launches: [...]}, ...]}, {data: [...]}, [...]
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("launches"), list):
            return data["launches"]
        if isinstance(data.get("data"), list):
            return data["data"]
        if isinstance(data.get("items"), list):
            return data["items"]
        if isinstance(data.get("pages"), list):
            out: list[dict] = []
            for p in data["pages"]:
                if isinstance(p, dict) and isinstance(p.get("launches"), list):
                    out.extend(p["launches"])
            return out
    print(f"[!] неизвестный формат ответа, ключи: {list(data.keys()) if isinstance(data, dict) else type(data)}")
    return []


def print_launch(s: dict[str, Any]) -> None:
    coll = s["collection"]
    creator = s["creator"]
    print(
        f"[+] NEW LAUNCH  id={s['launch_id']}  status={s['status']}  start={s['start_time']}\n"
        f"    name:       {s['name']!r}\n"
        f"    collection: id={coll['id']}  title={coll['title']!r}  badges={coll['badges']}\n"
        f"    creator:    id={creator['id']}  username=@{creator['username']}  name={creator['full_name']!r}\n"
        f"    contract:   {s['contract_address']}\n"
        f"    url:        {s['url']}",
        flush=True,
    )


def append_log(s: dict[str, Any]) -> None:
    line = json.dumps({"seen_at": datetime.now(timezone.utc).isoformat(), **s}, ensure_ascii=False)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


async def run_once(client: httpx.AsyncClient, seen: set[str]) -> int:
    items = await fetch_launches(client)
    new_count = 0
    for item in items:
        s = extract_summary(item)
        lid = str(s["launch_id"]) if s["launch_id"] is not None else None
        if not lid or lid in seen:
            continue
        seen.add(lid)
        new_count += 1
        print_launch(s)
        append_log(s)
    save_seen(seen)
    return new_count


async def main() -> None:
    seen = load_seen()
    print(f"[*] base={API_BASE}{STICKERPAD}  limit={LIMIT}  interval={POLL_INTERVAL}s  seen={len(seen)}")
    async with httpx.AsyncClient(headers=build_headers()) as client:
        while True:
            try:
                new = await run_once(client, seen)
                if new == 0:
                    print(f"[.] {datetime.now().strftime('%H:%M:%S')}  no new launches  (total seen: {len(seen)})", flush=True)
            except PermissionError as e:
                print(f"[!] {e}", file=sys.stderr)
                return
            except httpx.HTTPError as e:
                print(f"[!] HTTP error: {e}", file=sys.stderr)
            except Exception as e:
                print(f"[!] {type(e).__name__}: {e}", file=sys.stderr)

            if POLL_INTERVAL <= 0:
                return
            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
