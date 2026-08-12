"""サッカー／オンステージの絞り込み。

判定はタイトルとカテゴリ、URL だけを見る（本文は取得も要約もしない）。
"""

from __future__ import annotations

import re
import unicodedata

#: 「サッカー」等は英字部分だけ単語境界を見たいので、英数字のみのキーワードを区別する
_ASCII_ONLY = re.compile(r"^[a-z0-9 \-@]+$")


def normalize(text: str) -> str:
    """全角→半角、小文字化して比較しやすくする。"""
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"[\s　]+", " ", folded).strip()


def _contains(haystack: str, needle: str) -> bool:
    """キーワード一致。英単語は前後が英字でないことを確認する。"""
    needle = normalize(needle)
    if not needle:
        return False
    if not _ASCII_ONLY.match(needle):
        return needle in haystack
    for match in re.finditer(re.escape(needle), haystack):
        before = haystack[match.start() - 1] if match.start() > 0 else ""
        after = haystack[match.end()] if match.end() < len(haystack) else ""
        if not before.isalnum() and not after.isalnum():
            return True
    return False


def match_any(text: str, keywords: list[str]) -> bool:
    haystack = normalize(text)
    return any(_contains(haystack, keyword) for keyword in keywords)


class Classifier:
    """設定ファイルの ``leagues`` / ``exclude_keywords`` に従って項目を振り分ける。"""

    def __init__(self, leagues: dict, exclude_keywords: list[str]) -> None:
        self.leagues = leagues
        self.exclude_keywords = exclude_keywords

    def leagues_for(self, text: str) -> list[str]:
        """本文中に現れるリーグ ID を、設定の並び順で返す。"""
        found = [
            league_id
            for league_id, league in self.leagues.items()
            if match_any(text, league.get("keywords", []))
        ]
        return found

    def is_excluded(self, text: str) -> bool:
        return match_any(text, self.exclude_keywords)

    def decide(
        self,
        text: str,
        *,
        forced_leagues: list[str] | None = None,
        include_general: bool = True,
    ) -> tuple[bool, list[str], bool]:
        """``(採用するか, リーグID一覧, 全般扱いか)`` を返す。

        判定の順序:

        1. ``forced_leagues`` が設定にあるソース（国際ルールの更新など）は
           タイトルにリーグ名が出なくても必ず採用する。
        2. サッカー／オンステージのどちらかに一致したら採用。
           （レスキューにも触れていても、サッカーの話なら残す）
        3. どのリーグにも一致せず、レスキュー等の除外語に一致したら捨てる。
        4. 残り（大会日程・申込など一般連絡）は ``include_general`` に従う。
        """
        if forced_leagues is not None:
            leagues = [
                league_id for league_id in forced_leagues if league_id in self.leagues
            ]
            return True, leagues, not leagues

        leagues = self.leagues_for(text)
        if leagues:
            return True, leagues, False

        if self.is_excluded(text):
            return False, [], False

        if include_general:
            return True, [], True
        return False, [], False
