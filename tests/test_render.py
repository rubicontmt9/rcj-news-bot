import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from rcj_news import render
from rcj_news.models import Item, SourceResult

CONFIG = json.loads((Path(__file__).resolve().parent.parent / "sources.json").read_text("utf-8"))
JST = ZoneInfo("Asia/Tokyo")


def make_item(**kwargs):
    defaults = dict(
        source_id="chiba-node-news",
        source_name="千葉ノードニュース",
        region="chiba",
        title="【サッカー】事前連絡",
        url="https://rcjj-kanto.org/chiba/archives/1730",
        published=datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc),
        leagues=["soccer"],
    )
    defaults.update(kwargs)
    return Item(**defaults)


class ItemLineTest(unittest.TestCase):
    def test_contains_link_date_and_badge(self):
        line = render.item_line(make_item(), CONFIG, JST)
        self.assertIn("⚽サッカー", line)
        self.assertIn("[【サッカー】事前連絡](https://rcjj-kanto.org/chiba/archives/1730)", line)
        # UTC 8/10 00:00 は JST では 8/10 09:00
        self.assertIn("8/10", line)
        self.assertIn("千葉ノードニュース", line)

    def test_no_summary_text_is_added(self):
        item = make_item()
        line = render.item_line(item, CONFIG, JST)
        # タイトル・日付・情報源名・URL 以外の本文は入らない
        self.assertEqual(line.count("http"), 1)

    def test_general_badge(self):
        line = render.item_line(make_item(leagues=[], is_general=True), CONFIG, JST)
        self.assertIn("全般", line)

    def test_markdown_in_title_is_escaped(self):
        line = render.item_line(make_item(title="[重要] *サッカー*"), CONFIG, JST)
        self.assertIn(r"\[重要\]", line)
        self.assertIn(r"\*", line)

    def test_long_title_is_truncated(self):
        line = render.item_line(make_item(title="あ" * 400), CONFIG, JST)
        self.assertIn("…", line)
        self.assertLess(len(line), 400)

    def test_missing_date_is_omitted(self):
        line = render.item_line(make_item(published=None), CONFIG, JST)
        self.assertIn("千葉ノードニュース", line)

    def test_watch_notes_are_rendered(self):
        item = make_item(notes=["新しいリンク: [rules.pdf](https://x/rules.pdf)"])
        line = render.item_line(item, CONFIG, JST)
        self.assertIn("└ 新しいリンク", line)


class BuildEmbedsTest(unittest.TestCase):
    def test_grouped_by_region_in_config_order(self):
        items = [
            make_item(region="world", source_name="Intl"),
            make_item(region="chiba"),
            make_item(region="japan", source_name="JP"),
        ]
        embeds = render.build_embeds(items, CONFIG, JST)
        titles = [embed["title"] for embed in embeds]
        self.assertEqual(len(embeds), 3)
        self.assertIn("千葉", titles[0])
        self.assertIn("日本", titles[1])
        self.assertIn("世界", titles[2])

    def test_region_colors_applied(self):
        embeds = render.build_embeds([make_item()], CONFIG, JST)
        self.assertEqual(embeds[0]["color"], 3066993)

    def test_item_count_in_title(self):
        embeds = render.build_embeds([make_item(), make_item(url="https://x/2")], CONFIG, JST)
        self.assertIn("2件", embeds[0]["title"])

    def test_long_region_is_split_into_multiple_embeds(self):
        items = [
            make_item(title="お知らせ" * 30, url=f"https://rcjj-kanto.org/chiba/archives/{i}")
            for i in range(40)
        ]
        embeds = render.build_embeds(items, CONFIG, JST)
        self.assertGreater(len(embeds), 1)
        for embed in embeds:
            self.assertLessEqual(len(embed["description"]), render.EMBED_DESCRIPTION_LIMIT)
        self.assertIn("続き", embeds[1]["title"])


