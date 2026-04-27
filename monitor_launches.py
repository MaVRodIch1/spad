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

# Telethon мы импортируем лениво — он нужен только для резолва username по id.
_telethon_client = None
_telethon_lock = asyncio.Lock()
USER_CACHE_FILE = Path(os.environ.get("USER_CACHE_FILE", "users_cache.json"))

API_BASE = os.environ.get("API_BASE_URL", "https://api.stickerdom.store")
STICKERPAD = os.environ.get("STICKERPAD_PATH", "/stickerpad/v1")
# Имя бота, в чей мини-апп шарим лаунч. Используется для t.me/<bot>?startapp=lid_<id>
TARGET_BOT = os.environ.get("TG_TARGET", "sticker_bot")
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
        "url": f"https://t.me/{TARGET_BOT}?startapp=lid_{launch_id}" if launch_id else None,
        "web_url": f"https://stickerdom.store/launch-item/{launch_id}" if launch_id else None,
    }


async def fetch_user_lookup(client: httpx.AsyncClient, user_id: Any) -> dict | None:
    """GET /api/v1/user/lookup?id=<id> — Stickerdom отдаёт username и имя по числовому id."""
    if user_id is None:
        return None
    try:
        uid = int(user_id)
    except Exception:
        return None
    url = f"{API_BASE}/api/v1/user/lookup"

    if not TOKENS.locked_to_env and TOKENS.is_expiring():
        await TOKENS.refresh()

    r = await client.get(url, params={"id": str(uid)}, headers=build_headers(), timeout=TIMEOUT)
    if r.status_code in (401, 403) and not TOKENS.locked_to_env:
        if await TOKENS.refresh():
            r = await client.get(url, params={"id": str(uid)}, headers=build_headers(), timeout=TIMEOUT)
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except Exception:
        return None
    if isinstance(data, dict) and data.get("ok") is True and "data" in data:
        body = data["data"]
        return body if isinstance(body, dict) else None
    return data if isinstance(data, dict) else None


def find_preview_url(detail: dict | None) -> str | None:
    """Ищет первую картинку (preview > thumbnail) среди стикеров пака."""
    if not isinstance(detail, dict):
        return None

    def walk(obj: Any, want: str) -> str | None:
        if isinstance(obj, dict):
            media = obj.get("media")
            if isinstance(media, list):
                for m in media:
                    if isinstance(m, dict) and m.get("type") == want:
                        url = m.get("url")
                        if isinstance(url, str) and url:
                            return url
            for v in obj.values():
                r = walk(v, want)
                if r:
                    return r
        elif isinstance(obj, list):
            for v in obj:
                r = walk(v, want)
                if r:
                    return r
        return None

    for want in ("preview", "thumbnail"):
        url = walk(detail, want)
        if url:
            return url
    return None


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


