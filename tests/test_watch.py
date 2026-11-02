import asyncio
import argparse
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

import httpx

from avito_watch import (Item, Store, Telegram, TelegramProblem, PageProblem,
                         classify, parse_search, parse_description, make_search, scan_loop, Gate,
                         AccessLimited, AccessPaused, retry_after_seconds, navigate, browser_settings,
                         collect_search, request_info, detail_loop, Roster, scan_lane, load_config,
                         message, SearchRead)


def sample(id="1234567890", title="Железное пламя", **kwargs):
    return Item(id=id, url=f"https://www.avito.ru/moskva/knigi_i_zhurnaly/book_{id}",
                title=title, found_at=1793600000, **kwargs)


class BrowserProfileTests(unittest.TestCase):
    def test_existing_profile_and_visible_chrome_keep_database_directory_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = root / "avito-proxy"
            existing.mkdir()
            args = argparse.Namespace(profile_dir=None, headed=False, login=False)
            with patch.dict(os.environ, {"BROWSER_PROFILE_DIR": str(existing), "BROWSER_CHANNEL": "chrome",
                                        "BROWSER_HEADLESS": "false",
                                        "AVITO_PROXY_SERVER": "socks5://127.0.0.1:10808"}, clear=True):
                profile, options = browser_settings(args, root / "data")
            self.assertEqual(profile, existing.resolve())
            self.assertEqual(options["channel"], "chrome")
            self.assertFalse(options["headless"])
            self.assertEqual(options["proxy"]["server"], "socks5://127.0.0.1:10808")
            self.assertFalse((existing / "watch.sqlite3").exists())

    def test_invalid_existing_profile_never_silently_creates_an_empty_session(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"BROWSER_CHANNEL": "chrome"}, clear=True):
            args = argparse.Namespace(profile_dir=Path(tmp)/"typo", headed=True, login=False)
            with self.assertRaises(ValueError):
                browser_settings(args, Path(tmp)/"data")
            self.assertFalse(args.profile_dir.exists())
            args.profile_dir = Path(tmp)/"Default"
            args.profile_dir.mkdir()
            (args.profile_dir/"Preferences").write_text("{}")
            with self.assertRaises(ValueError):
                browser_settings(args, Path(tmp)/"data")

    def test_cli_profile_has_priority_and_browser_settings_are_unambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / "chrome"
            binary.write_text("test fixture, not executable")
            args = argparse.Namespace(profile_dir=root, headed=True, login=False)
            values = {"BROWSER_PROFILE_DIR": str(root/"ignored"), "BROWSER_EXECUTABLE_PATH": str(binary),
                      "BROWSER_HEADLESS": "true"}
            with patch.dict(os.environ, values, clear=True):
                profile, options = browser_settings(args, root/"data")
                self.assertEqual(profile, root.resolve())
                self.assertFalse(options["headless"])
                os.environ["BROWSER_CHANNEL"] = "chrome"
                with self.assertRaises(ValueError):
                    browser_settings(args, root/"data")


