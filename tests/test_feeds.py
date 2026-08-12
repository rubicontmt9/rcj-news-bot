import unittest
from datetime import datetime, timezone

from rcj_news import feeds

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>千葉ノードニュース</title>
  <item>
    <title>【サッカー】2026千葉ノード大会 事前連絡</title>
    <link>https://rcjj-kanto.org/chiba/archives/1730</link>
    <guid isPermaLink="false">https://rcjj-kanto.org/chiba/?p=1730</guid>
    <pubDate>Mon, 10 Aug 2026 09:00:00 +0900</pubDate>
    <category>サッカー</category>
  </item>
  <item>
    <title>レスキューライン ルール事前連絡</title>
    <link>https://rcjj-kanto.org/chiba/archives/1719</link>
    <pubDate>Sun, 09 Aug 2026 12:30:00 +0900</pubDate>
  </item>
</channel></rss>
"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Recent Commits to soccer-rules</title>
  <entry>
    <id>tag:github.com,2008:Grit::Commit/abc123</id>
    <title>Update ball diameter to 42mm</title>
    <updated>2026-08-11T04:05:06Z</updated>
    <link rel="alternate" type="text/html" href="https://github.com/robocup-junior/soccer-rules/commit/abc123"/>
  </entry>
</feed>
"""

RDF = """<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel rdf:about="http://example.com"><title>ch</title></channel>
  <item rdf:about="https://example.com/a">
    <title>オンステージ 開催要項</title>
    <link>https://example.com/a</link>
    <dc:date>2026-07-01T00:00:00+09:00</dc:date>
  </item>
</rdf:RDF>
"""


class ParseFeedTest(unittest.TestCase):
    def test_rss(self):
        entries = feeds.parse_feed(RSS)
        self.assertEqual(len(entries), 2)
        first = entries[0]
        self.assertEqual(first.title, "【サッカー】2026千葉ノード大会 事前連絡")
        self.assertEqual(first.url, "https://rcjj-kanto.org/chiba/archives/1730")
        self.assertEqual(first.native_id, "https://rcjj-kanto.org/chiba/?p=1730")
        self.assertEqual(first.categories, ["サッカー"])
        self.assertEqual(
            first.published, datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)
        )

    def test_atom_prefers_alternate_link(self):
        entries = feeds.parse_feed(ATOM)
        self.assertEqual(len(entries), 1)
        self.assertEqual(
            entries[0].url,
            "https://github.com/robocup-junior/soccer-rules/commit/abc123",
        )
        self.assertEqual(
            entries[0].published, datetime(2026, 8, 11, 4, 5, 6, tzinfo=timezone.utc)
        )

    def test_rdf(self):
        entries = feeds.parse_feed(RDF)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].title, "オンステージ 開催要項")
        self.assertIsNotNone(entries[0].published)

    def test_html_page_is_rejected(self):
        with self.assertRaises(feeds.FeedParseError):
            feeds.parse_feed("<html><body><p>not a feed</p></body></html>")

    def test_broken_xml(self):
        with self.assertRaises(feeds.FeedParseError):
            feeds.parse_feed("<rss><channel><item></channel>")

    def test_empty(self):
        with self.assertRaises(feeds.FeedParseError):
            feeds.parse_feed("   ")

    def test_bom_and_declaration(self):
        entries = feeds.parse_feed("﻿" + RSS)
        self.assertEqual(len(entries), 2)


class ParseDatetimeTest(unittest.TestCase):
    def test_rfc822(self):
        value = feeds.parse_datetime("Tue, 11 Aug 2026 09:00:00 +0900")
        self.assertEqual(value, datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc))

    def test_iso_with_fractional_seconds(self):
        value = feeds.parse_datetime("2026-08-11T04:05:06.123456Z")
        self.assertEqual(value, datetime(2026, 8, 11, 4, 5, 6, tzinfo=timezone.utc))

    def test_naive_is_treated_as_utc(self):
        value = feeds.parse_datetime("2026-08-11T04:05:06")
        self.assertEqual(value.tzinfo, timezone.utc)

    def test_date_only_fallback(self):
        value = feeds.parse_datetime("公開日 2026/08/11 更新")
        self.assertEqual(value, datetime(2026, 8, 11, tzinfo=timezone.utc))

    def test_garbage(self):
        self.assertIsNone(feeds.parse_datetime("いつか"))
        self.assertIsNone(feeds.parse_datetime(""))


if __name__ == "__main__":
    unittest.main()
