"""収集処理のテスト。HTTP は差し替えるのでネットワークには一切繋がない。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rcj_news.collect import Collector
from rcj_news.fetch import FetchError, Response
from rcj_news.state import State

CONFIG = json.loads((Path(__file__).resolve().parent.parent / "sources.json").read_text("utf-8"))

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item>
    <title>【サッカー】千葉ノード大会 事前連絡</title>
    <link>https://rcjj-kanto.org/chiba/archives/1730</link>
    <pubDate>Mon, 10 Aug 2026 09:00:00 +0900</pubDate>
  </item>
  <item>
    <title>レスキューライン 事前連絡</title>
    <link>https://rcjj-kanto.org/chiba/archives/1719</link>
    <pubDate>Sun, 09 Aug 2026 09:00:00 +0900</pubDate>
  </item>
  <item>
    <title>【OnStage】演技順の発表</title>
    <link>https://rcjj-kanto.org/chiba/archives/1740</link>
    <pubDate>Tue, 11 Aug 2026 09:00:00 +0900</pubDate>
  </item>
</channel></rss>
"""


def html_response(body: str, url: str = "https://example.com/") -> Response:
    return Response(
        url=url,
        status=200,
        headers={"Content-Type": "text/html; charset=UTF-8"},
        body=body.encode("utf-8"),
    )


def xml_response(body: str, url: str = "https://example.com/feed/") -> Response:
    return Response(
        url=url,
        status=200,
        headers={"Content-Type": "application/rss+xml; charset=UTF-8"},
        body=body.encode("utf-8"),
    )


class CollectorTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State.load(Path(self.tmp.name) / "seen.json")
        self.collector = Collector(CONFIG, self.state)

    def tearDown(self):
        self.tmp.cleanup()


