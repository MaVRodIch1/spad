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

Авторизация:
  - Если есть auth.json (от get_token.py) — токен подхватится автоматически.
  - При получении 401 монитор сам вызовет get_token.get_fresh_token() и
    обновит auth.json — для этого .env с TG_API_ID/TG_API_HASH/TG_PHONE
    должен быть на месте.
  - Можно явно передать AUTH_HEADER=... — тогда автообновление выключено.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

API_BASE = os.environ.get("API_BASE_URL", "https://api.stickerdom.store")
STICKERPAD = os.environ.get("STICKERPAD_PATH", "/stickerpad/v1")
LIMIT = int(os.environ.get("LIMIT", "20"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "30"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "launches_seen.json"))
LOG_FILE = Path(os.environ.get("LOG_FILE", "launches.jsonl"))
TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "20"))
AUTH_HEADER_ENV = os.environ.get("AUTH_HEADER", "").strip()
AUTH_FILE = Path(os.environ.get("AUTH_OUT", "auth.json"))
# обновлять токен заранее за N секунд до exp
TOKEN_REFRESH_LEAD = int(os.environ.get("TOKEN_REFRESH_LEAD", "120"))

# Уведомления в TG-канал (опционально)
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()  # @channelname или -100...
NOTIFY_BACKLOG = os.environ.get("NOTIFY_BACKLOG", "0") == "1"  # слать в TG для уже виденных при первом запуске

UA = (
    "Mozilla/5.0 (Linux; Android 13; SM-G998B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36 Telegram-Android/10.13"
)


# --- управление токеном ---------------------------------------------------

class TokenStore:
    """Хранит текущий JWT и умеет его обновлять через get_token.get_fresh_token."""

    def __init__(self) -> None:
        self.token: str | None = None
        self.exp: int | None = None
        self.locked_to_env = False

    def jwt_exp(self, jwt: str) -> int | None:
        try:
            _, payload, _ = jwt.split(".")
            payload += "=" * (-len(payload) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload))
            exp = data.get("exp")
            return int(exp) if isinstance(exp, (int, float)) else None
        except Exception:
            return None

    def load(self) -> None:
        # 1) Явный AUTH_HEADER в env — значит юзер сам управляет, авторефреш off
        if AUTH_HEADER_ENV and ":" in AUTH_HEADER_ENV:
            name, _, value = AUTH_HEADER_ENV.partition(":")
            value = value.strip()
            if name.strip().lower() == "authorization" and value.lower().startswith("bearer "):
                self.token = value.split(None, 1)[1]
            else:
                self.token = value
            self.exp = self.jwt_exp(self.token) if self.token else None
            self.locked_to_env = True
            return

        # 2) Иначе пробуем auth.json
        if AUTH_FILE.exists():
            try:
                data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
                tok = data.get("token")
                if isinstance(tok, str) and tok:
                    self.token = tok
                    self.exp = self.jwt_exp(tok)
            except Exception as e:
                print(f"[!] Не смог прочитать {AUTH_FILE}: {e}", file=sys.stderr)

    def is_expiring(self) -> bool:
        if not self.token or not self.exp:
            return False
        return time.time() >= self.exp - TOKEN_REFRESH_LEAD

    async def refresh(self) -> bool:
        if self.locked_to_env:
            print("[!] Токен задан явно через AUTH_HEADER, автообновление отключено.", file=sys.stderr)
            return False
        try:
            from get_token import get_fresh_token
        except ImportError as e:
            print(f"[!] Не нашёл get_token.py: {e}", file=sys.stderr)
            return False
        try:
            print(f"[*] Обновляю токен через get_token.get_fresh_token() …")
            new_token = await get_fresh_token(verbose=False)
            self.token = new_token
            self.exp = self.jwt_exp(new_token)
            print(f"[+] Новый токен получен (exp={self.exp})")
            return True
        except Exception as e:
            print(f"[!] Не удалось обновить токен: {e}", file=sys.stderr)
            return False


TOKENS = TokenStore()


