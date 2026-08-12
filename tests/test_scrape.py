import unittest
from datetime import datetime, timezone

from rcj_news import scrape

PAGE = """<html><head><title>ロボカップジュニアジャパン</title></head><body>
<nav><a href="/info.html">ロボカップジュニアとは</a></nav>
<div id="news">
  <p>2026年8月10日 <a href="2026aichi_outline.html">2026愛知大会 開催情報</a></p>
  <p>8/3 <a href="/docs/RCJJ-Soccer_Referee-Handbook_ver7.01.pdf">サッカー審判ハンドブック</a></p>
  <p><a href="https://drive.google.com/file/d/xyz/view">オンステージ 開催要項</a></p>
  <p><a href="/docs/no-text.pdf"></a></p>
</div>
<footer><a href="/contact.html">お問い合わせ</a></footer>
<script>var a = "<a href='/trap.html'>trap</a>";</script>
</body></html>
"""


class ParsePageTest(unittest.TestCase):
    def setUp(self):
        self.today = datetime(2026, 8, 12, tzinfo=timezone.utc)
        self.page = scrape.parse_page(
            PAGE, "https://www.robocupjunior.jp/", today=self.today
        )
        self.by_url = {link.url: link for link in self.page.links}

    def test_relative_links_are_absolute(self):
        self.assertIn("https://www.robocupjunior.jp/2026aichi_outline.html", self.by_url)

    def test_nav_footer_and_script_links_are_skipped(self):
        urls = " ".join(self.by_url)
        self.assertNotIn("/info.html", urls)
        self.assertNotIn("/contact.html", urls)
        self.assertNotIn("trap.html", urls)

    def test_full_date_is_detected(self):
        link = self.by_url["https://www.robocupjunior.jp/2026aichi_outline.html"]
        self.assertEqual(link.date, datetime(2026, 8, 10, tzinfo=timezone.utc))

    def test_month_day_without_year_uses_reference_year(self):
        link = self.by_url[
            "https://www.robocupjunior.jp/docs/RCJJ-Soccer_Referee-Handbook_ver7.01.pdf"
        ]
        self.assertEqual(link.date, datetime(2026, 8, 3, tzinfo=timezone.utc))

    def test_external_link_kept(self):
        self.assertIn("https://drive.google.com/file/d/xyz/view", self.by_url)

    def test_link_text_is_captured(self):
        link = self.by_url["https://drive.google.com/file/d/xyz/view"]
        self.assertEqual(link.text, "オンステージ 開催要項")

    def test_title(self):
        self.assertEqual(self.page.title, "ロボカップジュニアジャパン")

    def test_visible_text_excludes_script(self):
        self.assertNotIn("var a", self.page.visible_text)
        self.assertIn("2026愛知大会 開催情報", self.page.visible_text)


class DateContextTest(unittest.TestCase):
    """日付はリンクと同じ段落のものを優先して拾う。"""

    def test_previous_paragraph_date_is_not_stolen(self):
        page = scrape.parse_page(
            '<p>2026年8月10日 <a href="/a.html">記事A</a></p>'
            '<p><a href="/b.html">記事B</a></p>',
            "https://example.com/",
            today=datetime(2026, 8, 12, tzinfo=timezone.utc),
        )
        by_url = {link.url: link for link in page.links}
        self.assertEqual(
            by_url["https://example.com/a.html"].date,
            datetime(2026, 8, 10, tzinfo=timezone.utc),
        )
        # 記事B に日付は無いので、記事A の日付を流用しない
        self.assertIsNone(by_url["https://example.com/b.html"].date)

    def test_table_layout_date_in_adjacent_cell(self):
        page = scrape.parse_page(
            "<table><tr><td>2026年7月1日</td>"
            '<td><a href="/c.html">サッカー説明会</a></td></tr></table>',
            "https://example.com/",
            today=datetime(2026, 8, 12, tzinfo=timezone.utc),
        )
        self.assertEqual(page.links[0].date, datetime(2026, 7, 1, tzinfo=timezone.utc))


class MonthDayRolloverTest(unittest.TestCase):
    def test_future_month_day_rolls_back_a_year(self):
        page = scrape.parse_page(
            '<p>12/25 <a href="/x.html">クリスマス告知</a></p>',
            "https://example.com/",
            today=datetime(2026, 1, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(page.links[0].date, datetime(2025, 12, 25, tzinfo=timezone.utc))


class HelpersTest(unittest.TestCase):
    def test_normalize_url_drops_fragment(self):
        self.assertEqual(
            scrape.normalize_url("https://example.com/a?b=1#frag"),
            "https://example.com/a?b=1",
        )

    def test_filename_title(self):
        self.assertEqual(
            scrape.filename_title("https://x.jp/docs/RCJJ2026_Guideline_V1.0.pdf"),
            "RCJJ2026_Guideline_V1.0.pdf",
        )

    def test_filename_title_decodes_japanese(self):
        self.assertEqual(
            scrape.filename_title("https://x.jp/docs/%E3%83%AB%E3%83%BC%E3%83%AB.pdf"),
            "ルール.pdf",
        )

    def test_broken_html_still_yields_links(self):
        page = scrape.parse_page(
            '<div><a href="/a.html">A<div>unclosed', "https://example.com/"
        )
        self.assertEqual(page.links[0].url, "https://example.com/a.html")


if __name__ == "__main__":
    unittest.main()