def _load_user_cache() -> dict[str, dict[str, Any]]:
    if not USER_CACHE_FILE.exists():
        return {}
    try:
        return json.loads(USER_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_user_cache(cache: dict[str, dict[str, Any]]) -> None:
    try:
        USER_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[!] cache save: {e}", file=sys.stderr)


_user_cache: dict[str, dict[str, Any]] | None = None


async def resolve_username(user_id: Any) -> dict[str, Any] | None:
    """По числовому Telegram user_id возвращает {id, username, full_name}.
    Использует юзер-бот Telethon (та же сессия что у get_token.py).
    Пробует несколько методов, чтобы вытащить инфу о юзерах, которых
    юзербот никогда не встречал."""
    global _telethon_client, _user_cache
    if user_id is None:
        return None
    try:
        uid = int(user_id)
    except Exception:
        return None
    key = str(uid)

    if _user_cache is None:
        _user_cache = _load_user_cache()
    if key in _user_cache:
        return _user_cache[key]

    try:
        from telethon import TelegramClient
        from telethon.tl.functions.users import GetFullUserRequest, GetUsersRequest
        from telethon.tl.types import InputUser, PeerUser
    except ImportError:
        return None

    api_id = os.environ.get("TG_API_ID")
    api_hash = os.environ.get("TG_API_HASH")
    if not api_id or not api_hash:
        return None

    async with _telethon_lock:
        if _telethon_client is None:
            client = TelegramClient(
                os.environ.get("TG_SESSION", "userbot"),
                int(api_id),
                api_hash,
            )
            await client.start(phone=os.environ.get("TG_PHONE"))
            _telethon_client = client

        entity = None
        # 1) Самый простой и быстрый — get_entity по числу
        try:
            entity = await _telethon_client.get_entity(uid)
        except Exception:
            entity = None
        # 2) Через PeerUser
        if entity is None:
            try:
                entity = await _telethon_client.get_entity(PeerUser(uid))
            except Exception:
                entity = None
        # 3) GetUsersRequest с access_hash=0 — иногда выезжает для публичных
        if entity is None:
            try:
                users = await _telethon_client(GetUsersRequest(id=[InputUser(uid, 0)]))
                if users and users[0].__class__.__name__ != "UserEmpty":
                    entity = users[0]
            except Exception:
                entity = None
        # 4) Полный fetch — может подтянуть кэш в сессию
        if entity is None:
            try:
                full = await _telethon_client(GetFullUserRequest(InputUser(uid, 0)))
                if full and getattr(full, "users", None):
                    entity = full.users[0]
            except Exception:
                entity = None

    if entity is None:
        info: dict[str, Any] = {"id": uid, "username": None, "full_name": None}
    else:
        first_name = getattr(entity, "first_name", None) or ""
        last_name = getattr(entity, "last_name", None) or ""
        full = (first_name + " " + last_name).strip() or None
        info = {
            "id": uid,
            "username": getattr(entity, "username", None),
            "full_name": full,
        }
    _user_cache[key] = info
    _save_user_cache(_user_cache)
    return info


def merge_detail_into_summary(s: dict[str, Any], detail: dict | None) -> None:
    """Дописывает в summary creator/прочее из ответа /launch/{id}.

    Реальная схема Stickerpad (увидено через inspect_launch.py):
        data.launch_info.launch.creator          (int — Telegram user_id)
        data.launch_info.launch.creator_address  (str — TON-кошелёк)
        data.launch_info.launch.name / description / contract_address / ...
    """
    if not isinstance(detail, dict):
        return

    li = detail.get("launch_info") if isinstance(detail.get("launch_info"), dict) else {}
    launch = li.get("launch") if isinstance(li, dict) else None

    # Главный путь: launch.creator как число
    creator_id: Any = None
    if isinstance(launch, dict):
        c = launch.get("creator")
        if isinstance(c, (int, str)):
            creator_id = c
        elif isinstance(c, dict):
            creator_id = first(c, "id", "user_id", "telegram_id")
            s["creator"]["full_name"] = first(c, "full_name", "name") or s["creator"].get("full_name")
            s["creator"]["username"] = first(c, "username") or s["creator"].get("username")

    # Запасные места — если бэк когда-нибудь начнёт класть иначе
    if creator_id is None:
        for src in (detail.get("creator"), detail.get("user"), detail.get("owner"),
                    li.get("creator"), li.get("user")):
            if isinstance(src, (int, str)):
                creator_id = src
                break
            if isinstance(src, dict):
                cid = first(src, "id", "user_id", "telegram_id")
                if cid is not None:
                    creator_id = cid
                    s["creator"]["full_name"] = first(src, "full_name", "name") or s["creator"].get("full_name")
                    s["creator"]["username"] = first(src, "username") or s["creator"].get("username")
                    break

    if creator_id is not None:
        s["creator"]["id"] = creator_id

    if isinstance(launch, dict):
        s["contract_address"] = launch.get("contract_address") or s.get("contract_address")
        s["start_price"] = launch.get("start_price") or s.get("start_price")
        s["end_price"] = launch.get("end_price") or s.get("end_price")
        # description пригодится для сообщения
        if launch.get("description"):
            s["description"] = launch["description"]
        if launch.get("creator_address"):
            s["creator_address"] = launch["creator_address"]
        # имя лаунча
        if launch.get("name") and not s.get("name"):
            s["name"] = launch["name"]


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
    uname = f"@{creator['username']}" if creator.get("username") else "—"
    print(
        f"[+] NEW LAUNCH  id={s['launch_id']}  status={s['status']}  start={s['start_time']}\n"
        f"    name:       {s['name']!r}\n"
        f"    description:{s.get('description')!r}\n"
        f"    collection: id={coll['id']}  title={coll['title']!r}  badges={coll['badges']}\n"
        f"    creator:    id={creator.get('id')}  username={uname}  name={creator.get('full_name')!r}\n"
        f"    creator_w:  {s.get('creator_address')}\n"
        f"    contract:   {s.get('contract_address')}\n"
        f"    tg_url:     {s.get('url')}\n"
        f"    web_url:    {s.get('web_url')}\n"
        f"    preview:    {s.get('preview_url')}",
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
    description = html_escape(s.get("description"))
    coll_title = html_escape(coll.get("title")) or "—"
    coll_id = html_escape(coll.get("id"))
    cu = creator.get("username")
    cn = html_escape(creator.get("full_name"))
    cid = html_escape(creator.get("id"))
    if cu:
        creator_line = f'<a href="https://t.me/{html_escape(cu)}">@{html_escape(cu)}</a>'
        if cn:
            creator_line += f" ({cn})"
    elif cn:
        creator_line = cn
    elif cid:
        creator_line = f"<code>{cid}</code>"
    else:
        creator_line = "—"

    status = html_escape(s["status"]) or "—"
    start = html_escape(s["start_time"])
    contract = s.get("contract_address")
    creator_addr = s.get("creator_address")
    url = s.get("url") or ""

    parts = [
        "🆕 <b>New Sticker Pack launch</b>",
        f"<b>Pack:</b> {name}",
    ]
    if description:
        parts.append(f"<i>{description}</i>")
    parts += [
        f"<b>Collection:</b> {coll_title}" + (f" (id <code>{coll_id}</code>)" if coll_id else ""),
        f"<b>Creator:</b> {creator_line}",
        f"<b>Status:</b> {status}   <b>Start:</b> {start}",
    ]
    if creator_addr:
        parts.append(f"<b>Creator wallet:</b> <code>{html_escape(creator_addr)}</code>")
    if contract:
        parts.append(f"<b>Contract:</b> <code>{html_escape(contract)}</code>")
    web_url = s.get("web_url") or ""
    if url:
        parts.append(f'<a href="{html_escape(url)}">▶ Open in Telegram</a>')
    if web_url:
        parts.append(f'<a href="{html_escape(web_url)}">Open in browser</a>')
    return "\n".join(parts)


async def notify_telegram(client: httpx.AsyncClient, s: dict[str, Any]) -> None:
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    text = format_tg_message(s)
    preview = s.get("preview_url")

    # 1) Если есть превью и текст помещается в caption (1024 знака) — sendPhoto.
    if preview and len(text) <= 1024:
        photo_payload = {
            "chat_id": TG_CHAT_ID,
            "photo": preview,
            "caption": text,
            "parse_mode": "HTML",
        }
        try:
            r = await client.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto",
                json=photo_payload,
                timeout=TIMEOUT,
            )
            if r.status_code == 200:
                return
            print(f"[!] sendPhoto failed {r.status_code}: {r.text[:300]}", file=sys.stderr)
        except httpx.HTTPError as e:
            print(f"[!] sendPhoto HTTP error: {e}", file=sys.stderr)
        # упадём на sendMessage, чтобы хоть текст ушёл

    # 2) Без превью или caption не влез — обычный sendMessage.
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": preview is None,
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
        # Список /launch не отдаёт creator — всегда тянем детали через /launch/{id}.
        detail: dict | None = None
        try:
            detail = await fetch_launch_detail(client, lid)
            merge_detail_into_summary(s, detail)
        except Exception as e:
            print(f"[!] detail({lid}): {e}", file=sys.stderr)
        # Превью — первая картинка из стикеров пака
        if detail and not s.get("preview_url"):
            s["preview_url"] = find_preview_url(detail)
        # Резолвим username/имя в первую очередь через Stickerdom /user/lookup,
        # затем (если не отдал) — через юзер-бот Telethon.
        if s["creator"].get("id") and not s["creator"].get("username"):
            try:
                u = await fetch_user_lookup(client, s["creator"]["id"])
                if u:
                    if not s["creator"].get("username"):
                        s["creator"]["username"] = u.get("username") or first(u, "user_name", "handle")
                    if not s["creator"].get("full_name"):
                        s["creator"]["full_name"] = u.get("full_name") or first(u, "name", "first_name")
            except Exception as e:
                print(f"[!] /user/lookup: {e}", file=sys.stderr)
        if s["creator"].get("id") and not s["creator"].get("username"):
            try:
                info = await resolve_username(s["creator"]["id"])
                if info:
                    s["creator"]["username"] = info.get("username") or s["creator"].get("username")
                    if not s["creator"].get("full_name"):
                        s["creator"]["full_name"] = info.get("full_name")
            except Exception as e:
                print(f"[!] resolve creator: {e}", file=sys.stderr)
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


async def _entrypoint() -> None:
    try:
        await main()
    finally:
        if _telethon_client is not None:
            try:
                await _telethon_client.disconnect()
            except Exception:
                pass


if __name__ == "__main__":
    try:
        asyncio.run(_entrypoint())
    except KeyboardInterrupt:
        pass
