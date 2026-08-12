"""HTTP 取得（標準ライブラリのみ）。

日本語の古いサイト（Shift_JIS など）も読めるように文字コードを推定する。
"""

from __future__ import annotations

import gzip
import re
import socket
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field

USER_AGENT = (
    "rcj-news-bot/1.0 (+https://github.com/rubicontmt9/rcj-news-bot) "
    "Python-urllib"
)

#: 最大受信サイズ。ルール PDF が数 MB あるので余裕を持たせる。
MAX_BYTES = 12 * 1024 * 1024

_META_CHARSET = re.compile(
    rb"""<meta[^>]*?charset\s*=\s*["']?\s*([a-zA-Z0-9_:.\-]+)""", re.IGNORECASE
)
_XML_ENCODING = re.compile(
    rb"""<\?xml[^>]*?encoding\s*=\s*["']([a-zA-Z0-9_:.\-]+)["']""", re.IGNORECASE
)

#: 推定に失敗したときに順に試す文字コード
_FALLBACK_ENCODINGS = ("utf-8", "cp932", "euc-jp", "iso-2022-jp", "latin-1")

#: Python が知らない別名の対応表
_ENCODING_ALIASES = {
    "shift-jis": "cp932",
    "shift_jis": "cp932",
    "sjis": "cp932",
    "x-sjis": "cp932",
    "windows-31j": "cp932",
    "euc_jp": "euc-jp",
    "utf8": "utf-8",
}


class FetchError(Exception):
    """取得に失敗した（リトライ後も回復しなかった）。"""


@dataclass
class Response:
    url: str
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    def header(self, name: str) -> str | None:
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return None

    def text(self) -> str:
        """本文を文字列に復号する。"""
        return decode_body(self.body, self.header("Content-Type"))


def _normalize_encoding(name: str | None) -> str | None:
    if not name:
        return None
    cleaned = name.strip().strip("\"'").lower()
    cleaned = _ENCODING_ALIASES.get(cleaned, cleaned)
    try:
        "".encode(cleaned)
    except LookupError:
        return None
    return cleaned


def _sniff_encoding(body: bytes) -> str | None:
    head = body[:4096]
    for pattern in (_XML_ENCODING, _META_CHARSET):
        match = pattern.search(head)
        if match:
            encoding = _normalize_encoding(match.group(1).decode("ascii", "ignore"))
            if encoding:
                return encoding
    return None


def _mojibake_score(text: str) -> int:
    """文字化けらしさを数える（小さいほどまとも）。

    EUC-JP のページを cp932 として読むと例外は出ないまま半角カナの羅列に
    なるため、「例外が出なかった最初の候補」を採用すると誤判定する。
    そこで宣言が無いときだけ、結果を見比べて一番まともなものを選ぶ。
    """
    score = 0
    for char in text:
        code = ord(char)
        if char == "�":
            score += 5
        elif 0xFF61 <= code <= 0xFF9F:  # 半角カナ（本文で多用されることはまず無い）
            score += 3
        elif code < 0x20 and char not in "\t\r\n":
            score += 3
        elif 0xE000 <= code <= 0xF8FF:  # 私用領域
            score += 2
    return score


def decode_body(body: bytes, content_type: str | None = None) -> str:
    """Content-Type / meta タグ / 総当たりの順で文字コードを決めて復号する。"""
    if not body:
        return ""

    # 1. サイトが宣言している文字コードを最優先で信用する
    declared: list[str] = []
    if content_type and "charset=" in content_type.lower():
        normalized = _normalize_encoding(
            content_type.lower().split("charset=", 1)[1].split(";")[0]
        )
        if normalized:
            declared.append(normalized)

    sniffed = _sniff_encoding(body)
    if sniffed:
        declared.append(sniffed)

    for encoding in declared:
        try:
            return body.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue

    # 2. 宣言が無い／間違っている場合は、復号できた候補のうち最もまともなものを選ぶ
    best: tuple[int, str] | None = None
    for encoding in _FALLBACK_ENCODINGS:
        try:
            decoded = body.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        score = _mojibake_score(decoded)
        if score == 0:
            return decoded
        if best is None or score < best[0]:
            best = (score, decoded)

    if best is not None:
        return best[1]
    return body.decode("utf-8", errors="replace")


def _decompress(body: bytes, encoding: str | None) -> bytes:
    if not body or not encoding:
        return body
    encoding = encoding.lower()
    try:
        if "gzip" in encoding:
            return gzip.decompress(body)
        if "deflate" in encoding:
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
    except (OSError, zlib.error):
        # 圧縮ヘッダが嘘だった場合は生のまま返す
        return body
    return body


def fetch(
    url: str,
    *,
    timeout: int = 25,
    retries: int = 3,
    etag: str | None = None,
    last_modified: str | None = None,
    accept: str | None = None,
) -> Response:
    """URL を取得する。

    ``etag`` / ``last_modified`` を渡すと条件付き GET を行い、変化が無ければ
    ステータス 304 の ``Response`` を返す（更新監視で使う）。
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "ja,en;q=0.8",
    }
    if accept:
        headers["Accept"] = accept
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as raw:
                body = raw.read(MAX_BYTES)
                response_headers = dict(raw.headers.items())
                body = _decompress(body, raw.headers.get("Content-Encoding"))
                return Response(
                    url=raw.geturl(),
                    status=raw.status,
                    headers=response_headers,
                    body=body,
                )
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return Response(url=url, status=304, headers=dict(exc.headers.items()))
            last_error = exc
            # 4xx は繰り返しても同じ結果なので即あきらめる（429 は待って再試行）
            if 400 <= exc.code < 500 and exc.code != 429:
                break
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            last_error = exc

        if attempt < retries - 1:
            time.sleep(2 ** attempt)

    raise FetchError(f"{url}: {last_error}") from last_error


def fetch_first(
    urls: list[str],
    *,
    timeout: int = 25,
    retries: int = 3,
    accept: str | None = None,
) -> Response:
    """候補 URL を順に試し、最初に成功したレスポンスを返す。

    サイト改装で URL が変わっても代替候補で拾えるようにするための仕組み。
    """
    errors: list[str] = []
    for url in urls:
        try:
            response = fetch(url, timeout=timeout, retries=retries, accept=accept)
        except FetchError as exc:
            errors.append(str(exc))
            continue
        if response.status == 200 and response.body:
            return response
        errors.append(f"{url}: HTTP {response.status}")
    raise FetchError("; ".join(errors) if errors else "候補URLが空です")
