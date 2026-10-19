"""Логика рассылки: слежение за YouTube RSS и комментарии в Telegram-каналах."""
import asyncio
import json
import os
import random
import re
import urllib.request
from pathlib import Path
from typing import Callable

import feedparser
from telethon import TelegramClient, errors

DEFAULT_TEMPLATE = "Новый ролик: {title}\n{url}"
DEFAULT_CONFIG = {
    "api_id": "",
    "api_hash": "",
    "youtube": "",
    "template": DEFAULT_TEMPLATE,
    "channels": "",
    "check_minutes": 5,
    "delay_min": 30,
    "delay_max": 90,
}


def data_dir() -> Path:
    base = os.getenv("APPDATA") or str(Path.home() / ".config")
    path = Path(base) / "AdBot"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    f = data_dir() / "config.json"
    if f.exists():
        cfg.update(json.loads(f.read_text(encoding="utf-8")))
    return cfg


def save_config(cfg: dict) -> None:
    (data_dir() / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def parse_channels(text: str, default_template: str) -> dict[str, str]:
    """`@channel` или `@channel | свой текст` (\\n = перенос строки)."""
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        channel, _, template = line.partition("|")
        template = template.strip().replace("\\n", "\n") or default_template
        channel = channel.strip()
        channel = re.sub(r"^(https?://)?t\.me/", "", channel).strip("/")
        if channel and not channel.startswith("@") and not channel.lstrip("-").isdigit():
            channel = "@" + channel
        result[channel] = template
    return result


def resolve_youtube_id(value: str) -> str:
    """Принимает ID (UC...), ссылку /channel/UC... или @handle / ссылку на него."""
    value = value.strip()
    m = re.search(r"(UC[\w-]{22})", value)
    if m:
        return m.group(1)
    handle = re.search(r"@[\w.-]+", value)
    if not handle:
        raise ValueError("Не понял, что за YouTube-канал. Вставьте ссылку на канал или его ID.")
    req = urllib.request.Request(
        f"https://www.youtube.com/{handle.group(0)}",
        headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en"},
    )
    html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
    m = (
        re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', html)
        or re.search(r'channel_id=(UC[\w-]{22})', html)
        or re.search(r'itemprop="(?:identifier|channelId)" content="(UC[\w-]{22})"', html)
    )
    if not m:
        raise ValueError("Не удалось найти ID канала по ссылке. Укажите ID вида UC...")
    return m.group(1)


class Engine:
    def __init__(self, cfg: dict, log: Callable[[str], None], ask: Callable[[str, str], str | None]):
        self.cfg = cfg
        self.log = log
        self.ask = ask  # ask(kind, prompt) -> ответ или None при отмене
        self.stop_event: asyncio.Event | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.state_file = data_dir() / "state.json"

    # --- состояние ---
    def _load_state(self) -> dict:
        if self.state_file.exists():
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        return {"seen_videos": None, "pending": [], "sent": {}}

    def _save_state(self, state: dict) -> None:
        self.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    # --- управление ---
    def run(self, send_latest: bool = False) -> None:
        self.loop = asyncio.new_event_loop()
        try:
            self.loop.run_until_complete(self._main(send_latest))
        except Exception as e:
            self.log(f"Ошибка: {e}")
        finally:
            self.loop.close()

    def stop(self) -> None:
        if self.loop and self.stop_event:
            self.loop.call_soon_threadsafe(self.stop_event.set)

    async def _sleep(self, seconds: float) -> bool:
        """Спит; возвращает True, если нажали «Стоп»."""
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    # --- Telegram / YouTube ---
    async def _login(self) -> TelegramClient:
        def need(kind, prompt):
            ans = self.ask(kind, prompt)
            if ans is None:
                raise RuntimeError("Вход в Telegram отменён")
            return ans

        client = TelegramClient(
            str(data_dir() / "session"), int(self.cfg["api_id"]), self.cfg["api_hash"].strip()
        )
        await client.start(
            phone=lambda: need("phone", "Номер телефона Telegram (в формате +79991234567):"),
            code_callback=lambda: need("code", "Код, который пришёл в Telegram:"),
            password=lambda: need("password", "Пароль двухэтапной аутентификации:"),
        )
        me = await client.get_me()
        self.log(f"Вход выполнен: {me.first_name}")
        return client

    async def _fetch_videos(self, yt_id: str) -> list[dict]:
        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={yt_id}"
        feed = await asyncio.to_thread(feedparser.parse, url)
        if not feed.entries and feed.get("bozo"):
            raise RuntimeError("Не удалось загрузить ленту YouTube (проверьте ID канала и интернет)")
        return [
            {"id": e.yt_videoid, "title": e.title, "url": e.link} for e in reversed(feed.entries)
        ]

    async def _comment(self, client: TelegramClient, channel: str, text: str) -> bool:
        """True — обработано (отправлено или повторять бессмысленно), False — повторить позже."""
        try:
            msgs = await client.get_messages(channel, limit=1)
            if not msgs:
                self.log(f"{channel}: в канале нет постов")
                return True
            await client.send_message(channel, text, comment_to=msgs[0].id)
            return True
        except errors.FloodWaitError as e:
            self.log(f"{channel}: Telegram просит подождать {e.seconds} с")
            await self._sleep(e.seconds + 5)
            return False
        except (errors.MsgIdInvalidError, errors.ChatWriteForbiddenError,
                errors.ChannelPrivateError, errors.UserBannedInChannelError,
                errors.ChatGuestSendForbiddenError):
            self.log(f"{channel}: комментарии недоступны (выключены или нет доступа) — пропускаю")
            return True
        except (ValueError, errors.UsernameInvalidError, errors.UsernameNotOccupiedError):
            self.log(f"{channel}: канал не найден — проверьте имя")
            return True
        except Exception as e:
            self.log(f"{channel}: ошибка отправки ({type(e).__name__}: {e})")
            return False

    async def _broadcast(self, client, state, video) -> bool:
        """True — ролик разослан во все каналы."""
        channels = parse_channels(self.cfg["channels"], self.cfg["template"])
        done = set(state["sent"].get(video["id"], []))
        self.log(f"Рассылка ролика «{video['title']}» ({len(channels)} каналов)")
        for channel, template in channels.items():
            if self.stop_event.is_set():
                return False
            if channel in done:
                continue
            if await self._comment(client, channel, template.format(**video)):
                done.add(channel)
                state["sent"][video["id"]] = sorted(done)
                self._save_state(state)
                self.log(f"{channel}: готово")
            lo, hi = int(self.cfg["delay_min"]), int(self.cfg["delay_max"])
            if await self._sleep(random.randint(lo, max(lo, hi))):
                return False
        return set(channels) <= done

    async def _main(self, send_latest: bool) -> None:
        self.stop_event = asyncio.Event()
        yt_id = resolve_youtube_id(self.cfg["youtube"])
        client = await self._login()
        state = self._load_state()
        try:
            self.log("Слежу за новыми роликами. Для остановки нажмите «Стоп».")
            while not self.stop_event.is_set():
                try:
                    videos = await self._fetch_videos(yt_id)
                    if state["seen_videos"] is None:
                        state["seen_videos"] = [v["id"] for v in videos]
                        state["pending"] = []
                        self.log(f"Запомнил {len(videos)} существующих роликов — их рассылать не буду")
                    else:
                        known = set(state["seen_videos"])
                        for v in videos:
                            if v["id"] not in known:
                                self.log(f"Найден новый ролик: {v['title']}")
                                state["seen_videos"].append(v["id"])
                                state["pending"].append(v)
                    if send_latest and videos:
                        send_latest = False
                        latest = videos[-1]
                        state["sent"].pop(latest["id"], None)
                        state["pending"] = [p for p in state["pending"] if p["id"] != latest["id"]]
                        state["pending"].insert(0, latest)
                    self._save_state(state)
                    while state["pending"] and not self.stop_event.is_set():
                        if not await self._broadcast(client, state, state["pending"][0]):
                            break  # остановка или часть каналов — повторим позже
                        state["pending"].pop(0)
                        self._save_state(state)
                except Exception as e:
                    self.log(f"Ошибка: {e}")
                if await self._sleep(max(1, int(self.cfg["check_minutes"])) * 60):
                    break
        finally:
            await client.disconnect()
            self.log("Остановлено.")