def build_headers() -> dict[str, str]:
    h = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Origin": "https://stickerdom.store",
        "Referer": "https://stickerdom.store/",
    }
    if TOKENS.token:
        h["Authorization"] = f"Bearer {TOKENS.token}"
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


async def fetch_launch_detail(client: httpx.AsyncClient, launch_id: str) -> dict | None:
    """GET /stickerpad/v1/launch/{id} — там обычно полная инфа, включая creator."""
    if not launch_id:
        return None
    url = f"{API_BASE}{STICKERPAD}/launch/{launch_id}"

    if not TOKENS.locked_to_env and TOKENS.is_expiring():
        await TOKENS.refresh()

    r = await client.get(url, headers=build_headers(), timeout=TIMEOUT)
    if r.status_code in (401, 403) and not TOKENS.locked_to_env:
        if await TOKENS.refresh():
            r = await client.get(url, headers=build_headers(), timeout=TIMEOUT)
    if r.status_code != 200:
        print(f"[!] /launch/{launch_id} -> {r.status_code}", file=sys.stderr)
        return None
    try:
        data = r.json()
    except Exception:
        return None
    if isinstance(data, dict) and data.get("ok") is True and "data" in data:
        return data["data"] if isinstance(data["data"], dict) else None
    return data if isinstance(data, dict) else None


def merge_detail_into_summary(s: dict[str, Any], detail: dict | None) -> None:
    """Дописывает в summary creator/прочее из ответа /launch/{id}."""
    if not isinstance(detail, dict):
        return
    # Возможные места, где лежит creator: detail.creator, detail.launch_info.creator,
    # detail.launch_info.launch.creator, detail.user, detail.owner
    candidates = []
    candidates.append(detail.get("creator"))
    candidates.append(detail.get("user"))
    candidates.append(detail.get("owner"))
    li = detail.get("launch_info")
    if isinstance(li, dict):
        candidates.append(li.get("creator"))
        candidates.append(li.get("user"))
        launch = li.get("launch")
        if isinstance(launch, dict):
            candidates.append(launch.get("creator"))
            candidates.append(launch.get("user"))
    creator = next((c for c in candidates if isinstance(c, dict) and c), None)
    if creator:
        s["creator"] = {
            "id": first(creator, "id", "user_id", "telegram_id"),
            "full_name": first(creator, "full_name", "name"),
            "username": first(creator, "username"),
        }
    # Если в деталях есть более точный contract/start_price — обновим
    li = detail.get("launch_info") if isinstance(detail.get("launch_info"), dict) else {}
    launch = li.get("launch") if isinstance(li, dict) else None
    if isinstance(launch, dict):
        s["contract_address"] = first(launch, "contract_address") or s.get("contract_address")
        s["start_price"] = first(launch, "start_price") or s.get("start_price")
        s["end_price"] = first(launch, "end_price") or s.get("end_price")


async def fetch_launches(client: httpx.AsyncClient) -> list[dict]:
    url = f"{API_BASE}{STICKERPAD}/launch"
    params = {"approved": "true", "limit": str(LIMIT)}

    # Проактивный refresh, если до exp осталось мало
    if not TOKENS.locked_to_env and TOKENS.is_expiring():
        await TOKENS.refresh()

    r = await client.get(url, params=params, headers=build_headers(), timeout=TIMEOUT)
    if r.status_code in (401, 403):
        # Реактивный refresh: попробуем обновить токен и повторить запрос
        if not TOKENS.locked_to_env:
            print(f"[*] Получили {r.status_code}, пытаюсь обновить токен …")
            if await TOKENS.refresh():
                r = await client.get(url, params=params, headers=build_headers(), timeout=TIMEOUT)
        if r.status_code in (401, 403):
            raise PermissionError(
                f"{r.status_code} {r.reason_phrase}: эндпоинт требует авторизацию. "
                "Запусти `python get_token.py`, либо передай AUTH_HEADER явно."
            )
    r.raise_for_status()
    data = r.json()
    # Stickerdom оборачивает успешный ответ как {"ok":true,"data":{...}}
    if isinstance(data, dict) and data.get("ok") is True and "data" in data:
        data = data["data"]

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