class ParsingTests(unittest.TestCase):
    def test_extract_card_and_ignore_recommendations(self):
        html = '''<html><title>Книги</title><main><div data-marker="item">
            <a data-marker="item-title" href="/moskva/knigi_i_zhurnaly/test_1234567890?context=tracking">
            Железное пламя</a><meta itemprop="price" content="4500">
            <div data-marker="item-location">Москва</div>
            <p data-marker="item-description">Комплект с цветным обрезом</p>
            </div></main><div data-marker="recommendations"><div data-marker="item">
            <a data-marker="item-title" href="/moskva/test_2222222222">Другая книга</a>
            </div></div></html>'''
        items = parse_search(html, "Железное пламя")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].id, "1234567890")
        self.assertEqual(items[0].price, "4500 ₽")
        self.assertNotIn("context", items[0].url)

    def test_real_empty_is_distinct_from_challenge_and_broken_markup(self):
        self.assertEqual(parse_search('<main>Ничего не найдено</main>'), [])
        for html in ('<title>Доступ ограничен</title>', '<html><body>Loading...</body></html>',
                     '<div data-marker="item">new layout</div>'):
            with self.subTest(html=html), self.assertRaises(PageProblem):
                parse_search(html)

    def test_external_links_not_allowed(self):
        with self.assertRaises(PageProblem):
            parse_search('''<div data-marker="item"><a data-marker="item-title"
                href="https://attacker.example/test_1234567890">Железное пламя</a></div>''')
        with self.assertRaises(ValueError):
            make_search({"url": "http://localhost:1234", "query": "test"}, 5)

    def test_description(self):
        self.assertEqual(parse_description('<div data-marker="item-view/item-description">Железное <b>пламя</b></div>'),
                         "Железное пламя")

    def test_filter_mixed_lots_and_unrelated_books(self):
        self.assertEqual(classify(sample(title="Цикл Эмпирей", snippet="В наборе Железное пламя")), "confirmed")
        self.assertEqual(classify(sample(title="Рыцарь и мотылёк")), "reject")
        self.assertEqual(classify(sample(title="Четвёртое крыло")), "candidate")
        self.assertEqual(classify(sample(title="Четвёртое крыло"), False), "reject")
        self.assertEqual(classify(sample(title="Куплю Железное пламя")), "reject")
        self.assertEqual(classify(sample(description="Железное пламя — продано")), "reject")
        self.assertEqual(classify(sample(title="Цикл Эмпирей", description="Четвёртое крыло продано. Железное пламя 5000")), "confirmed")


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test.db"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_two_new_items_two_recipients_and_no_repeat_after_restart(self):
        chats = ["11", "22"]
        self.assertTrue(self.store.record(sample(), chats, True))
        self.assertTrue(self.store.record(sample("2234567890"), chats, True))
        self.assertFalse(self.store.record(sample(), chats, True))
        sent = []
        while row := self.store.next_delivery():
            sent.append((row["item_id"], row["chat_id"]))
            self.store.delivered(row, 100+len(sent))
        self.assertEqual(len(set(sent)), 4)
        self.store.close()
        self.store = Store(self.path)
        self.assertFalse(self.store.record(sample(), chats, True))
        self.assertIsNone(self.store.next_delivery())

    def test_failed_recipient_does_not_block_the_other(self):
        self.store.record(sample(), ["11", "22"], True)
        first = self.store.next_delivery()
        self.store.delivery_failed(first, 60)
        second = self.store.next_delivery()
        self.assertNotEqual(first["chat_id"], second["chat_id"])
        self.store.delivered(second, 123)
        self.assertIsNone(self.store.next_delivery())

    def test_recovery_of_pending_delivery_and_same_message_update(self):
        item = sample(title="Комплект Эмпирей")
        self.store.record(item, ["11"], True)
        self.store.close()
        self.store = Store(self.path)
        first = self.store.next_delivery()
        self.store.delivered(first, 55)
        item.description = "Железное пламя"
        item.state = "confirmed"
        self.store.update_item(item, ["11"])
        edited = self.store.next_delivery()
        self.assertEqual(edited["message_id"], 55)
        self.assertEqual(edited["revision"], 2)

    def test_seeded_items_and_strict_mode_hidden_target(self):
        self.store.seed("query1", [sample()])
        self.assertTrue(self.store.is_seeded("query1"))
        self.assertFalse(self.store.record(sample(), ["11"], True))
        hidden = sample("3234567890", title="Книги")
        self.assertFalse(self.store.record(hidden, ["11"], False))
        self.assertIsNone(self.store.next_delivery())
        hidden.description = "В комплекте есть Железное пламя"
        hidden.state = classify(hidden, False)
        self.store.update_item(hidden, ["11"])
        self.assertIsNotNone(self.store.next_delivery())

    def test_rejected_candidate_unsent_suppressed_but_inflight_corrected(self):
        item = sample(title="Цикл Эмпирей")
        self.store.record(item, ["11", "22"], True)
        inflight = self.store.next_delivery()
        item.state = "reject"
        self.store.update_item(item, ["11", "22"])
        self.assertIsNone(self.store.next_delivery())
        # The first message may have been accepted while detail analysis ran.
        self.store.delivered(inflight, 77)
        edit = self.store.next_delivery()
        self.assertEqual(edit["message_id"], 77)
        self.assertIn("не подходит", edit["body"])


class TelegramTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_text_no_token_in_error_and_rate_limit(self):
        bot = Telegram("123:secret")
        await bot.client.aclose()
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})
        bot.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        row = {"chat_id": "11", "body": "<b>Seller text</b>", "message_id": None}
        self.assertEqual(await bot.deliver(row), 7)
        self.assertNotIn("parse_mode", requests[0])
        await bot.client.aclose()
        bot.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(
            429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 42}})))
        try:
            await bot.deliver(row)
            self.fail("expected rate limit")
        except TelegramProblem as exc:
            self.assertEqual(exc.retry_after, 42)
            self.assertNotIn("secret", str(exc))
        await bot.client.aclose()


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_independent_searches_queue_individual_items_without_waiting_for_batch(self):
        class Context:
            async def new_page(self):
                class Page:
                    html = ""
                    async def content(self):
                        return self.html
                return Page()
        active, peak, finished = 0, 0, 0
        stop = asyncio.Event()
        async def navigate(page, url, gate, search):
            nonlocal active, peak, finished
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            finished += 1
            ident = "1234567890" if "one" in url else "2234567890"
            if finished == 2:
                stop.set()
            page.html = f'''<div data-marker="item"><a data-marker="item-title"
                 href="/moskva/test_{ident}">Железное пламя</a></div>'''
            return page.html
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.db")
            config = {"search_concurrency": 2, "notify_candidates": True, "searches": [
                make_search({"query": "one"}, 5), make_search({"query": "two"}, 5)]}
            try:
                with patch("avito_watch.navigate", navigate):
                    await asyncio.wait_for(scan_loop(Context(), store, config, ["11", "22"],
                                                     Gate(0.5), stop, "notify"), timeout=4)
                self.assertEqual(peak, 2)
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 4)
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 2)
            finally:
                store.close()


class DOMReadTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def html(*ids):
        return "<main>"+"".join(f'''<div data-marker="item"><a data-marker="item-title"
            href="/moskva/test_{ident}">Железное пламя</a></div>''' for ident in ids)+"</main>"

    async def test_growing_list_queues_first_alert_before_waiting_and_collects_late_cards(self):
        initial = self.html("1234567890")
        final = self.html("1234567890", "2234567890", "3234567890")
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/"db.sqlite")
            class Page:
                reads = 0
                async def content(page):
                    page.reads += 1
                    # Initial alert was queued before we even read a later DOM.
                    self.assertIsNotNone(store.next_delivery())
                    return final
            page = Page()
            try:
                def consume(items):
                    for item in items:
                        store.record(item, ["11", "22"], True)
                result = await collect_search(page, initial, "test", consume,
                                              max_wait=0.2, stable_for=0.02, poll=0.01)
                self.assertEqual(result.first_count, 1)
                self.assertEqual(result.final_count, 3)
                self.assertEqual(len(result.items), 3)
                self.assertTrue(result.settled)
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 6)
            finally:
                store.close()

    async def test_same_count_with_replaced_ids_keeps_both_observed_items(self):
        initial, final = self.html("1234567890"), self.html("2234567890")
        class Page:
            content = AsyncMock(return_value=final)
        result = await collect_search(Page(), initial, "test", max_wait=0.2, stable_for=0.02, poll=0.01)
        self.assertEqual(result.first_count, result.final_count)
        self.assertEqual({x.id for x in result.items}, {"1234567890", "2234567890"})

    async def test_placeholder_is_not_mistaken_for_empty_and_baseline_stays_quiet(self):
        placeholder = '<div data-marker="item"></div>'
        final = self.html("1234567890", "2234567890")
        class Page:
            content = AsyncMock(return_value=final)
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/"db.sqlite")
            try:
                def consume(items):
                    store.seed("query", items, mark=False)
                    self.assertFalse(store.is_seeded("query"))
                result = await collect_search(Page(), placeholder, "test", consume,
                                              max_wait=0.2, stable_for=0.02, poll=0.01)
                self.assertEqual(len(result.items), 2)
                self.assertIsNone(store.next_delivery())
                store.seed("query", [])
                self.assertTrue(store.is_seeded("query"))
            finally:
                store.close()

    async def test_unstable_list_has_bounded_wait_without_reload_or_goto(self):
        class Page:
            number = 2234567890
            goto = AsyncMock()
            reload = AsyncMock()
            async def content(page):
                page.number += 1
                return self.html(str(page.number))
        page = Page()
        result = await asyncio.wait_for(collect_search(page, self.html("1234567890"), "test",
            max_wait=0.04, stable_for=0.2, poll=0.005), timeout=1)
        self.assertFalse(result.settled)
        self.assertGreater(len(result.items), 1)
        page.goto.assert_not_called()
        page.reload.assert_not_called()


class RequestDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_429_persists_exact_request_kind_and_url_and_logs_start_end(self):
        class Response:
            status = 429
            headers = {}
        class Page:
            goto = AsyncMock(return_value=Response())
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/"db.sqlite")
            try:
                url = "https://www.avito.ru/moskva/book_1234567890"
                with self.assertLogs("avito-watch", level="INFO") as captured:
                    with self.assertRaises(AccessLimited):
                        await navigate(Page(), url, Gate(0, store, ["11"]), False)
                description = Gate(0, store).describe()
                self.assertIn("описание", description)
                self.assertIn(url, description)
                self.assertIn("HTTP 429", description)
                log = "\n".join(captured.output)
                self.assertIn("Авито START", log)
                self.assertIn("Авито END", log)
                self.assertIn("тип=описание", log)
            finally:
                store.close()

    def test_request_log_drops_session_tracking_and_keeps_search(self):
        info = request_info("https://www.avito.ru/all?q=book&s=104&token=SECRET&context=TRACK#fragment", True)
        self.assertEqual(info["type"], "поиск")
        self.assertEqual(info["label"], "book")
        self.assertIn("q=book", info["url"])
        self.assertNotIn("SECRET", info["url"])
        self.assertNotIn("TRACK", info["url"])
        self.assertNotIn("fragment", info["url"])


class DetailRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_descriptions_do_not_open_a_tab_and_keep_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/"db.sqlite")
            context = type("Context", (), {"new_page": AsyncMock()})()
            try:
                store.record(sample(), ["11"], True, details_enabled=False)
                await detail_loop(context, store, {"fetch_descriptions": False}, ["11"], Gate(0), asyncio.Event())
                context.new_page.assert_not_awaited()
                self.assertIsNotNone(store.next_detail())
                self.assertIn("Загрузка описаний отключена", store.next_delivery()["body"])
            finally:
                store.close()

    async def test_markup_failure_is_terminal_but_timeout_gets_one_delayed_retry(self):
        for failure, expected_done in [(PageProblem("Описание не распознано"), 1), (TimeoutError(), 0)]:
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as tmp:
                store = Store(Path(tmp)/"db.sqlite")
                stop = asyncio.Event()
                context = type("Context", (), {"new_page": AsyncMock(return_value=object())})()
                async def failing_navigation(*args):
                    stop.set()
                    raise failure
                try:
                    store.record(sample(), ["11"], True)
                    with patch("avito_watch.navigate", failing_navigation):
                        await detail_loop(context, store, {"notify_candidates": True}, ["11"], Gate(0), stop)
                    state = store.db.execute("SELECT tries,done FROM details").fetchone()
                    self.assertEqual(tuple(state), (1, expected_done))
                    if expected_done:
                        self.assertIsNone(store.next_detail())
                        self.assertIn("повторы завершены", store.next_delivery()["body"])
                finally:
                    store.close()

    def test_retry_budget_and_delay_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"db.sqlite"
            store = Store(path)
            try:
                store.record(sample(), ["11"], True)
                with patch("avito_watch.time.time", return_value=1000):
                    self.assertEqual(store.detail_failed(store.next_detail()), (1, False))
                store.close()
                store = Store(path)
                with patch("avito_watch.time.time", return_value=1899):
                    self.assertIsNone(store.next_detail())
                with patch("avito_watch.time.time", return_value=1901):
                    self.assertEqual(store.detail_failed(store.next_detail()), (2, True))
                with patch("avito_watch.time.time", return_value=9000):
                    self.assertIsNone(store.next_detail())
            finally:
                store.close()

    def test_old_pending_jobs_at_new_attempt_cap_are_completed_without_reopening(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/"db.sqlite")
            try:
                store.record(sample(), ["11"], True)
                with store.db:
                    store.db.execute("UPDATE details SET tries=2,done=0")
                store.cap_details(2)
                self.assertIsNone(store.next_detail())
                self.assertIn("повторы завершены", store.next_delivery()["body"])
            finally:
                store.close()


