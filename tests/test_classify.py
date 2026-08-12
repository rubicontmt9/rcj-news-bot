import json
import unittest
from pathlib import Path

from rcj_news.classify import Classifier, match_any, normalize

CONFIG = json.loads((Path(__file__).resolve().parent.parent / "sources.json").read_text("utf-8"))


class NormalizeTest(unittest.TestCase):
    def test_fullwidth_and_case(self):
        self.assertEqual(normalize("ＳＯＣＣＥＲ"), "soccer")
        self.assertEqual(normalize("ｻｯｶｰ"), "サッカー")
        self.assertEqual(normalize("  a　b "), "a b")


class MatchAnyTest(unittest.TestCase):
    def test_ascii_word_boundary(self):
        # "open" が "opening" に誤ヒットしない
        self.assertFalse(match_any("Opening ceremony", ["open"]))
        self.assertTrue(match_any("Soccer Open League", ["open"]))

    def test_japanese_substring(self):
        self.assertTrue(match_any("【サッカー】連絡", ["サッカー"]))


class ClassifierTest(unittest.TestCase):
    def setUp(self):
        self.classifier = Classifier(
            CONFIG["leagues"], CONFIG["exclude_keywords"]
        )

    def decide(self, text, **kwargs):
        return self.classifier.decide(text, **kwargs)

    def test_soccer_is_kept(self):
        keep, leagues, general = self.decide("【サッカー】適用ルール（日本リーグ）")
        self.assertTrue(keep)
        self.assertEqual(leagues, ["soccer"])
        self.assertFalse(general)

    def test_onstage_is_kept(self):
        keep, leagues, _ = self.decide("【OnStage】事前連絡事項")
        self.assertTrue(keep)
        self.assertEqual(leagues, ["onstage"])

    def test_onstage_japanese(self):
        keep, leagues, _ = self.decide("オンステージ 開催要項を公開しました")
        self.assertTrue(keep)
        self.assertEqual(leagues, ["onstage"])

    def test_rescue_only_is_dropped(self):
        keep, _, _ = self.decide("2026千葉・レスキューラインルール事前連絡")
        self.assertFalse(keep)

    def test_rescue_english_is_dropped(self):
        keep, _, _ = self.decide("Rescue Maze rules update")
        self.assertFalse(keep)

    def test_soccer_plus_rescue_is_kept_as_soccer(self):
        keep, leagues, _ = self.decide("サッカー・レスキュー合同練習会のお知らせ")
        self.assertTrue(keep)
        self.assertEqual(leagues, ["soccer"])

    def test_both_leagues_detected(self):
        keep, leagues, _ = self.decide("サッカー／オンステージ 参加者へ")
        self.assertTrue(keep)
        self.assertEqual(sorted(leagues), ["onstage", "soccer"])

    def test_general_item_kept_when_enabled(self):
        keep, leagues, general = self.decide("2026関東ブロック大会 参加申込開始", include_general=True)
        self.assertTrue(keep)
        self.assertEqual(leagues, [])
        self.assertTrue(general)

    def test_general_item_dropped_when_disabled(self):
        keep, _, _ = self.decide("総会のお知らせ", include_general=False)
        self.assertFalse(keep)

    def test_forced_leagues_bypass_keywords(self):
        # ルール用リポジトリのコミットはタイトルにリーグ名が出ないことが多い
        keep, leagues, general = self.decide(
            "Update rules.tex", forced_leagues=["soccer"]
        )
        self.assertTrue(keep)
        self.assertEqual(leagues, ["soccer"])
        self.assertFalse(general)

    def test_forced_leagues_empty_means_general(self):
        keep, leagues, general = self.decide("Fix typo", forced_leagues=[])
        self.assertTrue(keep)
        self.assertEqual(leagues, [])
        self.assertTrue(general)

    def test_forced_leagues_overrides_rescue_exclusion(self):
        keep, leagues, _ = self.decide(
            "Rescue section moved", forced_leagues=["soccer"]
        )
        self.assertTrue(keep)
        self.assertEqual(leagues, ["soccer"])


if __name__ == "__main__":
    unittest.main()
