"""RSS 2.0 / RDF / Atom の解析（xml.etree のみ使用）。"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree


class FeedParseError(Exception):
    """XML として読めない、またはフィードではない。"""


def _localname(tag: str) -> str:
    """``{namespace}title`` → ``title``。"""
    return tag.rsplit("}", 1)[-1].lower() if "}" in tag else tag.lower()


def _text(element: ElementTree.Element | None) -> str:
    if element is None:
        return ""
    # 子要素に散った文字も拾う（<title>a<b>c</b></title> 対策）
    return html.unescape("".join(element.itertext())).strip()


def parse_datetime(value: str) -> datetime | None:
    """RFC 822 / ISO 8601 のどちらでも日時として解釈する。"""
    value = (value or "").strip()
    if not value:
        return None

    # RSS の pubDate: Tue, 11 Aug 2026 09:00:00 +0900
    try:
        parsed = parsedate_to_datetime(value)
        if parsed is not None:
            return _as_utc(parsed)
    except (TypeError, ValueError, IndexError):
        pass

    # Atom の updated: 2026-08-11T09:00:00+09:00 / ...Z
    candidate = value.replace("Z", "+00:00")
    # 小数秒の桁数が多いと fromisoformat が落ちる版があるので削る
    candidate = re.sub(r"\.\d+", "", candidate)
    try:
        return _as_utc(datetime.fromisoformat(candidate))
    except ValueError:
        pass

    match = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", value)
    if match:
        year, month, day = (int(part) for part in match.groups())
        try:
            return datetime(year, month, day, tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _pick_link(entry: ElementTree.Element) -> str:
    """記事本体の URL を選ぶ。Atom の rel 属性を考慮する。"""
    fallback = ""
    for child in entry:
        if _localname(child.tag) != "link":
            continue
        href = (child.get("href") or "").strip()
        rel = (child.get("rel") or "alternate").strip().lower()
        if href:
            if rel == "alternate":
                return href
            if not fallback:
                fallback = href
            continue
        # RSS は <link>URL</link>
        text = _text(child)
        if text and not fallback:
            fallback = text
    return fallback


def _pick_categories(entry: ElementTree.Element) -> list[str]:
    categories: list[str] = []
    for child in entry:
        if _localname(child.tag) != "category":
            continue
        value = (child.get("term") or _text(child)).strip()
        if value:
            categories.append(value)
    return categories


class FeedEntry:
    """フィード 1 件分。要約本文は意図的に持たない（要約は投稿しない方針）。"""

    __slots__ = ("title", "url", "published", "native_id", "categories")

    def __init__(
        self,
        title: str,
        url: str,
        published: datetime | None,
        native_id: str | None,
        categories: list[str],
    ) -> None:
        self.title = title
        self.url = url
        self.published = published
        self.native_id = native_id
        self.categories = categories

    def __repr__(self) -> str:  # pragma: no cover - デバッグ用
        return f"FeedEntry(title={self.title!r}, url={self.url!r})"


_ENTRY_TAGS = {"item", "entry"}
_TITLE_TAGS = ("title",)
_DATE_TAGS = ("pubdate", "published", "updated", "date", "modified", "created")
_ID_TAGS = ("guid", "id")


def parse_feed(xml_text: str) -> list[FeedEntry]:
    """RSS/Atom/RDF を ``FeedEntry`` のリストにする。"""
    text = xml_text.lstrip("﻿ \t\r\n")
    if not text:
        raise FeedParseError("空のレスポンス")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise FeedParseError(f"XML として解析できません: {exc}") from exc

    entries: list[FeedEntry] = []
    # RSS は channel/item、Atom は feed/entry、RDF は rdf:RDF/item と階層が違うので
    # 深さを問わず item/entry を拾う
    for node in root.iter():
        if _localname(node.tag) not in _ENTRY_TAGS:
            continue

        fields: dict[str, str] = {}
        for child in node:
            name = _localname(child.tag)
            if name not in fields:
                value = _text(child)
                if value:
                    fields[name] = value

        title = ""
        for tag in _TITLE_TAGS:
            if fields.get(tag):
                title = fields[tag]
                break

        url = _pick_link(node)

        published = None
        for tag in _DATE_TAGS:
            if fields.get(tag):
                published = parse_datetime(fields[tag])
                if published:
                    break

        native_id = None
        for tag in _ID_TAGS:
            if fields.get(tag):
                native_id = fields[tag]
                break

        if not title and not url:
            continue

        entries.append(
            FeedEntry(
                title=title or url,
                url=url,
                published=published,
                native_id=native_id,
                categories=_pick_categories(node),
            )
        )

    if not entries and _localname(root.tag) not in {"rss", "feed", "rdf"}:
        raise FeedParseError(f"フィードではないようです (root=<{root.tag}>)")
    return entries
