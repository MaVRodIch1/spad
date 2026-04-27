"""
Исследует структуру Telegram-бота через юзер-бот (Telethon).

Запуск:
    cp .env.example .env  # заполнить TG_API_ID, TG_API_HASH, TG_PHONE
    pip install -r requirements.txt
    python explore_bot.py

Алгоритм:
    1. Отправляет /start целевому боту
    2. BFS-обход меню до глубины MAX_DEPTH
    3. Для каждого состояния заново проигрывает путь от /start
       (так мы не зависим от внутреннего состояния бота)
    4. Сохраняет дерево состояний в structure.json
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import deque
from hashlib import sha256
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.custom import Message

load_dotenv()

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
PHONE = os.environ.get("TG_PHONE")
TARGET = os.environ.get("TG_TARGET", "sticker_bot")
MAX_DEPTH = int(os.environ.get("MAX_DEPTH", "3"))
WAIT_TIMEOUT = float(os.environ.get("WAIT_TIMEOUT", "4.0"))
OUTPUT_FILE = os.environ.get("OUTPUT_FILE", "structure.json")
SESSION = os.environ.get("TG_SESSION", "userbot")


def serialize_button(btn: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "type": type(btn).__name__,
        "text": getattr(btn, "text", None),
    }
    data = getattr(btn, "data", None)
    if data:
        try:
            out["data"] = data.decode("utf-8")
        except UnicodeDecodeError:
            out["data"] = data.hex()
    url = getattr(btn, "url", None)
    if url:
        out["url"] = url
    return out


def serialize_keyboard(message: Message) -> list[list[dict[str, Any]]]:
    rows: list[list[dict[str, Any]]] = []
    if not message.buttons:
        return rows
    for row in message.buttons:
        rows.append([serialize_button(b) for b in row])
    return rows


def state_hash(text: str, keyboard: list[list[dict[str, Any]]]) -> str:
    h = sha256()
    h.update((text or "").encode("utf-8"))
    h.update(json.dumps(keyboard, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return h.hexdigest()[:16]


def capture(message: Message, path: list[list[int]]) -> dict[str, Any]:
    text = message.message or message.raw_text or ""
    keyboard = serialize_keyboard(message)
    return {
        "hash": state_hash(text, keyboard),
        "path": path,
        "text": text,
        "keyboard": keyboard,
        "message_id": message.id,
        "media_type": type(message.media).__name__ if message.media else None,
        "transitions": [],
    }


async def fetch_latest(client: TelegramClient, bot) -> Message:
    """Берёт самое свежее сообщение от бота."""
    messages = await client.get_messages(bot, limit=1)
    if not messages:
        raise RuntimeError("Бот не ответил")
    return messages[0]


async def replay_path(
    client: TelegramClient, bot, path: list[list[int]]
) -> Message:
    """Прогоняет путь из последовательности кликов от /start."""
    await client.send_message(bot, "/start")
    await asyncio.sleep(WAIT_TIMEOUT)
    msg = await fetch_latest(client, bot)

    for i, j in path:
        try:
            await msg.click(i, j)
        except Exception as e:
            print(f"  ! не удалось кликнуть [{i},{j}]: {e}")
            return msg
        await asyncio.sleep(WAIT_TIMEOUT)
        msg = await fetch_latest(client, bot)
    return msg


CLICKABLE = {"KeyboardButtonCallback", "KeyboardButton"}


async def explore(client: TelegramClient, bot) -> dict[str, dict[str, Any]]:
    visited: dict[str, dict[str, Any]] = {}
    queue: deque[dict[str, Any]] = deque()

    print(f"[*] Стартуем исследование @{TARGET}, глубина={MAX_DEPTH}")
    root_msg = await replay_path(client, bot, [])
    root = capture(root_msg, path=[])
    visited[root["hash"]] = root
    queue.append(root)
    print(f"[+] root state {root['hash']}: {root['text'][:80]!r}")

    while queue:
        state = queue.popleft()
        depth = len(state["path"])
        if depth >= MAX_DEPTH:
            continue

        for i, row in enumerate(state["keyboard"]):
            for j, btn in enumerate(row):
                if btn["type"] not in CLICKABLE:
                    state["transitions"].append(
                        {"button": btn, "leads_to": None, "reason": "non-clickable"}
                    )
                    continue

                new_path = state["path"] + [[i, j]]
                print(f"[>] depth={depth + 1} click {btn.get('text')!r} path={new_path}")
                try:
                    new_msg = await replay_path(client, bot, new_path)
                except Exception as e:
                    print(f"  ! ошибка replay: {e}")
                    state["transitions"].append(
                        {"button": btn, "leads_to": None, "reason": str(e)}
                    )
                    continue

                new_state = capture(new_msg, path=new_path)
                state["transitions"].append(
                    {"button": btn, "leads_to": new_state["hash"]}
                )

                if new_state["hash"] not in visited:
                    visited[new_state["hash"]] = new_state
                    queue.append(new_state)
                    print(f"  [+] new state {new_state['hash']}: {new_state['text'][:60]!r}")
                else:
                    print(f"  [=] visited {new_state['hash']}")

    return visited


async def main() -> None:
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.start(phone=PHONE)
    me = await client.get_me()
    print(f"[*] Авторизованы как {me.username or me.first_name} (id={me.id})")

    try:
        bot = await client.get_entity(TARGET)
    except Exception as e:
        raise SystemExit(f"Не нашёл @{TARGET}: {e}")

    states = await explore(client, bot)

    output = {
        "target": TARGET,
        "max_depth": MAX_DEPTH,
        "state_count": len(states),
        "root": next(iter(states)),
        "states": states,
    }
    Path(OUTPUT_FILE).write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[*] Сохранено {len(states)} состояний в {OUTPUT_FILE}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