class AccessControlTests(unittest.IsolatedAsyncioTestCase):
    def test_retry_after_numeric_http_date_and_invalid(self):
        self.assertEqual(retry_after_seconds("3600", now=0), 3600)
        self.assertEqual(retry_after_seconds("Thu, 01 Jan 1970 00:02:00 GMT", now=30), 90)
        for value in (None, "", "bad", "-1"):
            self.assertEqual(retry_after_seconds(value, now=0), 0)

    async def test_first_429_stops_other_search_and_description_and_survives_restart(self):
        class Response:
            status = 429
            headers = {"retry-after": "3600"}
        class Page:
            goto = AsyncMock(return_value=Response())
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/"db.sqlite")
            gate = Gate(0, store, ["11", "22"], cooldown=900)
            page = Page()
            try:
                with patch("avito_watch.time.time", return_value=1000):
                    results = await asyncio.gather(
                        navigate(page, "https://www.avito.ru/all?q=one", gate, True),
                        navigate(page, "https://www.avito.ru/all?q=two", gate, True),
                        return_exceptions=True)
                    self.assertIsInstance(results[0], AccessLimited)
                    self.assertIsInstance(results[1], AccessPaused)
                    self.assertEqual(page.goto.await_count, 1)
                    self.assertEqual(gate.state["until"], 4600)
                    with self.assertRaises(AccessPaused):
                        await navigate(page, "https://www.avito.ru/book_1234567890", gate, False)
                    self.assertEqual(page.goto.await_count, 1)
                    independent_store = Store(Path(tmp)/"db.sqlite")
                    try:
                        reopened = Gate(0, independent_store)
                        with self.assertRaises(AccessPaused):
                            reopened.check()
                    finally:
                        independent_store.close()
                    # One COMMON notice per receiver, not per blocked query.
                    self.assertEqual(store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 2)
            finally:
                store.close()

    async def test_second_429_requires_manual_action_and_resume_keeps_deadline(self):
        gate = Gate(0)
        with patch("avito_watch.time.time", return_value=1000):
            gate.block(AccessLimited("HTTP 429"))
        with patch("avito_watch.time.time", return_value=1901):
            # After cooldown a description cannot take the recovery probe.
            with self.assertRaises(AccessPaused):
                gate.check(search=False)
            gate.check(search=True)
            gate.block(AccessLimited("HTTP 429"))
            self.assertTrue(gate.state["manual"])
            gate.resume()
            self.assertFalse(gate.state["manual"])
            with self.assertRaises(AccessPaused):
                gate.check()  # --resume did not override the deadline.
        with patch("avito_watch.time.time", return_value=5000):
            gate.check()
            gate.inconclusive_probe()
            self.assertTrue(gate.state["manual"])
            with self.assertRaises(AccessPaused):
                gate.check()

    async def test_successful_search_clears_recovery_and_403_requires_manual_check(self):
        gate = Gate(0)
        gate.block(AccessLimited("HTTP 403", manual=True))
        with self.assertRaises(AccessPaused):
            gate.check()
        gate.confirm_success()
        self.assertEqual(gate.state, {})
        gate.check(search=False)

    async def test_challenge_on_200_is_global_manual_stop(self):
        class Response:
            status = 200
            headers = {}
        class Page:
            goto = AsyncMock(return_value=Response())
            wait_for_function = AsyncMock()
            content = AsyncMock(return_value="<title>Доступ ограничен</title>")
        gate = Gate(0)
        with self.assertRaises(AccessLimited):
            await navigate(Page(), "https://www.avito.ru/all", gate, True)
        self.assertTrue(gate.state["manual"])

    async def test_439_stops_before_any_other_request_without_diagnosing_ip(self):
        class Response:
            status = 439
            headers = {}
        class Page:
            goto = AsyncMock(return_value=Response())
        page = Page()
        gate = Gate(0)
        with self.assertRaises(AccessLimited):
            await navigate(page, "https://www.avito.ru/all", gate, True)
        self.assertTrue(gate.state["manual"])
        self.assertIn("439", gate.state["reason"])
        self.assertNotIn("IP", gate.state["reason"])
        with self.assertRaises(AccessPaused):
            await navigate(page, "https://www.avito.ru/all?q=another", gate, True)
        self.assertEqual(page.goto.await_count, 1)

    def test_silent_probe_pause_notified_once_on_monitor_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/"db.sqlite")
            try:
                # --probe has no Telegram recipients and makes no outbound call.
                probe_gate = Gate(0, store, [])
                probe_gate.block(AccessLimited("HTTP 429"))
                self.assertIsNone(store.next_delivery())
                normal_gate = Gate(0, store, ["11", "22"])
                normal_gate.announce_pause()
                normal_gate.announce_pause()
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 2)
                first = store.next_delivery()
                self.assertIn("Общая остановка", first["body"])
                store.delivered(first, 101)
                second = store.next_delivery()
                store.delivered(second, 102)
                normal_gate.announce_pause()
                self.assertIsNone(store.next_delivery())
            finally:
                store.close()


