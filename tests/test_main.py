import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from rcj_news import main as main_module
from rcj_news.models import Item, SourceResult
from rcj_news.state import State

CONFIG_PATH = Path(__file__).resolve().parent.parent / "sources.json"
CONFIG = json.loads(CONFIG_PATH.read_text("utf-8"))


def make_result(source_id="s1", count=5, region="chiba"):
    items = [
        Item(
            source_id=source_id,
            source_name="S1",
            region=region,
            title=f"【サッカー】お知らせ {i}",
            url=f"https://example.com/{source_id}/{i}",
            published=datetime(2026, 8, 12, tzinfo=timezone.utc) - timedelta(days=i),
            leagues=["soccer"],
        )
        for i in range(count)
    ]
    return SourceResult(source_id, "S1", region, items=items)


class ConfigTest(unittest.TestCase):
    def test_bundled_config_loads(self):
        config = main_module.load_config(CONFIG_PATH)
        self.assertTrue(config["sources"])

    def test_all_regions_referenced_by_sources_exist(self):
        region_ids = {region["id"] for region in CONFIG["regions"]}
        for source in CONFIG["sources"]:
            self.assertIn(source["region"], region_ids, source["id"])

    def test_every_region_has_at_least_one_source(self):
        used = {source["region"] for source in CONFIG["sources"]}
        for region in CONFIG["regions"]:
            self.assertIn(region["id"], used, f"{region['id']} の情報源がありません")

    def test_source_kinds_are_known(self):
        for source in CONFIG["sources"]:
            self.assertIn(source.get("kind", "feed"), {"feed", "html", "watch"}, source["id"])

    def test_urls_are_https(self):
        for source in CONFIG["sources"]:
            for url in source["urls"]:
                self.assertTrue(url.startswith("https://"), f"{source['id']}: {url}")

    def test_forced_leagues_reference_defined_leagues(self):
        for source in CONFIG["sources"]:
            for league in source.get("forced_leagues", []):
                self.assertIn(league, CONFIG["leagues"], source["id"])

    def test_duplicate_ids_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {"id": "a", "urls": ["https://x"]},
                            {"id": "a", "urls": ["https://y"]},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(SystemExit):
                main_module.load_config(path)

    def test_missing_urls_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text(json.dumps({"sources": [{"id": "a"}]}), encoding="utf-8")
            with self.assertRaises(SystemExit):
                main_module.load_config(path)

    def test_missing_file_rejected(self):
        with self.assertRaises(SystemExit):
            main_module.load_config(Path("/nonexistent/sources.json"))


class WebhookLookupTest(unittest.TestCase):
    def test_explicit_wins(self):
        with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://env"}, clear=True):
            self.assertEqual(main_module.find_webhook_url("https://cli"), "https://cli")

    def test_env_order(self):
        with mock.patch.dict(
            os.environ,
            {"DISCORD_WEBHOOK": "https://second", "WEBHOOK_URL": "https://fourth"},
            clear=True,
        ):
            self.assertEqual(main_module.find_webhook_url(), "https://second")

    def test_blank_env_ignored(self):
        with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "   "}, clear=True):
            self.assertIsNone(main_module.find_webhook_url())


class SelectNewItemsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State.load(Path(self.tmp.name) / "seen.json")
        self.options = dict(CONFIG["options"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_run_is_limited(self):
        self.options["first_run_items_per_source"] = 2
        items, dropped = main_module.select_new_items(
            [make_result(count=5)], self.state, self.options, force=False
        )
        self.assertEqual(len(items), 2)
        self.assertEqual(dropped, 0)

    def test_first_run_marks_everything_seen(self):
        self.options["first_run_items_per_source"] = 2
        main_module.select_new_items(
            [make_result(count=5)], self.state, self.options, force=False
        )
        # 2 回目は新規なし
        items, _ = main_module.select_new_items(
            [make_result(count=5)], self.state, self.options, force=False
        )
        self.assertEqual(items, [])

    def test_only_new_items_on_later_runs(self):
        main_module.select_new_items(
            [make_result(count=3)], self.state, self.options, force=False
        )
        items, _ = main_module.select_new_items(
            [make_result(count=4)], self.state, self.options, force=False
        )
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0].url.endswith("/3"))

    def test_force_ignores_seen(self):
        main_module.select_new_items(
            [make_result(count=3)], self.state, self.options, force=False
        )
        items, _ = main_module.select_new_items(
            [make_result(count=3)], self.state, self.options, force=True
        )
        self.assertEqual(len(items), 3)

    def test_failed_sources_contribute_nothing(self):
        failed = SourceResult("bad", "Bad", "chiba", error="404")
        items, _ = main_module.select_new_items(
            [failed], self.state, self.options, force=False
        )
        self.assertEqual(items, [])

    def test_total_cap_applied(self):
        self.options["first_run_items_per_source"] = 100
        self.options["max_items_total"] = 3
        results = [make_result(source_id=f"s{i}", count=4) for i in range(3)]
        items, dropped = main_module.select_new_items(
            results, self.state, self.options, force=False
        )
        self.assertEqual(len(items), 3)
        self.assertEqual(dropped, 9)

    def test_sorted_newest_first_across_sources(self):
        self.options["first_run_items_per_source"] = 100
        results = [make_result(source_id="a", count=3), make_result(source_id="b", count=3)]
        items, _ = main_module.select_new_items(
            results, self.state, self.options, force=False
        )
        dates = [item.published for item in items]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_items_without_date_go_last(self):
        self.options["first_run_items_per_source"] = 100
        dated = make_result(source_id="a", count=1)
        undated = make_result(source_id="b", count=1)
        undated.items[0].published = None
        items, _ = main_module.select_new_items(
            [undated, dated], self.state, self.options, force=False
        )
        self.assertIsNotNone(items[0].published)
        self.assertIsNone(items[-1].published)


class MainCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "seen.json"

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, extra_args, results):
        args = [
            "--config", str(CONFIG_PATH),
            "--state", str(self.state_path),
            *extra_args,
        ]
        with mock.patch.object(main_module.Collector, "collect", return_value=results):
            return main_module.main(args)

    def test_dry_run_does_not_post_or_save(self):
        with mock.patch("rcj_news.main.post_messages") as poster:
            code = self.run_main(["--dry-run"], [make_result()])
        self.assertEqual(code, 0)
        poster.assert_not_called()
        self.assertFalse(self.state_path.exists())

    def test_seed_saves_state_without_posting(self):
        with mock.patch("rcj_news.main.post_messages") as poster:
            code = self.run_main(["--seed"], [make_result()])
        self.assertEqual(code, 0)
        poster.assert_not_called()
        self.assertTrue(self.state_path.exists())

    def test_posts_and_saves(self):
        with mock.patch("rcj_news.main.post_messages", return_value=1) as poster:
            code = self.run_main(["--webhook", "https://discord.test/hook"], [make_result()])
        self.assertEqual(code, 0)
        poster.assert_called_once()
        payloads = poster.call_args[0][1]
        self.assertIn("content", payloads[0])
        self.assertTrue(self.state_path.exists())

    def test_missing_webhook_returns_error(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            code = self.run_main([], [make_result()])
        self.assertEqual(code, 2)
        self.assertFalse(self.state_path.exists())

    def test_all_sources_failing_returns_error(self):
        failed = [SourceResult("a", "A", "chiba", error="404")]
        with mock.patch("rcj_news.main.post_messages", return_value=1):
            code = self.run_main(["--webhook", "https://discord.test/hook"], failed)
        self.assertEqual(code, 1)

    def test_partial_failure_still_succeeds(self):
        results = [make_result(), SourceResult("b", "B", "kanto", error="404")]
        with mock.patch("rcj_news.main.post_messages", return_value=1):
            code = self.run_main(["--webhook", "https://discord.test/hook"], results)
        self.assertEqual(code, 0)

    def test_discord_failure_returns_error(self):
        from rcj_news.discord import DiscordError

        with mock.patch("rcj_news.main.post_messages", side_effect=DiscordError("400")):
            code = self.run_main(["--webhook", "https://discord.test/hook"], [make_result()])
        self.assertEqual(code, 1)

    def test_only_filter_selects_sources(self):
        with mock.patch("rcj_news.main.post_messages", return_value=1):
            code = self.run_main(
                ["--webhook", "https://discord.test/hook", "--only", "chiba-node-news"],
                [make_result()],
            )
        self.assertEqual(code, 0)

    def test_only_filter_with_unknown_id_errors(self):
        code = self.run_main(["--only", "does-not-exist"], [])
        self.assertEqual(code, 2)

    def test_first_run_note_is_included(self):
        with mock.patch("rcj_news.main.post_messages", return_value=1) as poster:
            self.run_main(["--webhook", "https://discord.test/hook"], [make_result()])
        content = poster.call_args[0][1][0]["content"]
        self.assertIn("初回実行", content)


if __name__ == "__main__":
    unittest.main()
