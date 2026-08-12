"""フィードを持たないサイト向けの HTML 解析（html.parser のみ使用）。

やることは 2 つだけ。
1. リンク（テキスト＋URL＋近くにある日付）の抽出
2. 更新監視用に「見える文字」だけを取り出す
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, urlunparse

#: リンク抽出時に無視するブロック（ナビやスクリプト）
_SKIP_CONTAINERS = {"script", "style", "nav", "header", "footer", "noscript", "select"}

_WHITESPACE = re.compile(r"[\s　]+")

_DATE_PATTERNS = (
    re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"),
    re.compile(r"(20\d{2})[-/\.](\d{1,2})[-/\.](\d{1,2})"),
)
_MONTH_DAY = re.compile(r"(?<!\d)(\d{1,2})[/月](\d{1,2})日?(?!\d)")


@dataclass
class Link:
    url: str
    text: str
    date: datetime | None = None


@dataclass
class ParsedPage:
    links: list[Link] = field(default_factory=list)
    visible_text: str = ""
    title: str = ""


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self.title = ""
        # (href, 開始時点の chunk 位置, テキスト片)
        self.anchors: list[tuple[str, int, list[str]]] = []
        self._container_stack: list[str] = []
        self._open_anchors: list[tuple[str, int, list[str]]] = []
        self._in_title = False

    # --- HTMLParser の実装 ---------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _SKIP_CONTAINERS:
            self._container_stack.append(tag)
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "br":
            self.chunks.append(" ")
            return
        if tag == "a" and not self._container_stack:
            href = ""
            for name, value in attrs:
                if name.lower() == "href" and value:
                    href = value.strip()
                    break
            if href:
                anchor = (href, len(self.chunks), [])
                self._open_anchors.append(anchor)
                self.anchors.append(anchor)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "br":
            self.chunks.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_CONTAINERS:
            if tag in self._container_stack:
                # 対応する開始タグまで巻き戻す（閉じ忘れに耐える）
                index = len(self._container_stack) - 1 - self._container_stack[::-1].index(tag)
                del self._container_stack[index:]
            return
        if tag == "title":
            self._in_title = False
            return
        if tag == "a" and self._open_anchors:
            self._open_anchors.pop()
        if tag in {"td", "th"}:
            self.chunks.append(_CELL_BREAK)
        elif tag in {"p", "div", "li", "tr", "table", "h1", "h2", "h3", "h4", "dt", "dd"}:
            self.chunks.append(_BLOCK_BREAK)

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._container_stack:
            return
        self.chunks.append(data)
        for _href, _start, texts in self._open_anchors:
            texts.append(data)


def _normalize(text: str) -> str:
    return _WHITESPACE.sub(" ", html.unescape(text)).strip()


#: 段落（p / div / li / 見出し / tr）の区切り。ここを越えて日付を探すことはしない
_BLOCK_BREAK = "\n"
#: 表のセル（td / th）の区切り。「日付セル｜リンクセル」の並びのために越えられる
_CELL_BREAK = "\x00"


def _block_context(
    chunks: list[str], position: int, *, cross_cells: bool, limit: int
) -> str:
    """リンクの手前の文字を集める。

    ニュース一覧では日付はリンクと同じ段落（``<p>2026年8月10日 <a>…``）か、
    表の隣のセル（``<td>2026年8月10日</td><td><a>…``）にある。
    そのため段落の境界は絶対に越えず、セルの境界だけ 1 段階だけ越えて探す。
    段落を越えて遡ると、1 つ前の記事の日付を拾ってしまう。
    """
    parts: list[str] = []
    index = position - 1
    while index >= 0 and len(parts) < limit:
        chunk = chunks[index]
        if chunk == _BLOCK_BREAK:
            break
        if chunk == _CELL_BREAK:
            if not cross_cells:
                break
        else:
            parts.append(chunk)
        index -= 1
    return _normalize("".join(reversed(parts)))


def _after_context(chunks: list[str], position: int, *, limit: int) -> str:
    """リンクの直後の文字を、同じ段落の範囲だけ集める。"""
    parts: list[str] = []
    for chunk in chunks[position:position + limit]:
        if chunk in (_BLOCK_BREAK, _CELL_BREAK):
            break
        parts.append(chunk)
    return _normalize("".join(parts))


def _find_date(text: str, *, today: datetime | None = None) -> datetime | None:
    """文字列から日付を拾う。年が無い ``8/12`` 形式にも対応する。"""
    for pattern in _DATE_PATTERNS:
        match = pattern.search(text)
        if match:
            year, month, day = (int(part) for part in match.groups())
            try:
                return datetime(year, month, day, tzinfo=timezone.utc)
            except ValueError:
                continue

    reference = today or datetime.now(timezone.utc)
    match = _MONTH_DAY.search(text)
    if match:
        month, day = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12 and 1 <= day <= 31:
            for year in (reference.year, reference.year - 1):
                try:
                    candidate = datetime(year, month, day, tzinfo=timezone.utc)
                except ValueError:
                    continue
                # 未来日は年をまたいだ表記と判断して 1 年戻す
                if candidate <= reference:
                    return candidate
            return None
    return None


def normalize_url(url: str) -> str:
    """比較用に URL を整える（末尾 ``#...`` や空クエリを落とす）。"""
    try:
        parts = urlparse(url)
    except ValueError:
        return url
    return urlunparse(parts._replace(fragment=""))


def parse_page(
    html_text: str,
    base_url: str,
    *,
    today: datetime | None = None,
) -> ParsedPage:
    """HTML からリンクと可視テキストを取り出す。"""
    parser = _PageParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:  # noqa: BLE001 - 壊れた HTML でも拾えた分は使う
        pass

    joined = "".join(parser.chunks).replace(_CELL_BREAK, " ")
    visible_text = _WHITESPACE.sub(" ", joined.replace("\n", "\n "))
    lines = [line.strip() for line in visible_text.split("\n")]
    visible_text = "\n".join(line for line in lines if line)

    links: list[Link] = []
    seen: set[str] = set()
    for href, position, texts in parser.anchors:
        text = _normalize("".join(texts))
        absolute = normalize_url(urljoin(base_url, href))
        if not absolute.lower().startswith(("http://", "https://")):
            continue
        key = f"{absolute}\n{text}"
        if key in seen:
            continue
        seen.add(key)

        # リンクの前後の文字から日付を探す（「2026年8月12日 ○○のお知らせ」等）。
        # まず同じ段落だけ見て、見つからなければ 1 つ前の段落まで広げる。
        after = _after_context(parser.chunks, position, limit=6)
        date = None
        for cross_cells in (False, True):
            before = _block_context(
                parser.chunks, position, cross_cells=cross_cells, limit=12
            )
            date = _find_date(f"{before[-120:]} {text} {after[:60]}", today=today)
            if date:
                break
        links.append(Link(url=absolute, text=text, date=date))

    return ParsedPage(links=links, visible_text=visible_text, title=_normalize(parser.title))


def filename_title(url: str) -> str:
    """PDF などリンク文字が無いときにファイル名からタイトルを作る。"""
    path = urlparse(url).path
    name = path.rsplit("/", 1)[-1] or path
    try:
        from urllib.parse import unquote

        name = unquote(name)
    except Exception:  # noqa: BLE001 - 復号できなければそのまま
        pass
    return name or url