class HealthEmbedTest(unittest.TestCase):
    def test_none_when_all_ok(self):
        results = [SourceResult("a", "A", "chiba", items=[make_item()])]
        self.assertIsNone(render.health_embed(results))

    def test_reports_failures_and_empties(self):
        results = [
            SourceResult("a", "千葉ノード", "chiba", error="取得失敗: 404"),
            SourceResult("b", "関東ブロック", "kanto", empty=True),
            SourceResult("c", "世界", "world", items=[make_item()]),
        ]
        embed = render.health_embed(results)
        self.assertIn("千葉ノード", embed["description"])
        self.assertIn("関東ブロック", embed["description"])
        self.assertNotIn("世界", embed["description"])

    def test_many_failures_are_collapsed(self):
        results = [
            SourceResult(f"s{i}", f"情報源{i}", "chiba", error="404") for i in range(12)
        ]
        embed = render.health_embed(results)
        lines = embed["description"].split("\n")
        self.assertEqual(len(lines), render.MAX_HEALTH_LINES + 1)
        self.assertIn("ほか 6 件", lines[-1])

    def test_long_error_is_truncated(self):
        results = [SourceResult("a", "A", "chiba", error="x" * 500)]
        embed = render.health_embed(results)
        self.assertLess(len(embed["description"]), 300)


class BuildMessagesTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 12, 7, 0, tzinfo=JST)

    def test_header_has_date_and_weekday(self):
        messages = render.build_messages([make_item()], [], CONFIG, self.now, JST)
        self.assertIn("2026/08/12(水)", messages[0]["content"])

    def test_empty_run_says_so(self):
        messages = render.build_messages([], [], CONFIG, self.now, JST)
        self.assertEqual(len(messages), 1)
        self.assertIn("新しいお知らせはありませんでした", messages[0]["content"])
        self.assertNotIn("embeds", messages[0])

    def test_prefix_note_included(self):
        messages = render.build_messages(
            [make_item()], [], CONFIG, self.now, JST, prefix_note="（初回実行）"
        )
        self.assertIn("初回実行", messages[0]["content"])

    def test_truncated_note_goes_to_last_embed_footer(self):
        messages = render.build_messages(
            [make_item()], [], CONFIG, self.now, JST, truncated_note="10 件省略"
        )
        self.assertEqual(messages[0]["embeds"][-1]["footer"]["text"], "10 件省略")

    def test_split_into_multiple_messages_when_many_embeds(self):
        # 4 地域 × 各 3 分割以上になる量を入れて、8 embed/通 を超えさせる
        regions = ["chiba", "kanto", "japan", "world"]
        items = [
            make_item(
                region=regions[i % 4],
                title="長いタイトル" * 40,
                url=f"https://example.com/{i}",
            )
            for i in range(240)
        ]
        messages = render.build_messages(items, [], CONFIG, self.now, JST)
        self.assertGreater(len(messages), 1)
        for message in messages:
            self.assertLessEqual(len(message["embeds"]), render.EMBEDS_PER_MESSAGE)
        # ヘッダは 1 通目だけ
        self.assertIn("content", messages[0])
        self.assertNotIn("content", messages[1])

    def test_health_embed_is_appended(self):
        results = [SourceResult("a", "千葉ノード", "chiba", error="404")]
        messages = render.build_messages([make_item()], results, CONFIG, self.now, JST)
        self.assertIn("情報源の状態", messages[0]["embeds"][-1]["title"])

    def test_payload_is_json_serializable(self):
        messages = render.build_messages([make_item()], [], CONFIG, self.now, JST)
        json.dumps(messages, ensure_ascii=False)


class RenderPlainTest(unittest.TestCase):
    def test_shows_items_and_status(self):
        results = [
            SourceResult("a", "A", "chiba", items=[make_item()]),
            SourceResult("b", "B", "kanto", error="404"),
        ]
        text = render.render_plain([make_item()], results, JST)
        self.assertIn("chiba", text)
        self.assertIn("NG", text)


if __name__ == "__main__":
    unittest.main()
