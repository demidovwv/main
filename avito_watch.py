#!/usr/bin/env python3
"""Avito book watcher. Python 3.11+. See README_RU.md before running."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import random
import re
import signal
import sqlite3
import time
import unicodedata
from dataclasses import asdict, dataclass
from collections import deque
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit, parse_qsl
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
import httpx
from dotenv import load_dotenv

LOG = logging.getLogger("avito-watch")
BASE = Path(__file__).resolve().parent
TARGET = re.compile(r"\bжелезн[а-я]*\s+плам[а-я]*\b")
RELATED = re.compile(r"четверт[а-я]*\s+крыл[а-я]*|\b4\s*крыл[а-я]*|эмпирей|яррос|ярос")
SET_WORDS = re.compile(r"комплект|набор|цикл|коллекци|\bкниги\b|трилоги")
OTHER = re.compile(r"рыцарь\s+и\s+мотыл[а-я]*")
BLOCK_WORDS = ("доступ ограничен", "доступ временно ограничен", "проверка браузера",
               "подтвердите, что вы не робот", "слишком много запросов", "access denied",
               "too many requests", "проверяем, что вы не робот")
EMPTY_WORDS = ("ничего не найдено", "ничего не нашлось", "нет объявлений по вашему запросу")


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    return re.sub(r"\s+", " ", text.replace("\u00ad", "")).strip()


def clean(text: str, limit: int = 900) -> str:
    return re.sub(r"\s+", " ", text).strip()[:limit]


def avito_url(value: str, require_item: bool = False) -> str:
    p = urlsplit(urljoin("https://www.avito.ru", value))
    if p.scheme != "https" or p.hostname not in ("avito.ru", "www.avito.ru") or p.username:
        raise ValueError("Нужна HTTPS-ссылка avito.ru")
    if require_item:
        if not re.search(r"_\d{5,}/?$", p.path):
            raise ValueError("Не ссылка на объявление")
        return "https://www.avito.ru" + p.path.rstrip("/")
    return p.geturl()


@dataclass
class Item:
    id: str
    url: str
    title: str
    price: str = "не указана"
    location: str = "не указан"
    snippet: str = ""
    query: str = ""
    description: str = ""
    published_hint: str = ""
    found_at: float = 0
    state: str = "candidate"


class PageProblem(Exception):
    def __init__(self, reason: str, retryable: bool = False):
        super().__init__(reason)
        self.retryable = retryable


class AccessLimited(PageProblem):
    def __init__(self, reason: str, retry_after: float = 0, manual: bool = False):
        super().__init__(reason)
        self.retry_after, self.manual = retry_after, manual
        self.handled = False
        self.request = {}


class AccessPaused(PageProblem):
    def __init__(self, seconds: float, manual: bool):
        super().__init__("Обращения к Авито приостановлены для всех поисков")
        self.seconds, self.manual = max(0, seconds), manual


def request_info(url: str, search: bool) -> dict:
    p = urlsplit(url)
    # Keep useful search/filter parameters; omit session-like tracking/secrets.
    params = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
              if not re.search(r"token|session|auth|secret|password|context|key", k, re.I)]
    public_url = urlunsplit((p.scheme, p.netloc, p.path, urlencode(params), ""))[:1200]
    query = next((v for k, v in params if k == "q"), "")
    ident = re.search(r"_(\d{5,})/?$", p.path)
    return {"type": "поиск" if search else "описание",
            "label": clean(query or (ident[1] if ident else p.path), 160), "url": public_url}


def retry_after_seconds(value: str | None, now: float | None = None) -> float:
    """RFC 9110: delay-seconds or HTTP-date. Malformed values are ignored."""
    if not value:
        return 0
    now = time.time() if now is None else now
    value = value.strip()
    if re.fullmatch(r"\d+", value):
        return float(value)
    try:
        return max(0, parsedate_to_datetime(value).timestamp()-now)
    except (TypeError, ValueError, OverflowError):
        return 0


def check_access_page(soup):
    title = norm(soup.title.get_text(" ") if soup.title else "")
    header = norm(soup.get_text(" ", strip=True)[:1600])
    if any(w in title for w in BLOCK_WORDS) or (
        not soup.select_one('[data-marker="item"]') and any(w in header for w in BLOCK_WORDS)
    ) or soup.select_one('form[action*="captcha"], iframe[src*="captcha"]'):
        raise AccessLimited("Авито показал ограничение доступа или проверку", manual=True)


def parse_search(html: str, query: str = "") -> list[Item]:
    """Fail closed on a changed/challenge page, never treat it as an empty search."""
    soup = BeautifulSoup(html, "html.parser")
    check_access_page(soup)
    cards = soup.select('[data-marker="item"]')
    result: dict[str, Item] = {}
    for card in cards:
        # Avoid a recommendation block when its marker identifies it as such.
        if card.find_parent(attrs={"data-marker": re.compile("recommend", re.I)}):
            continue
        anchor = card.select_one('a[data-marker="item-title"]')
        if anchor is None:
            continue
        try:
            url = avito_url(str(anchor.get("href", "")), require_item=True)
        except ValueError:
            continue
        match = re.search(r"_(\d{5,})$", url)
        assert match
        name = clean(anchor.get_text(" ", strip=True) or str(anchor.get("title", "")), 350)
        if not name:
            continue
        def field(selector: str, fallback: str) -> str:
            node = card.select_one(selector)
            return clean(str(node.get("content") or node.get_text(" ", strip=True)), 700) if node else fallback
        price = field('[data-marker="item-price"], [itemprop="price"]', "не указана")
        if re.fullmatch(r"[\d\s.,]+", price):
            price += " ₽"
        result[match[1]] = Item(
            id=match[1], url=url, title=name, price=price,
            location=field('[data-marker="item-location"], [data-marker="item-address"]', "не указан"),
            snippet=field('[data-marker="item-description"]', ""), query=query,
            published_hint=field('[data-marker="item-date"]', ""), found_at=time.time(),
        )
    if result:
        return list(result.values())
    # The presence of unparsed cards is a markup error, not an empty result.
    if cards:
        raise PageProblem("Карточки есть, но их разметка не распознана")
    main = soup.select_one('[data-marker="catalog-serp"], main')
    if main and any(w in norm(main.get_text(" ", strip=True)) for w in EMPTY_WORDS):
        return []
    raise PageProblem("Нет распознанной выдачи: страница не загрузилась или разметка изменилась")


def classify(item: Item, notify_candidates: bool = True) -> str:
    title = norm(item.title)
    text = norm(" ".join([item.title, item.snippet, item.description]))
    if re.match(r"^(куплю|ищу|разыскиваю)\b", title):
        return "reject"
    if TARGET.search(text):
        # Don't reject an entire mixed lot just because a DIFFERENT book is sold.
        unavailable = (
            r"(?:нет\s+в\s+наличии|продан[аоы]?|не\s+продается|без)\s*[:—–-]?\s*железн[а-я]*\s+плам[а-я]*",
            r"железн[а-я]*\s+плам[а-я]*\s*[-—–:!.,]*\s*(?:продан[аоы]?|нет\s+в\s+наличии|не\s+продается)",
        )
        if any(re.search(pattern, text) for pattern in unavailable):
            return "reject"
        return "confirmed"
    if not notify_candidates or OTHER.search(title):
        return "reject"
    if RELATED.search(text):
        return "candidate"
    # A general 'Книги фэнтези' card returned by the specified searches may
    # contain the target in the full description/photo. This is deliberately
    # a low-confidence alert, not a claim that the target is present.
    if SET_WORDS.search(title):
        return "candidate"
    return "reject"


def parse_description(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    check_access_page(soup)
    node = soup.select_one('[data-marker="item-view/item-description"], [itemprop="description"]')
    if node is None:
        raise PageProblem("Описание карточки не распознано")
    return clean(str(node.get("content") or node.get_text(" ", strip=True)), 18000)


@dataclass
class SearchRead:
    items: list[Item]
    first_count: int
    final_count: int
    settled: bool


async def collect_search(page, initial_html: str, query: str, on_batch=None,
                         max_wait: float = 8, stable_for: float = 1.5,
                         poll: float = 0.5) -> SearchRead:
    """Observe one loaded document, emitting cards before waiting for stability.

    This makes no goto/reload/scroll calls. Stability is a bounded heuristic,
    not evidence that Avito returned every possible listing.
    """
    started = time.monotonic()
    changed_at = started
    previous = None
    union: dict[str, Item] = {}
    first_count = None
    final_count = 0
    html = initial_html
    last_problem = None
    while True:
        try:
            items = parse_search(html, query)
        except AccessLimited:
            raise
        except PageProblem as exc:
            # A card container can precede its title/link during hydration.
            last_problem = exc
            previous = None
            changed_at = time.monotonic()
        else:
            last_problem = None
            now = time.monotonic()
            signature = tuple(item.id for item in items)
            if first_count is None:
                first_count = len(items)
            final_count = len(items)
            if signature != previous:
                previous, changed_at = signature, now
            for item in items:
                union[item.id] = item
            if on_batch is not None:
                on_batch(items)  # Queue alerts NOW, before any stabilization wait.
            if now-changed_at >= stable_for:
                return SearchRead(list(union.values()), first_count, final_count, True)
        remaining = max_wait-(time.monotonic()-started)
        if remaining <= 0:
            if last_problem:
                raise last_problem
            return SearchRead(list(union.values()), first_count or 0, final_count, False)
        await asyncio.sleep(min(poll, remaining))
        html = await page.content()


def message(item: Item, detailed: bool = False) -> str:
    status = {
        "confirmed": "📗 «Железное пламя» указано в тексте",
        "candidate": "🔎 Возможный вариант — состав нужно проверить",
        "reject": "❌ После проверки текста: не подходит / книга недоступна",
    }[item.state]
    stamp = datetime.fromtimestamp(item.found_at, ZoneInfo("Europe/Moscow")).strftime("%d.%m %H:%M:%S МСК")
    edition = "Обложка и цветной/огненный обрез: нужно проверить по фото или уточнить у продавца."
    excerpt = item.description if detailed else item.snippet
    lines = [status, item.url, item.title, f"Цена: {item.price}", f"Место: {item.location}",
             f"Впервые замечено: {stamp}"]
    if item.published_hint:
        lines.append(f"В выдаче Авито: {item.published_hint}")
    lines.extend([f"Поиск: {item.query}", edition])
    if excerpt:
        lines.append("\nФрагмент текста продавца: " + clean(excerpt, 1200))
    if not detailed:
        lines.append("Описание проверяется отдельно; это сообщение может обновиться.")
    elif item.state == "candidate":
        lines.append("В тексте наличие книги не подтверждено. Фотографии автоматически не анализировались.")
    return "\n".join(lines)[:3800]


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS items(
                id TEXT PRIMARY KEY, data TEXT NOT NULL, body TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1, notify INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS seen(id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS seeded(search_key TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS outbox(
                item_id TEXT NOT NULL, chat_id TEXT NOT NULL, message_id INTEGER,
                sent_revision INTEGER NOT NULL DEFAULT 0, tries INTEGER NOT NULL DEFAULT 0,
                next_try REAL NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(item_id,chat_id));
            CREATE TABLE IF NOT EXISTS details(
                item_id TEXT PRIMARY KEY, tries INTEGER NOT NULL DEFAULT 0,
                next_try REAL NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS runtime_state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        """)

    def seed(self, key: str, items: list[Item], mark: bool = True):
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO seen VALUES(?)", [(x.id,) for x in items])
            if mark:
                self.db.execute("INSERT OR IGNORE INTO seeded VALUES(?)", (key,))

    def is_seeded(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM seeded WHERE search_key=?", (key,)).fetchone() is not None

    def record(self, item: Item, chats: list[str], notify_candidates: bool, details_enabled: bool = True) -> bool:
        with self.db:
            if not self.db.execute("INSERT OR IGNORE INTO seen VALUES(?)", (item.id,)).rowcount:
                return False
            item.state = classify(item, notify_candidates)
            body = message(item)
            if not details_enabled:
                body = body.replace("Описание проверяется отдельно; это сообщение может обновиться.",
                                    "Загрузка описаний отключена. Проверьте состав и издание по ссылке.")
            self.db.execute("INSERT INTO items VALUES(?,?,?,1,?)",
                            (item.id, json.dumps(asdict(item), ensure_ascii=False), body, int(item.state != "reject")))
            # Even a rejected search-card title can conceal a mixed lot. Check
            # its full description off the detection path, without an alert yet.
            self.db.execute("INSERT OR IGNORE INTO details(item_id) VALUES(?)", (item.id,))
            if item.state != "reject":
                self._queue(item.id, chats)
            return item.state != "reject"

    def _queue(self, item_id: str, chats: list[str]):
        self.db.executemany("INSERT OR IGNORE INTO outbox(item_id,chat_id) VALUES(?,?)",
                            [(item_id, chat) for chat in chats])

    def update_item(self, item: Item, chats: list[str]):
        with self.db:
            self.db.execute("UPDATE items SET data=?, body=?, revision=revision+1,notify=? WHERE id=?",
                            (json.dumps(asdict(item), ensure_ascii=False), message(item, True), int(item.state != "reject"), item.id))
            if item.state != "reject":
                self._queue(item.id, chats)
            self.db.execute("UPDATE details SET done=1 WHERE item_id=?", (item.id,))

    def notice(self, key: str, text: str, chats: list[str]):
        # One message per failure episode. Persistent state avoids restart spam.
        item_id = "notice:" + key
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO items VALUES(?,?,?,1,1)", (item_id, "{}", text))
            self._queue(item_id, chats)

    def next_delivery(self):
        return self.db.execute("""SELECT o.*,i.body,i.revision FROM outbox o JOIN items i ON i.id=o.item_id
            WHERE o.disabled=0 AND o.next_try<=? AND o.sent_revision<i.revision
            AND (o.message_id IS NOT NULL OR i.notify=1)
            ORDER BY (o.message_id IS NOT NULL),o.next_try,o.rowid LIMIT 1""", (time.time(),)).fetchone()

    def delivered(self, row, message_id: int):
        with self.db:
            self.db.execute("UPDATE outbox SET message_id=?,sent_revision=?,tries=0,next_try=0 WHERE item_id=? AND chat_id=?",
                            (message_id, row["revision"], row["item_id"], row["chat_id"]))

    def delivery_failed(self, row, delay: float, permanent: bool = False):
        with self.db:
            self.db.execute("UPDATE outbox SET tries=tries+1,next_try=?,disabled=? WHERE item_id=? AND chat_id=?",
                            (time.time()+delay, int(permanent), row["item_id"], row["chat_id"]))

    def next_detail(self):
        return self.db.execute("""SELECT d.*,i.data FROM details d JOIN items i ON i.id=d.item_id
            WHERE d.done=0 AND d.next_try<=? ORDER BY d.tries,d.rowid LIMIT 1""", (time.time(),)).fetchone()

    def finish_detail_message(self, item_id: str):
        self.db.execute("UPDATE items SET body=replace(body,?,?),revision=revision+1 WHERE id=?", (
            "Описание проверяется отдельно; это сообщение может обновиться.",
            "Описание не удалось проверить. Автоматические повторы завершены; проверьте объявление вручную.", item_id))

    def cap_details(self, max_attempts: int):
        with self.db:
            rows = self.db.execute("SELECT item_id FROM details WHERE done=0 AND tries>=?", (max_attempts,)).fetchall()
            for row in rows:
                self.finish_detail_message(row[0])
            self.db.execute("UPDATE details SET done=1 WHERE done=0 AND tries>=?", (max_attempts,))

    def detail_failed(self, row, max_attempts: int = 2, retry_delay: float = 900, permanent: bool = False):
        tries = row["tries"] + 1
        done = permanent or tries >= max_attempts
        with self.db:
            self.db.execute("UPDATE details SET tries=?,next_try=?,done=? WHERE item_id=?",
                            (tries, 0 if done else time.time()+retry_delay, int(done), row["item_id"]))
            if done:
                self.finish_detail_message(row["item_id"])
        return tries, done

    def close(self):
        self.db.close()

    def get_state(self, key: str) -> dict:
        row = self.db.execute("SELECT value FROM runtime_state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else {}

    def set_state(self, key: str, value: dict):
        with self.db:
            self.db.execute("INSERT INTO runtime_state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, json.dumps(value)))


class TelegramProblem(Exception):
    def __init__(self, code: int = 0, retry_after: float = 10, not_modified: bool = False):
        super().__init__(f"Telegram error {code}")
        self.code, self.retry_after, self.not_modified = code, retry_after, not_modified


class Telegram:
    def __init__(self, token: str):
        self.token = token
        self.client = httpx.AsyncClient(timeout=15, trust_env=False)

    async def call(self, method: str, payload: dict) -> dict:
        try:
            r = await self.client.post(f"https://api.telegram.org/bot{self.token}/{method}", json=payload)
            body = r.json()
        except (httpx.HTTPError, ValueError):
            # Never log an httpx exception containing a bot token in its URL.
            raise TelegramProblem() from None
        if not body.get("ok"):
            raise TelegramProblem(
                int(body.get("error_code", r.status_code)),
                max(1, float(body.get("parameters", {}).get("retry_after", 10))),
                "message is not modified" in body.get("description", "").lower(),
            )
        return body["result"]

    async def deliver(self, row) -> int:
        payload = {"chat_id": row["chat_id"], "text": row["body"],
                   "link_preview_options": {"is_disabled": True}}
        if row["message_id"] is not None:
            payload["message_id"] = row["message_id"]
            try:
                result = await self.call("editMessageText", payload)
            except TelegramProblem as exc:
                if exc.not_modified:
                    return row["message_id"]
                raise
        else:
            result = await self.call("sendMessage", payload)
        return int(result["message_id"])


async def wait(stop: asyncio.Event, seconds: float):
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=max(0.01, seconds))


async def deliver_loop(store: Store, telegram: Telegram, stop: asyncio.Event):
    while not stop.is_set():
        row = store.next_delivery()
        if row is None:
            await wait(stop, 0.15)
            continue
        try:
            mid = await telegram.deliver(row)
        except TelegramProblem as exc:
            permanent = exc.code in (400, 401, 403, 404)
            delay = max(exc.retry_after, min(300, 2 ** min(row["tries"]+1, 8)))
            store.delivery_failed(row, delay, permanent)
            LOG.error("Telegram: код %s; %s", exc.code,
                      "отправка этой записи остановлена, проверьте токен/chat_id" if permanent else "будет повтор")
        else:
            store.delivered(row, mid)
            LOG.info("Telegram: %s %s → %s", "обновлено" if row["message_id"] else "отправлено",
                     row["item_id"], row["chat_id"])
        await wait(stop, 0.06)


class Gate:
    """One shared, persistent circuit for ALL Avito navigation, including probes."""
    def __init__(self, gap: float, store: Store | None = None, chats: list[str] | None = None,
                 cooldown: float = 900):
        self.gap, self.store, self.chats = gap, store, chats or []
        self.cooldown = cooldown
        self.last = 0.0
        self.recent_starts = deque()
        self.lock = asyncio.Lock()
        self.state = store.get_state("avito_access") if store else {}

    @property
    def recovering(self) -> bool:
        return bool(self.state.get("strikes", 0))

    def persist(self):
        if self.store:
            self.store.set_state("avito_access", self.state)

    def check(self, search: bool = True):
        remaining = self.state.get("until", 0)-time.time()
        manual = self.state.get("manual", False)
        if remaining > 0 or manual:
            raise AccessPaused(remaining, manual)
        if self.recovering and not search:
            # Only one SEARCH may test recovery; don't spend the probe on a
            # description or release the complete description backlog first.
            raise AccessPaused(1, False)

    def describe(self) -> str:
        until = self.state.get("until", 0)
        label = datetime.fromtimestamp(until, ZoneInfo("Europe/Moscow")).strftime("%d.%m %H:%M:%S МСК") if until else "—"
        if not self.recovering:
            return "Общей паузы нет."
        request = self.state.get("request", {})
        source = (f" Источник: {request['type']} «{request['label']}»; URL: {request['url']}. "
                  if request else " ")
        return (f"Причина: {self.state.get('reason', 'ограничение')}." + source + f"Следующая попытка не раньше {label}. "
                + ("Автопроверки остановлены; нужна ручная проверка и --resume."
                   if self.state.get("manual") else "Дальше программа обновит страницу сама, с более длинным интервалом."))

    def block(self, exc: AccessLimited):
        exc.handled = True
        strikes = int(self.state.get("strikes", 0)) + 1
        episode = self.state.get("episode") or str(time.time_ns())
        # 429 means the refresh was too fast. Wait for Retry-After if Avito sent one,
        # then continue. Captcha, 403 and 439 still need a person.
        delay = max(self.cooldown * 2**min(strikes-1, 4), exc.retry_after) if exc.manual else max(0.0, exc.retry_after)
        self.state = {"strikes": strikes, "episode": episode,
                      "until": max(self.state.get("until", 0), time.time()+delay),
                      "manual": bool(exc.manual), "reason": str(exc),
                      "request": dict(exc.request)}
        self.persist()
        if exc.manual:
            LOG.warning("Все обращения к Авито остановлены. %s", self.describe())
        else:
            LOG.info("Авито ограничил частоту. Обновление продолжится само, интервал станет чуть длиннее. %s", self.describe())
        self.announce_pause()

    def announce_pause(self):
        """Attach recipients to a pause originally created by a silent --probe."""
        if self.store and self.chats and self.recovering:
            # Stable keys survive a restart. Not one notification per query.
            suffix = ":manual" if self.state["manual"] else ":pause"
            title = "⚠️ Общая остановка поиска Авито\n" if self.state.get("manual") else "⏸ Авито просит обновлять реже\n"
            self.store.notice("avito-access:"+self.state["episode"]+suffix, title + self.describe()
                + "\nНовые объявления во время паузы не проверяются. Telegram продолжает работать.", self.chats)

    def ease(self, seconds: float):
        """Keep the shared pause at least `seconds` long without asking for --resume."""
        self.state["manual"] = False
        self.state["until"] = max(float(self.state.get("until", 0)), time.time() + seconds)
        self.state["reason"] = f"Обновление через {int(seconds)} с"
        self.persist()

    def inconclusive_probe(self):
        # A timeout or unrecognized page is not a permanent stop. The search loop
        # lengthens its own interval and opens the page again.
        if self.recovering and not self.state.get("manual"):
            self.state["reason"] = "Страница поиска не открылась, следующая проверка будет позже"
            self.persist()

    def confirm_success(self):
        if self.recovering:
            episode = self.state["episode"]
            self.state = {}
            self.persist()
            if self.store:
                self.store.notice("avito-access:"+episode+":recovered",
                    "✅ Пробная выдача Авито распознана. Поиск возобновлён. "
                    "Это не гарантия отсутствия дальнейших ограничений.", self.chats)

    def resume(self):
        # A user-confirmed manual check does not override Retry-After/cooldown.
        if self.recovering:
            self.state["manual"] = False
            self.persist()

    @contextlib.asynccontextmanager
    async def navigation(self, search: bool = True, url: str = ""):
        async with self.lock:
            self.check(search)
            await asyncio.sleep(max(0.0, self.last + self.gap - time.monotonic()))
            self.check(search)
            self.last = time.monotonic()
            request = request_info(url, search)
            self.recent_starts.append(self.last)
            while self.recent_starts and self.recent_starts[0] < self.last-60:
                self.recent_starts.popleft()
            LOG.info("Авито START | тип=%s | цель=%s | URL=%s | навигаций за 60 с=%s",
                     request["type"], request["label"], request["url"], len(self.recent_starts))
            try:
                yield
            except AccessLimited as exc:
                exc.request = request
                self.block(exc)
                raise
            except Exception as exc:
                reason = str(exc) if isinstance(exc, PageProblem) else type(exc).__name__
                LOG.warning("Авито ERROR | тип=%s | цель=%s | URL=%s | причина=%s",
                            request["type"], request["label"], request["url"], reason)
                if search:
                    self.inconclusive_probe()
                raise
            finally:
                LOG.info("Авито END | тип=%s | цель=%s | длительность=%.1f с | URL=%s",
                         request["type"], request["label"], time.monotonic()-self.last, request["url"])


def release_old_pause(gate: Gate) -> bool:
    """Start the browser even if an older run left a manual stop in the database."""
    paused = bool(gate.state.get("manual")) or float(gate.state.get("until", 0)) > time.time()
    if not paused:
        return False
    gate.state["manual"] = False
    gate.state["until"] = 0
    gate.persist()
    return True


def stretch_pace(store: Store, roster: Roster, search: dict, cap: float, step: float) -> float:
    """Lengthen one search a little after Avito refuses or the page does not load."""
    base = float(search.get("base_interval", search["interval"]))
    ceiling = max(base, cap)
    if search["interval"] < ceiling:
        search["interval"] = min(ceiling, float(search["interval"]) + step)
    roster.interval[search["key"]] = search["interval"]
    saved = store.get_state("poll_pace")
    saved[search["key"]] = search["interval"]
    store.set_state("poll_pace", saved)
    LOG.info("Интервал «%s» увеличен до %s с", search["query"], int(search["interval"]))
    return float(search["interval"])


READY_JS = """() => !!document.querySelector('[data-marker="item"]') ||
    ['ничего не найдено','ничего не нашлось','нет объявлений по вашему запросу',
     'доступ ограничен','доступ временно ограничен','проверка браузера',
     'подтвердите, что вы не робот','слишком много запросов'].some(
       x => document.body && document.body.innerText.toLowerCase().includes(x))"""


async def navigate(page, url: str, gate: Gate, search: bool):
    from playwright.async_api import TimeoutError as PlaywrightTimeout
    async with gate.navigation(search, url):
        response = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        if response:
            retry = retry_after_seconds(response.headers.get("retry-after"))
            if response.status == 429:
                raise AccessLimited("Авито вернул HTTP 429", retry_after=retry)
            if response.status in (403, 439):
                # 439 has no standard HTTP meaning. We only know the search
                # was refused; stop for inspection rather than retrying queries
                # or claiming the VPN IP is the cause.
                raise AccessLimited(f"Авито вернул HTTP {response.status}", retry_after=retry, manual=True)
            if response.status >= 400:
                raise PageProblem(f"Авито вернул HTTP {response.status}",
                                  retryable=response.status >= 500 or response.status == 408)
        try:
            if search:
                await page.wait_for_function(READY_JS, timeout=10000)
            else:
                await page.wait_for_selector('[data-marker="item-view/item-description"], [itemprop="description"]',
                                             state="attached", timeout=10000)
        except PlaywrightTimeout:
            pass
        html = await page.content()
        check_access_page(BeautifulSoup(html, "html.parser"))
        return html


async def detail_loop(context, store: Store, config: dict, chats: list[str], gate: Gate, stop: asyncio.Event,
                       roster: "Roster | None" = None):
    if not config.get("fetch_descriptions", True):
        LOG.info("Загрузка описаний отключена: очередь сохранена, вторичные навигации не выполняются")
        return
    attempts = config.get("detail_max_attempts", 2)
    delay = config.get("detail_retry_seconds", 900)
    gap = config.get("detail_min_gap_seconds", 30)
    store.cap_details(attempts)
    page = await context.new_page()
    next_allowed = 0.0
    while not stop.is_set():
        # Descriptions are extra page loads. Never take the browser while a
        # search is due or already reading: the Telegram link must not wait on them.
        if roster is not None and (roster.busy or roster.seconds_until_next() < 15):
            await wait(stop, 1)
            continue
        if next_allowed > time.monotonic():
            await wait(stop, min(60, next_allowed-time.monotonic()))
            continue
        row = store.next_detail()
        if row is None:
            await wait(stop, 0.3)
            continue
        item = Item(**json.loads(row["data"]))
        try:
            html = await navigate(page, item.url, gate, False)
            item.description = parse_description(html)
            item.state = classify(item, config["notify_candidates"])
            store.update_item(item, chats)
            LOG.info("Описание %s: готово; попытка %s/%s; URL=%s", item.id, row["tries"]+1,
                     attempts, request_info(item.url, False)["url"])
        except asyncio.CancelledError:
            raise
        except AccessPaused as exc:
            await wait(stop, 60 if exc.manual else min(60, max(1, exc.seconds)))
            continue
        except AccessLimited as exc:
            if not exc.handled:
                exc.request = request_info(item.url, False)
                gate.block(exc)
            # A shared access pause is not an unsuccessful description attempt.
            continue
        except Exception as exc:
            permanent = isinstance(exc, PageProblem) and not exc.retryable
            tries, done = store.detail_failed(row, attempts, delay, permanent=permanent)
            reason = str(exc) if isinstance(exc, PageProblem) else type(exc).__name__
            LOG.warning("Описание %s: %s; попытка %s/%s; %s; URL=%s",
                        item.id, reason, tries, attempts,
                        "проверка завершена, повторов не будет" if done else f"повтор не раньше чем через {delay:g} с",
                        request_info(item.url, False)["url"])
        finally:
            next_allowed = time.monotonic()+gap


def make_search(entry: dict, default_interval: float) -> dict:
    query = str(entry.get("query", "")).strip()
    url = entry.get("url") or "https://www.avito.ru/all/knigi_i_zhurnaly?" + urlencode({"s": "104", "q": query})
    if not query:
        query = urlsplit(url).path
    url = avito_url(url)
    interval = float(entry.get("interval_seconds", default_interval))
    if interval < 30:
        LOG.warning("Интервал «%s» увеличен до 30 с: более частые обновления снова приводят к ограничению Авито", query)
        interval = 30
    return {"query": query, "url": url, "interval": interval, "base_interval": interval,
            "key": hashlib.sha256(url.encode()).hexdigest()}


class Roster:
    """When each search may open Avito. The shortest interval is the priority search."""

    def __init__(self, searches: list[dict]):
        indexed = list(enumerate(searches))
        self.primary = min(indexed, key=lambda pair: (pair[1]["interval"], pair[0]))[1]["key"]
        self.interval = {search["key"]: search["interval"] for search in searches}
        now = time.monotonic()
        secondaries = [search for search in searches if search["key"] != self.primary]
        self.due: dict[str, float] = {}
        self.busy = False
        self.secondaries_since_primary = 0
        for search in searches:
            if search["key"] == self.primary or not secondaries:
                self.due[search["key"]] = now
            else:
                rank = next(i for i, item in enumerate(secondaries) if item["key"] == search["key"])
                step = float(search["interval"]) / len(secondaries)
                # Spread the first pass. Eight immediate page loads are what triggered HTTP 429.
                self.due[search["key"]] = now + (rank + 1) * step

    def set_due(self, key: str, when: float):
        self.due[key] = when

    def seconds_until_next(self) -> float:
        if not self.due:
            return 1e9
        return min(self.due.values()) - time.monotonic()

    def choose(self, gate_gap: float, secondary_limit: int) -> str | None:
        now = time.monotonic()
        if self.due[self.primary] <= now:
            return self.primary
        reserve = min(20.0, max(float(gate_gap), self.interval[self.primary] * 0.35))
        if self.due[self.primary] - now < reserve or self.secondaries_since_primary >= secondary_limit:
            return None
        ready = [key for key, due in self.due.items() if key != self.primary and due <= now]
        if not ready:
            return None
        return min(ready, key=lambda key: (self.due[key], key))


async def poll_search(page, store: Store, config: dict, chats: list[str], gate: Gate,
                      first_run: str, search: dict, meta: dict) -> float:
    """Open one search page, queue new listings immediately, and return the next due time."""
    started = time.monotonic()
    html = await navigate(page, search["url"], gate, True)
    count = 0
    baseline = not store.is_seeded(search["key"]) and first_run == "baseline"
    def consume(batch):
        nonlocal count
        if baseline:
            store.seed(search["key"], batch, mark=False)
        else:
            for item in batch:
                count += int(store.record(
                    item, chats, config["notify_candidates"], config.get("fetch_descriptions", True)))
    reading = await collect_search(page, html, search["query"], consume)
    items = reading.items
    gate.confirm_success()
    lag = time.monotonic() - meta["last_ok"] if meta["last_ok"] else 0
    store.seed(search["key"], [])
    if baseline:
        LOG.info("Начальная база «%s»: %s объявлений, без рассылки", search["query"], len(items))
    LOG.info("DOM «%s»: сначала %s, в конце %s, уникальных за чтение %s; список %s",
             search["query"], reading.first_count, reading.final_count, len(items),
             "стабилен 1.5 с" if reading.settled else "ещё менялся, предел ожидания 8 с")
    LOG.info("Поиск «%s»: %s карточек, %s к отправке; загрузка %.1f с; между проверками %.1f с",
             search["query"], len(items), count, time.monotonic() - started, lag)
    if meta["errors"] >= 3:
        store.notice(search["key"] + ":" + meta["episode"] + ":recovered",
                     f"✅ Поиск снова работает: {search['query']}", chats)
    meta["errors"] = 0
    meta["episode"] = str(int(time.time()))
    meta["last_ok"] = time.monotonic()
    nxt = max(time.monotonic() + 0.05, started + search["interval"])
    if search["interval"] >= 30:
        nxt += random.uniform(0, min(5.0, search["interval"] * 0.1))
    return nxt


async def scan_search(context, store: Store, config: dict, chats: list[str], gate: Gate,
                      stop: asyncio.Event, first_run: str, search: dict, slots: asyncio.Semaphore):
    page = await context.new_page()
    meta = {"errors": 0, "episode": str(int(time.time())), "last_ok": 0.0}
    due = 0.0
    while not stop.is_set():
        if due > time.monotonic():
            await wait(stop, min(1, due - time.monotonic()))
            continue
        try:
            # Separate search tasks avoid one slow page delaying every query.
            # The semaphore and shared gate still bound total browser traffic.
            async with slots:
                if stop.is_set():
                    break
                due = await poll_search(page, store, config, chats, gate, first_run, search, meta)
        except asyncio.CancelledError:
            raise
        except AccessPaused as exc:
            await wait(stop, 60 if exc.manual else min(60, max(1, exc.seconds)))
        except AccessLimited as exc:
            # One common notification and cooldown, not eight query failures.
            if not exc.handled:
                exc.request = request_info(search["url"], True)
                gate.block(exc)
            due = time.monotonic() + 1
        except Exception as exc:
            due = fail_search(store, gate, search, meta, exc, chats)


def fail_search(store: Store, gate: Gate, search: dict, meta: dict, exc: Exception, chats: list[str]) -> float:
    gate.inconclusive_probe()
    meta["errors"] += 1
    delay = min(900, 30 * 2 ** min(meta["errors"] - 1, 5))
    description = str(exc) if isinstance(exc, PageProblem) else type(exc).__name__
    LOG.warning("Поиск «%s»: %s; пауза %s с", search["query"], description, delay)
    if meta["errors"] == 3:
        store.notice(search["key"] + ":" + meta["episode"] + ":failed",
                     f"⚠️ Поиск «{search['query']}» не работает после трёх попыток.\n"
                     f"Причина: {description}.\nЭто ошибка, а не отсутствие новых объявлений. "
                     "Проверьте доступ к Авито. Интервал временно увеличен.", chats)
    return time.monotonic() + delay + random.uniform(0, 3)


async def scan_lane(context, store: Store, config: dict, chats: list[str], gate: Gate,
                    stop: asyncio.Event, first_run: str, roster: Roster):
    """One browser tab. Priority search keeps its interval; other searches take at most
    one turn in each gap, so eight queries cannot stampede Avito."""
    page = await context.new_page()
    meta = {search["key"]: {"errors": 0, "episode": str(int(time.time())), "last_ok": 0.0, "ok_streak": 0}
            for search in config["searches"]}
    searches = {search["key"]: search for search in config["searches"]}
    saved_pace = store.get_state("poll_pace")
    cap = float(config.get("interval_cap_seconds", 120))
    step = float(config.get("interval_step_seconds", 15))
    for search in config["searches"]:
        base = float(search.get("base_interval", search["interval"]))
        search["base_interval"] = base
        if search["key"] in saved_pace:
            search["interval"] = min(max(base, cap), max(base, float(saved_pace[search["key"]])))
            roster.interval[search["key"]] = search["interval"]
    limit = int(config.get("max_secondary_per_cycle", 1))
    while not stop.is_set():
        key = roster.choose(gate.gap, limit)
        if key is None:
            nxt = min(roster.due.values())
            await wait(stop, min(1.0, max(0.02, nxt - time.monotonic())))
            continue
        search = searches[key]
        if key == roster.primary:
            roster.secondaries_since_primary = 0
        else:
            roster.secondaries_since_primary += 1
        roster.busy = True
        try:
            try:
                roster.set_due(key, await poll_search(
                    page, store, config, chats, gate, first_run, search, meta[key]))
                info = meta[key]
                info["ok_streak"] = info.get("ok_streak", 0) + 1
                floor = float(search.get("base_interval", search["interval"]))
                if info["ok_streak"] >= 3 and search["interval"] > floor:
                    search["interval"] = max(floor, float(search["interval"]) - step)
                    roster.interval[key] = search["interval"]
                    pace = store.get_state("poll_pace")
                    pace[key] = search["interval"]
                    store.set_state("poll_pace", pace)
                    info["ok_streak"] = 0
                    LOG.info("Интервал «%s» уменьшен до %s с", search["query"], int(search["interval"]))
            except asyncio.CancelledError:
                raise
            except AccessPaused as exc:
                await wait(stop, 60 if exc.manual else min(60, max(1, exc.seconds)))
            except AccessLimited as exc:
                if not exc.handled:
                    exc.request = request_info(search["url"], True)
                    gate.block(exc)
                meta[key]["ok_streak"] = 0
                if exc.manual:
                    roster.set_due(key, time.monotonic() + 1)
                else:
                    wait_s = max(stretch_pace(store, roster, search, cap, step), float(exc.retry_after or 0))
                    gate.ease(wait_s)
                    roster.set_due(key, time.monotonic() + wait_s)
            except Exception as exc:
                meta[key]["ok_streak"] = 0
                if type(exc).__name__ == "TimeoutError":
                    wait_s = stretch_pace(store, roster, search, cap, step)
                    gate.ease(wait_s)
                    roster.set_due(key, time.monotonic() + wait_s)
                    LOG.warning("Страница «%s» не загрузилась. Следующее обновление через %s с",
                                search["query"], int(wait_s))
                else:
                    roster.set_due(key, fail_search(store, gate, search, meta[key], exc, chats))
        finally:
            roster.busy = False


async def scan_loop(context, store: Store, config: dict, chats: list[str], gate: Gate,
                    stop: asyncio.Event, first_run: str, roster: Roster | None = None):
    if int(config["search_concurrency"]) <= 1:
        roster = roster or Roster(config["searches"])
        await scan_lane(context, store, config, chats, gate, stop, first_run, roster)
        return
    slots = asyncio.Semaphore(config["search_concurrency"])
    async with asyncio.TaskGroup() as group:
        for search in config["searches"]:
            group.create_task(scan_search(context, store, config, chats, gate, stop, first_run, search, slots))


def load_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    interval = float(config.get("interval_seconds", 300))
    config["searches"] = [make_search(x, interval) for x in config["searches"] if x.get("enabled", True)]
    if not config["searches"]:
        raise ValueError("Нет включённых поисков")
    config["navigation_gap_seconds"] = max(8, float(config.get("navigation_gap_seconds", 8)))
    config["search_concurrency"] = 1
    config["cooldown_seconds"] = max(900, float(config.get("cooldown_seconds", 900)))
    config["notify_candidates"] = bool(config.get("notify_candidates", True))
    config["fetch_descriptions"] = bool(config.get("fetch_descriptions", True))
    config["detail_max_attempts"] = max(1, min(2, int(config.get("detail_max_attempts", 2))))
    config["detail_retry_seconds"] = max(900, float(config.get("detail_retry_seconds", 900)))
    config["detail_min_gap_seconds"] = max(60, float(config.get("detail_min_gap_seconds", 60)))
    config["max_secondary_per_cycle"] = max(0, min(2, int(config.get("max_secondary_per_cycle", 1))))
    config["interval_step_seconds"] = max(15, float(config.get("interval_step_seconds", 15)))
    config["interval_cap_seconds"] = max(60, float(config.get("interval_cap_seconds", 120)))
    return config


def credentials() -> tuple[str, list[str]]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chats = list(dict.fromkeys(x.strip() for x in os.environ.get("TELEGRAM_CHAT_IDS", "").split(",") if x.strip()))
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token) or not chats:
        raise ValueError("Заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_IDS в .env")
    if not all(re.fullmatch(r"-?\d+", x) for x in chats):
        raise ValueError("TELEGRAM_CHAT_IDS: нужны числовые chat_id через запятую")
    return token, chats


async def get_chat_ids(token: str):
    bot = Telegram(token)
    try:
        # Explicit helper mode only; never called by normal monitoring. Does not
        # delete webhooks or change the settings of an existing bot.
        updates = await bot.call("getUpdates", {"timeout": 0})
        found = {}
        for update in updates:
            msg = update.get("message") or update.get("channel_post") or {}
            chat = msg.get("chat", {})
            if "id" in chat:
                found[str(chat["id"])] = chat.get("title") or chat.get("first_name") or chat.get("username", "")
        for chat_id, label in found.items():
            print(chat_id, label)
        if not found:
            print("Нет сообщений. Откройте бота, нажмите Старт и отправьте /start с каждого аккаунта.")
    finally:
        await bot.client.aclose()


@contextlib.contextmanager
def instance_lock(path: Path):
    """OS-owned lock is released even after a process crash; no stale PID file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("Уже запущена другая копия с этой папкой данных") from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def browser_settings(args, state_dir: Path) -> tuple[Path, dict]:
    chosen = getattr(args, "profile_dir", None) or os.environ.get("BROWSER_PROFILE_DIR", "").strip()
    if chosen:
        profile = Path(chosen).expanduser()
        if not profile.is_absolute() or not profile.is_dir():
            raise ValueError("BROWSER_PROFILE_DIR / --profile-dir: нужна существующая абсолютная папка профиля")
        if (profile / "Preferences").is_file() and not (profile / "Local State").exists():
            raise ValueError("Укажите корень User Data Directory, а не вложенную папку Default / Profile 1")
    else:
        profile = state_dir / "browser-profile"
    profile = profile.resolve()
    headless = os.environ.get("BROWSER_HEADLESS", "true").strip().lower()
    if headless not in ("true", "false", "1", "0", "yes", "no"):
        raise ValueError("BROWSER_HEADLESS должен быть true или false")
    options = {"headless": headless in ("true", "1", "yes") and not (args.headed or args.login),
               "viewport": {"width": 1366, "height": 900}, "timeout": 30000}
    channel = os.environ.get("BROWSER_CHANNEL", "").strip()
    executable = os.environ.get("BROWSER_EXECUTABLE_PATH", "").strip()
    if channel and executable:
        raise ValueError("Задайте либо BROWSER_CHANNEL, либо BROWSER_EXECUTABLE_PATH, не оба")
    if chosen and not (channel or executable):
        raise ValueError("Для существующего внешнего профиля укажите тот же браузер: BROWSER_CHANNEL или BROWSER_EXECUTABLE_PATH")
    if channel:
        options["channel"] = channel
    if executable:
        binary = Path(executable).expanduser()
        if not binary.is_absolute() or not binary.is_file():
            raise ValueError("BROWSER_EXECUTABLE_PATH: нужен существующий абсолютный путь к браузеру")
        options["executable_path"] = str(binary)
    proxy_server = os.environ.get("AVITO_PROXY_SERVER", "").strip()
    if proxy_server:
        options["proxy"] = {"server": proxy_server}
        for field in ("username", "password"):
            value = os.environ.get("AVITO_PROXY_" + field.upper())
            if value:
                options["proxy"][field] = value
    return profile, options


async def run(args):
    load_dotenv(BASE / ".env", override=False)
    if args.get_chat_ids:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
            raise ValueError("Заполните TELEGRAM_BOT_TOKEN в .env")
        await get_chat_ids(token)
        return 0
    if args.test_telegram:
        token, chats = credentials()
        bot = Telegram(token)
        try:
            for chat in chats:
                await bot.call("sendMessage", {"chat_id": chat, "text": "✅ Проверка: уведомления о книгах подключены."})
                print("Тест отправлен:", chat)
        finally:
            await bot.client.aclose()
        return 0
    config = load_config(args.config)
    if args.probe_all and not args.probe:
        raise ValueError("--probe-all используется вместе с --probe")
    token, chats = credentials() if not (args.probe or args.login or args.status or args.resume) else ("", [])
    state_dir = Path(os.environ.get("WATCH_STATE_DIR", str(BASE / "data"))).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    with instance_lock(state_dir / "watch.lock"), contextlib.closing(Store(state_dir / "watch.sqlite3")) as store:
        gate = Gate(config["navigation_gap_seconds"], store, chats, config["cooldown_seconds"])
        if args.status:
            print(gate.describe())
            return 0
        if args.resume:
            gate.resume()
            print(gate.describe())
            print("Мониторинг не запущен. Для одной проверки используйте --probe, затем запустите монитор отдельно.")
            return 0
        if args.probe or args.login:
            try:
                gate.check()
            except AccessPaused:
                print(gate.describe())
                return 2
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            profile, options = browser_settings(args, state_dir)
            LOG.info("Браузер: %s; профиль: %s; окно: %s",
                     options.get("channel") or options.get("executable_path") or "Playwright Chromium",
                     profile, "нет" if options["headless"] else "да")
            proxy = os.environ.get("AVITO_PROXY_SERVER", "").strip()
            if proxy:
                parts = urlsplit(proxy)
                LOG.info("Маршрут Авито: прокси %s://%s. Логин и пароль в журнал не пишутся.",
                         parts.scheme, parts.hostname or "")
            else:
                LOG.info("Маршрут Авито: прямое подключение этой машины. "
                         "VPN на весь сервер подходит. Локальный SOCKS без AVITO_PROXY_SERVER браузер не использует.")
            try:
                context = await p.chromium.launch_persistent_context(str(profile), **options)
            except Exception as exc:
                raise ValueError("Не удалось открыть браузер ("+type(exc).__name__+"). "
                    "Проверьте путь/канал браузера, что другой процесс с этим профилем закрыт, "
                    "а для режима с окном доступна графическая сессия DISPLAY. "
                    "Не удаляйте файлы блокировки профиля вручную.") from None
            try:
                if args.login:
                    page = await context.new_page()
                    try:
                        await navigate(page, config["searches"][0]["url"], gate, True)
                    except AccessLimited:
                        print(gate.describe())
                        return 2
                    print("Откройте поиск в браузере. При необходимости войдите / пройдите обычную проверку вручную.")
                    await asyncio.to_thread(input, "После этого нажмите Enter здесь. Профиль сохранится: ")
                    return 0
                if args.probe:
                    page = await context.new_page()
                    errors = 0
                    selected = config["searches"] if args.probe_all else config["searches"][:1]
                    for search in selected:
                        try:
                            html = await navigate(page, search["url"], gate, True)
                            reading = await collect_search(page, html, search["query"])
                            items = reading.items
                            gate.confirm_success()
                            print(f"OK | {search['query']} | {len(items)} карточек")
                            print(f"DOM: сначала {reading.first_count}, в конце {reading.final_count}; "
                                  + ("стабилен 1.5 с" if reading.settled else "предел ожидания 8 с"))
                            for item in items[:3]:
                                print("  ", item.id, item.title, "|", item.price)
                        except AccessLimited as exc:
                            if not exc.handled:
                                exc.request = request_info(search["url"], True)
                                gate.block(exc)
                            print(gate.describe())
                            return 2
                        except AccessPaused:
                            print(gate.describe())
                            return 2
                        except Exception as exc:
                            gate.inconclusive_probe()
                            errors += 1
                            print("ОШИБКА |", search["query"], "|", str(exc) if isinstance(exc, PageProblem) else type(exc).__name__)
                    return 2 if errors else 0
                if args.seed_ids:
                    ids = []
                    for line in args.seed_ids.read_text(encoding="utf-8-sig").splitlines():
                        line = line.strip()
                        if re.fullmatch(r"\d{5,}", line):
                            ids.append((line,))
                        else:
                            m = re.search(r"_(\d{5,})(?:[/?#]|$)", line)
                            if m:
                                ids.append((m[1],))
                    with store.db:
                        store.db.executemany("INSERT OR IGNORE INTO seen VALUES(?)", ids)
                    LOG.info("Загружено старых номеров/ссылок: %s", len(ids))
                stop = asyncio.Event()
                loop = asyncio.get_running_loop()
                for sig in (signal.SIGTERM, signal.SIGINT):
                    with contextlib.suppress(NotImplementedError):
                        loop.add_signal_handler(sig, stop.set)
                bot = Telegram(token)
                gate.announce_pause()
                if release_old_pause(gate):
                    LOG.info("Старая пауза снята. Браузер откроется и сам продолжит обновлять поиск.")
                roster = Roster(config["searches"])
                for search in config["searches"]:
                    delay = max(0.0, roster.due[search["key"]] - time.monotonic())
                    LOG.info("Поиск «%s»: каждые %s с, первая проверка через %.0f с%s",
                             search["query"], int(search["interval"]), delay,
                             "; приоритет, ссылка уходит сразу" if search["key"] == roster.primary else "")
                primary = next(search for search in config["searches"] if search["key"] == roster.primary)
                LOG.info("Мониторинг 1.7 запущен. Приоритетный поиск «%s» каждые %s с; "
                         "между этими проверками не больше %s остальных запросов. "
                         "Описания: %s. Возможные варианты без подтверждения: %s.",
                         primary["query"], int(primary["interval"]),
                         config.get("max_secondary_per_cycle", 1),
                         "включены" if config.get("fetch_descriptions", True) else "выключены",
                         "включены" if config.get("notify_candidates", True) else "выключены")
                try:
                    async with asyncio.TaskGroup() as tasks:
                        tasks.create_task(scan_loop(context, store, config, chats, gate, stop, args.first_run, roster))
                        tasks.create_task(deliver_loop(store, bot, stop))
                        tasks.create_task(detail_loop(context, store, config, chats, gate, stop, roster))
                finally:
                    await bot.client.aclose()
            finally:
                await context.close()
    return 0


def main():
    parser = argparse.ArgumentParser(description="Быстрые уведомления о книгах Авито → Telegram")
    parser.add_argument("--version", action="version", version="Avito Book Watch 1.7 — refresh every 45s and lengthen the interval when Avito refuses")
    parser.add_argument("--config", type=Path, default=BASE / "config.json")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--probe", action="store_true", help="Один поиск без рассылки/начальной базы; состояние ограничения сохраняется")
    modes.add_argument("--login", action="store_true", help="Открыть браузер для ручного входа")
    modes.add_argument("--test-telegram", action="store_true", help="Отправить тест каждому получателю")
    modes.add_argument("--get-chat-ids", action="store_true", help="Показать chat_id из входящих сообщений бота")
    modes.add_argument("--status", action="store_true", help="Показать общую паузу без браузера и обращений к Авито")
    modes.add_argument("--resume", action="store_true", help="После ручной проверки разрешить одну попытку; срок паузы не сокращается")
    parser.add_argument("--probe-all", action="store_true", help="С --probe проверить все поиски; при первом 429 остановиться")
    parser.add_argument("--headed", action="store_true", help="Показывать окно браузера")
    parser.add_argument("--profile-dir", type=Path, help="Существующий абсолютный User Data Directory; приоритет над BROWSER_PROFILE_DIR")
    parser.add_argument("--first-run", choices=("baseline", "notify"), default="baseline",
                        help="Первый успешный обход каждого поиска: запомнить текущие или уведомить о них")
    parser.add_argument("--seed-ids", type=Path, help="TXT старых номеров/ссылок, по одному на строку")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0
    except (ValueError, FileNotFoundError) as exc:
        LOG.error("%s", exc)
        return 2
    except TelegramProblem as exc:
        LOG.error("Telegram: код %s. Проверьте токен, chat_id и что оба получателя нажали Старт.", exc.code)
        return 2
    except Exception as exc:
        # Avoid raw exception chains, which may expose proxy or bot secrets.
        LOG.error("Остановка: %s. Проверьте зависимости, браузер и подключение; начните с --probe --headed.", type(exc).__name__)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