class FeedSourceTest(CollectorTestBase):
    SOURCE = {
        "id": "chiba-node-news",
        "region": "chiba",
        "name": "千葉ノードニュース",
        "kind": "feed",
        "urls": ["https://rcjj-kanto.org/chiba/feed/"],
    }

    def test_soccer_and_onstage_kept_rescue_dropped(self):
        with mock.patch("rcj_news.collect.fetch_first", return_value=xml_response(RSS)):
            result = self.collector.collect_source(self.SOURCE)

        self.assertTrue(result.ok)
        titles = [item.title for item in result.items]
        self.assertIn("【サッカー】千葉ノード大会 事前連絡", titles)
        self.assertIn("【OnStage】演技順の発表", titles)
        self.assertNotIn("レスキューライン 事前連絡", titles)

    def test_items_sorted_newest_first(self):
        with mock.patch("rcj_news.collect.fetch_first", return_value=xml_response(RSS)):
            result = self.collector.collect_source(self.SOURCE)
        self.assertEqual(result.items[0].title, "【OnStage】演技順の発表")

    def test_region_and_source_name_propagated(self):
        with mock.patch("rcj_news.collect.fetch_first", return_value=xml_response(RSS)):
            result = self.collector.collect_source(self.SOURCE)
        self.assertEqual(result.items[0].region, "chiba")
        self.assertEqual(result.items[0].source_name, "千葉ノードニュース")

    def test_fetch_failure_is_recorded_not_raised(self):
        with mock.patch("rcj_news.collect.fetch_first", side_effect=FetchError("404")):
            result = self.collector.collect_source(self.SOURCE)
        self.assertFalse(result.ok)
        self.assertIn("404", result.error)
        self.assertEqual(result.items, [])
        self.assertIn("404", self.state.source("chiba-node-news")["last_error"])

    def test_old_items_are_dropped(self):
        old_rss = RSS.replace("2026", "2019")
        with mock.patch("rcj_news.collect.fetch_first", return_value=xml_response(old_rss)):
            result = self.collector.collect_source(self.SOURCE)
        self.assertEqual(result.items, [])

    def test_per_source_limit_and_truncation_count(self):
        source = dict(self.SOURCE, max_items_per_source=1)
        with mock.patch("rcj_news.collect.fetch_first", return_value=xml_response(RSS)):
            result = self.collector.collect_source(source)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.truncated, 1)

    def test_link_must_contain_filter(self):
        source = dict(self.SOURCE, link_must_contain=["/archives/1740"])
        with mock.patch("rcj_news.collect.fetch_first", return_value=xml_response(RSS)):
            result = self.collector.collect_source(source)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].url, "https://rcjj-kanto.org/chiba/archives/1740")

    def test_title_must_not_contain_filter(self):
        source = dict(self.SOURCE, title_must_not_contain=["OnStage"])
        with mock.patch("rcj_news.collect.fetch_first", return_value=xml_response(RSS)):
            result = self.collector.collect_source(source)
        titles = [item.title for item in result.items]
        self.assertNotIn("【OnStage】演技順の発表", titles)

    def test_forced_leagues_applied(self):
        source = {
            "id": "world-soccer-rules-commits",
            "region": "world",
            "name": "国際ルール更新 (Soccer)",
            "kind": "feed",
            "forced_leagues": ["soccer"],
            "urls": ["https://github.com/x/commits/main.atom"],
        }
        atom = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
          <entry><id>c1</id><title>Update rules.tex</title>
          <updated>2026-08-11T00:00:00Z</updated>
          <link rel="alternate" href="https://github.com/x/commit/c1"/></entry></feed>"""
        with mock.patch("rcj_news.collect.fetch_first", return_value=xml_response(atom)):
            result = self.collector.collect_source(source)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].leagues, ["soccer"])

    def test_html_fallback_used_when_feed_broken(self):
        source = dict(
            self.SOURCE,
            html_fallback={
                "url": "https://rcjj-kanto.org/chiba/",
                "link_must_contain": ["/chiba/archives/"],
            },
        )
        page = """<html><body>
          <p>2026年8月10日 <a href="/chiba/archives/1730">【サッカー】事前連絡</a></p>
          <p><a href="/about.html">このサイトについて</a></p>
        </body></html>"""

        def fake_fetch_first(urls, **kwargs):
            if urls[0].endswith("/feed/"):
                raise FetchError("410 Gone")
            return html_response(page, "https://rcjj-kanto.org/chiba/")

        with mock.patch("rcj_news.collect.fetch_first", side_effect=fake_fetch_first):
            result = self.collector.collect_source(source)

        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].url, "https://rcjj-kanto.org/chiba/archives/1730")

    def test_error_when_feed_and_fallback_both_fail(self):
        source = dict(
            self.SOURCE,
            html_fallback={"url": "https://rcjj-kanto.org/chiba/", "link_must_contain": ["/x/"]},
        )

        def fake_fetch_first(urls, **kwargs):
            raise FetchError("feed-410" if urls[0].endswith("/feed/") else "html-503")

        with mock.patch("rcj_news.collect.fetch_first", side_effect=fake_fetch_first):
            result = self.collector.collect_source(source)

        self.assertFalse(result.ok)
        # どちらの取得が失敗したか両方分かるようにする
        self.assertIn("feed-410", result.error)
        self.assertIn("html-503", result.error)

    def test_fallback_page_without_matching_links_is_not_an_error(self):
        """HTML は取得できたが該当リンクが無い場合は「0 件」として扱う。"""
        source = dict(
            self.SOURCE,
            html_fallback={"url": "https://rcjj-kanto.org/chiba/", "link_must_contain": ["/x/"]},
        )

        def fake_fetch_first(urls, **kwargs):
            if urls[0].endswith("/feed/"):
                raise FetchError("410 Gone")
            return html_response('<a href="/other.html">別のページ</a>')

        with mock.patch("rcj_news.collect.fetch_first", side_effect=fake_fetch_first):
            result = self.collector.collect_source(source)

        self.assertTrue(result.ok)
        self.assertEqual(result.items, [])


class HtmlSourceTest(CollectorTestBase):
    SOURCE = {
        "id": "japan-docs",
        "region": "japan",
        "name": "ジャパン 公開資料",
        "kind": "html",
        "urls": ["https://www.robocupjunior.jp/documentations.html"],
        "link_must_contain": [".pdf", "drive.google.com"],
        "min_title_length": 4,
    }

    PAGE = """<html><body>
      <p>2026年8月1日 <a href="/docs/RCJJ-Soccer_Referee-Handbook_ver7.01.pdf">サッカー審判ハンドブック</a></p>
      <p>2026年8月1日 <a href="/docs/RCJJ-Rescue_Handbook.pdf">レスキュー審判ハンドブック</a></p>
      <p><a href="/index.html">トップへ</a></p>
      <p><a href="/docs/onstage_form.pdf">オンステージ 提出書類</a></p>
    </body></html>"""

    def test_pdf_links_become_items_with_league_filter(self):
        with mock.patch(
            "rcj_news.collect.fetch_first",
            return_value=html_response(self.PAGE, "https://www.robocupjunior.jp/documentations.html"),
        ):
            result = self.collector.collect_source(self.SOURCE)

        titles = [item.title for item in result.items]
        self.assertIn("サッカー審判ハンドブック", titles)
        self.assertIn("オンステージ 提出書類", titles)
        self.assertNotIn("レスキュー審判ハンドブック", titles)
        self.assertNotIn("トップへ", titles)

    def test_absolute_urls(self):
        with mock.patch(
            "rcj_news.collect.fetch_first",
            return_value=html_response(self.PAGE, "https://www.robocupjunior.jp/documentations.html"),
        ):
            result = self.collector.collect_source(self.SOURCE)
        for item in result.items:
            self.assertTrue(item.url.startswith("https://www.robocupjunior.jp/docs/"))

    def test_empty_flag_when_no_links(self):
        with mock.patch(
            "rcj_news.collect.fetch_first",
            return_value=html_response("<html><body>準備中</body></html>"),
        ):
            result = self.collector.collect_source(self.SOURCE)
        self.assertTrue(result.ok)
        self.assertTrue(result.empty)
        self.assertEqual(result.items, [])


class WatchSourceTest(CollectorTestBase):
    SOURCE = {
        "id": "world-soccer-page",
        "region": "world",
        "name": "国際 Soccer ページ",
        "kind": "watch",
        "forced_leagues": ["soccer"],
        "urls": ["https://junior.robocup.org/robocupjunior-soccer/"],
    }

    V1 = '<html><body><p>Rules 2026</p><a href="/rules/soccer_2026.pdf">2026 rules</a></body></html>'
    V2 = (
        '<html><body><p>Rules 2026 updated</p>'
        '<a href="/rules/soccer_2026.pdf">2026 rules</a>'
        '<a href="/rules/soccer_2026_v2.pdf">2026 rules v2</a></body></html>'
    )

    def test_first_run_records_baseline_without_reporting(self):
        with mock.patch("rcj_news.collect.fetch", return_value=html_response(self.V1)):
            result = self.collector.collect_source(self.SOURCE)
        self.assertTrue(result.ok)
        self.assertEqual(result.items, [])
        self.assertIsNotNone(self.state.content_hash("world-soccer-page"))

    def test_unchanged_page_reports_nothing(self):
        with mock.patch("rcj_news.collect.fetch", return_value=html_response(self.V1)):
            self.collector.collect_source(self.SOURCE)
            result = self.collector.collect_source(self.SOURCE)
        self.assertEqual(result.items, [])

    def test_change_is_reported_with_new_links(self):
        with mock.patch("rcj_news.collect.fetch", return_value=html_response(self.V1)):
            self.collector.collect_source(self.SOURCE)
        with mock.patch("rcj_news.collect.fetch", return_value=html_response(self.V2)):
            result = self.collector.collect_source(self.SOURCE)

        self.assertEqual(len(result.items), 1)
        item = result.items[0]
        self.assertIn("更新されました", item.title)
        self.assertEqual(item.leagues, ["soccer"])
        self.assertEqual(len(item.notes), 1)
        self.assertIn("soccer_2026_v2.pdf", item.notes[0])
        # 既に知っていたリンクは「新しいリンク」に出さない
        self.assertNotIn("soccer_2026.pdf](", item.notes[0])

    def test_change_reported_once_per_change(self):
        with mock.patch("rcj_news.collect.fetch", return_value=html_response(self.V1)):
            self.collector.collect_source(self.SOURCE)
        with mock.patch("rcj_news.collect.fetch", return_value=html_response(self.V2)):
            first = self.collector.collect_source(self.SOURCE)
            second = self.collector.collect_source(self.SOURCE)
        self.assertEqual(len(first.items), 1)
        self.assertEqual(second.items, [])

    def test_304_not_modified_is_quiet(self):
        not_modified = Response(url=self.SOURCE["urls"][0], status=304, headers={}, body=b"")
        with mock.patch("rcj_news.collect.fetch", return_value=not_modified):
            result = self.collector.collect_source(self.SOURCE)
        self.assertTrue(result.ok)
        self.assertEqual(result.items, [])

    def test_conditional_headers_are_sent_next_time(self):
        response = Response(
            url=self.SOURCE["urls"][0],
            status=200,
            headers={"Content-Type": "text/html", "ETag": 'W/"abc"', "Last-Modified": "Mon, 10 Aug 2026 00:00:00 GMT"},
            body=self.V1.encode("utf-8"),
        )
        with mock.patch("rcj_news.collect.fetch", return_value=response) as fetcher:
            self.collector.collect_source(self.SOURCE)
            self.collector.collect_source(self.SOURCE)
        _args, kwargs = fetcher.call_args
        self.assertEqual(kwargs["etag"], 'W/"abc"')
        self.assertEqual(kwargs["last_modified"], "Mon, 10 Aug 2026 00:00:00 GMT")

    def test_falls_back_to_second_candidate_url(self):
        def fake_fetch(url, **kwargs):
            if "robocupjunior-soccer" in url:
                raise FetchError("404")
            return html_response(self.V1, url)

        source = dict(
            self.SOURCE,
            urls=[
                "https://junior.robocup.org/robocupjunior-soccer/",
                "https://junior.robocup.org/soccer/",
            ],
        )
        with mock.patch("rcj_news.collect.fetch", side_effect=fake_fetch):
            result = self.collector.collect_source(source)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.used_url, "https://junior.robocup.org/soccer/")


class CollectAllTest(CollectorTestBase):
    def test_disabled_sources_are_skipped(self):
        sources = [
            {"id": "a", "region": "chiba", "name": "A", "kind": "feed", "urls": ["u"], "enabled": False},
            {"id": "b", "region": "chiba", "name": "B", "kind": "feed", "urls": ["u"]},
        ]
        with mock.patch("rcj_news.collect.fetch_first", return_value=xml_response(RSS)):
            results = self.collector.collect(sources)
        self.assertEqual([result.source_id for result in results], ["b"])

    def test_one_bad_source_does_not_stop_others(self):
        sources = [
            {"id": "bad", "region": "chiba", "name": "Bad", "kind": "feed", "urls": ["u"]},
            {"id": "good", "region": "chiba", "name": "Good", "kind": "feed", "urls": ["u"]},
        ]
        calls = {"n": 0}

        def flaky(urls, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise FetchError("boom")
            return xml_response(RSS)

        with mock.patch("rcj_news.collect.fetch_first", side_effect=flaky):
            results = self.collector.collect(sources)
        self.assertFalse(results[0].ok)
        self.assertTrue(results[1].ok)
        self.assertTrue(results[1].items)

    def test_time_budget_skips_remaining_sources(self):
        """複数サイトが落ちていても、集めた分は必ず送れるようにする。"""
        sources = [
            {"id": f"s{i}", "region": "chiba", "name": f"S{i}", "kind": "feed", "urls": ["u"]}
            for i in range(3)
        ]
        self.collector.time_budget = 30

        clock = {"now": 0.0}

        def fake_monotonic():
            return clock["now"]

        def slow_fetch(urls, **kwargs):
            clock["now"] += 20  # 1 ソースあたり 20 秒かかったことにする
            return xml_response(RSS)

        with mock.patch("rcj_news.collect.time.monotonic", side_effect=fake_monotonic):
            with mock.patch("rcj_news.collect.fetch_first", side_effect=slow_fetch):
                results = self.collector.collect(sources)

        self.assertEqual(len(results), 3)
        self.assertTrue(results[0].ok)
        # 予算を超えた分は取得せずエラーとして残る（Discord の状態表示に出る）
        self.assertFalse(results[-1].ok)
        self.assertIn("時間切れ", results[-1].error)

    def test_remaining_timeout_shrinks_near_deadline(self):
        self.collector.time_budget = 100
        with mock.patch("rcj_news.collect.time.monotonic", return_value=0):
            self.collector.collect([])
        with mock.patch("rcj_news.collect.time.monotonic", return_value=92):
            self.assertEqual(self.collector._remaining_timeout(), 8)
        with mock.patch("rcj_news.collect.time.monotonic", return_value=99.9):
            self.assertEqual(self.collector._remaining_timeout(), 5)

    def test_no_budget_when_disabled(self):
        self.collector.time_budget = 0
        self.collector.collect([])
        self.assertIsNone(self.collector._deadline)
        self.assertEqual(self.collector._remaining_timeout(), self.collector.timeout)

    def test_unknown_kind_is_an_error(self):
        source = {"id": "x", "region": "chiba", "name": "X", "kind": "magic", "urls": ["u"]}
        result = self.collector.collect_source(source)
        self.assertFalse(result.ok)
        self.assertIn("magic", result.error)


if __name__ == "__main__":
    unittest.main()