class StableScheduleTests(unittest.IsolatedAsyncioTestCase):
    def test_fast_interval_is_raised_and_config_floor_stays_stable(self):
        with self.assertLogs("avito-watch", level="WARNING"):
            self.assertEqual(make_search({"query": "x", "interval_seconds": 5}, 30)["interval"], 30)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({
                "interval_seconds": 10,
                "navigation_gap_seconds": 1,
                "search_concurrency": 4,
                "detail_min_gap_seconds": 5,
                "max_secondary_per_cycle": 9,
                "searches": [
                    {"query": "Железное пламя", "interval_seconds": 45},
                    {"query": "Яррос"},
                ],
            }), encoding="utf-8")
            config = load_config(path)
        self.assertEqual(config["searches"][0]["interval"], 45)
        self.assertEqual(config["searches"][0]["query"], "Железное пламя")
        self.assertGreaterEqual(config["searches"][1]["interval"], 30)
        self.assertGreaterEqual(config["navigation_gap_seconds"], 8)
        self.assertEqual(config["search_concurrency"], 1)
        self.assertGreaterEqual(config["detail_min_gap_seconds"], 60)
        self.assertEqual(config["max_secondary_per_cycle"], 2)

    def test_message_puts_the_listing_link_on_the_second_line(self):
        lines = message(sample()).splitlines()
        self.assertTrue(lines[1].startswith("https://www.avito.ru/"))
        self.assertIn("1234567890", lines[1])

    def test_secondaries_are_spread_instead_of_starting_together(self):
        searches = []
        for name in ("Железное пламя", "a", "b", "c"):
            item = make_search({"query": name}, 30)
            item["interval"] = 45 if name == "Железное пламя" else 300
            searches.append(item)
        roster = Roster(searches)
        now = time.monotonic()
        secondary_dues = [roster.due[item["key"]] for item in searches if item["key"] != roster.primary]
        self.assertEqual(roster.primary, searches[0]["key"])
        self.assertLessEqual(roster.due[roster.primary], now)
        self.assertGreaterEqual(min(secondary_dues), now + 30)
        self.assertGreater(max(secondary_dues) - min(secondary_dues), 30)

    async def test_priority_search_is_not_followed_by_a_secondary_burst(self):
        order = []
        stop = asyncio.Event()

        async def navigate(page, url, gate, search):
            order.append(url)
            await asyncio.sleep(0.01)
            if len(order) >= 6:
                stop.set()
            return '''<div data-marker="item"><a data-marker="item-title"
                href="/moskva/test_1234567890">Железное пламя</a></div>'''

        async def fast_collect(page, html, query, on_batch=None, **kwargs):
            items = parse_search(html, query)
            if on_batch:
                on_batch(items)
            return SearchRead(items, len(items), len(items), True)

        class Page:
            async def content(self):
                return ""

        class Context:
            async def new_page(self):
                return Page()

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            primary = make_search({"query": "primary"}, 30)
            side_a = make_search({"query": "side-a"}, 30)
            side_b = make_search({"query": "side-b"}, 30)
            for item in (primary, side_a, side_b):
                item["interval"] = 0.05
            config = {"search_concurrency": 1, "notify_candidates": True, "fetch_descriptions": True,
                      "max_secondary_per_cycle": 1, "searches": [primary, side_a, side_b]}
            roster = Roster(config["searches"])
            now = time.monotonic()
            for key in roster.due:
                roster.due[key] = now
            try:
                with patch("avito_watch.navigate", navigate), patch("avito_watch.collect_search", fast_collect):
                    await asyncio.wait_for(scan_lane(
                        Context(), store, config, ["11"], Gate(0), stop, "notify", roster), timeout=4)
            finally:
                store.close()
        self.assertGreaterEqual(len(order), 6)
        self.assertEqual(order[0], primary["url"])
        kinds = ["P" if url == primary["url"] else "S" for url in order]
        self.assertIn("S", kinds)
        for previous, current in zip(kinds, kinds[1:]):
            if previous == "S":
                self.assertEqual(current, "P")

    async def test_description_waits_while_priority_search_is_due(self):
        context = type("Context", (), {"new_page": AsyncMock(return_value=object())})()
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            try:
                store.record(sample(), ["11"], True)
                primary = make_search({"query": "primary"}, 30)
                roster = Roster([primary])
                roster.due[primary["key"]] = time.monotonic()
                stop = asyncio.Event()

                async def refuse_navigation(*args):
                    stop.set()
                    raise AssertionError("описание не должно открывать Авито, пока поиск уже пора проверять")

                async def stopper():
                    await asyncio.sleep(0.2)
                    stop.set()

                with patch("avito_watch.navigate", refuse_navigation):
                    await asyncio.gather(
                        detail_loop(context, store, {"notify_candidates": True, "fetch_descriptions": True},
                                    ["11"], Gate(0), stop, roster),
                        stopper())
                context.new_page.assert_awaited()
                self.assertIsNotNone(store.next_detail())
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