def html_escape(s: Any) -> str:
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def format_tg_message(s: dict[str, Any]) -> str:
    coll = s["collection"]
    creator = s["creator"]
    name = html_escape(s["name"]) or "—"
    coll_title = html_escape(coll.get("title")) or "—"
    coll_id = html_escape(coll.get("id"))
    cu = creator.get("username")
    cn = html_escape(creator.get("full_name")) or "—"
    cid = html_escape(creator.get("id"))
    creator_line = f"@{html_escape(cu)} ({cn})" if cu else cn
    if cid:
        creator_line += f" • <code>{cid}</code>"
    status = html_escape(s["status"]) or "—"
    start = html_escape(s["start_time"])
    contract = s.get("contract_address")
    url = s.get("url") or ""

    parts = [
        "🆕 <b>New Sticker Pack launch</b>",
        f"<b>Pack:</b> {name}",
        f"<b>Collection:</b> {coll_title}" + (f" (id <code>{coll_id}</code>)" if coll_id else ""),
        f"<b>Creator:</b> {creator_line}",
        f"<b>Status:</b> {status}   <b>Start:</b> {start}",
    ]
    if contract:
        parts.append(f"<b>Contract:</b> <code>{html_escape(contract)}</code>")
    if url:
        parts.append(f'<a href="{html_escape(url)}">Open in Sticker Pack</a>')
    return "\n".join(parts)


async def notify_telegram(client: httpx.AsyncClient, s: dict[str, Any]) -> None:
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    text = format_tg_message(s)
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        r = await client.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            print(f"[!] TG notify failed {r.status_code}: {r.text[:300]}", file=sys.stderr)
    except httpx.HTTPError as e:
        print(f"[!] TG notify HTTP error: {e}", file=sys.stderr)


async def run_once(client: httpx.AsyncClient, seen: set[str], first_run: bool) -> int:
    items = await fetch_launches(client)
    new_count = 0
    will_notify = (not first_run) or NOTIFY_BACKLOG
    for item in items:
        s = extract_summary(item)
        lid = str(s["launch_id"]) if s["launch_id"] is not None else None
        if not lid or lid in seen:
            continue
        seen.add(lid)
        new_count += 1
        # Если creator отсутствует — дотягиваем детали через /launch/{id}.
        if not s["creator"].get("username") and not s["creator"].get("full_name"):
            try:
                detail = await fetch_launch_detail(client, lid)
                merge_detail_into_summary(s, detail)
            except Exception as e:
                print(f"[!] detail({lid}): {e}", file=sys.stderr)
        print_launch(s)
        append_log(s)
        if will_notify:
            await notify_telegram(client, s)
            # лёгкий троттлинг, чтобы не словить 429 при backlog-рассылке
            if NOTIFY_BACKLOG and first_run:
                await asyncio.sleep(0.7)
    save_seen(seen)
    return new_count


async def main() -> None:
    seen = load_seen()
    TOKENS.load()
    if not TOKENS.token and not TOKENS.locked_to_env:
        print("[*] auth.json пуст и AUTH_HEADER не задан — пробую получить токен сразу.")
        await TOKENS.refresh()
    tg_status = "ON" if (TG_BOT_TOKEN and TG_CHAT_ID) else "OFF"
    auth_status = (
        "env" if TOKENS.locked_to_env
        else f"file (exp={TOKENS.exp})" if TOKENS.token
        else "none"
    )
    print(
        f"[*] base={API_BASE}{STICKERPAD}  limit={LIMIT}  interval={POLL_INTERVAL}s  "
        f"seen={len(seen)}  tg_notify={tg_status}  auth={auth_status}"
    )
    first_run = len(seen) == 0
    async with httpx.AsyncClient() as client:
        while True:
            try:
                new = await run_once(client, seen, first_run)
                first_run = False
                if new == 0:
                    print(
                        f"[.] {datetime.now().strftime('%H:%M:%S')}  no new launches  "
                        f"(total seen: {len(seen)})",
                        flush=True,
                    )
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
