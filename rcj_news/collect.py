"""情報源から項目を集める処理。

kind ごとの動き:
  feed  … RSS/Atom を解析。失敗したら html_fallback で HTML から拾う
  html  … ページ内のリンクを項目として扱う（新しい PDF 資料の検出に強い）
  watch … ページ内容のハッシュを比べ、変わったら「更新あり」＋新規リンクを報告
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timedelta, timezone

from . import feeds, scrape
from .classify import Classifier, match_any
from .fetch import FetchError, fetch, fetch_first
from .models import Item, SourceResult
from .state import State

#: watch で報告する新規リンクの最大数
MAX_NEW_LINKS_REPORTED = 5


def _passes_link_filters(url: str, source: dict) -> bool:
    must_contain = source.get("link_must_contain")
    if must_contain and not any(fragment.lower() in url.lower() for fragment in must_contain):
        return False
    must_not_contain = source.get("link_must_not_contain") or []
    return not any(fragment.lower() in url.lower() for fragment in must_not_contain)


def _passes_title_filters(title: str, source: dict) -> bool:
    blocked = source.get("title_must_not_contain") or []
    if blocked and match_any(title, blocked):
        return False
    minimum = source.get("min_title_length", 0)
    return len(title.strip()) >= minimum


def _too_old(published: datetime | None, max_age_days: int) -> bool:
    if published is None or max_age_days <= 0:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    return published < cutoff


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


class Collector:
    def __init__(self, config: dict, state: State) -> None:
        self.config = config
        self.options = config.get("options", {})
        self.state = state
        self.classifier = Classifier(
            config.get("leagues", {}), config.get("exclude_keywords", [])
        )
        self.timeout = int(self.options.get("http_timeout", 25))
        self.retries = int(self.options.get("http_retries", 3))
        self.max_age_days = int(self.options.get("max_age_days", 210))
        #: 全体の制限時間。複数サイトが同時に落ちていても、集まった分だけは
        #: 必ず Discord に届くようにするための保険。
        self.time_budget = int(self.options.get("total_time_budget", 600))
        self._deadline: float | None = None

    # --- 入口 -----------------------------------------------------------
    def collect(self, sources: list[dict]) -> list[SourceResult]:
        if self.time_budget > 0:
            self._deadline = time.monotonic() + self.time_budget

        results: list[SourceResult] = []
        for source in sources:
            if not source.get("enabled", True):
                continue
            if self._out_of_time():
                results.append(
                    SourceResult(
                        source_id=source["id"],
                        source_name=source.get("name", source["id"]),
                        region=source.get("region", "world"),
                        error=f"時間切れ（全体 {self.time_budget} 秒）のため今回は取得を省略",
                    )
                )
                continue
            results.append(self.collect_source(source))
        return results

    def _out_of_time(self) -> bool:
        return self._deadline is not None and time.monotonic() >= self._deadline

    def _remaining_timeout(self) -> int:
        """1 回の HTTP 取得に許す秒数。残り時間が少なければ短くする。"""
        if self._deadline is None:
            return self.timeout
        remaining = int(self._deadline - time.monotonic())
        return max(5, min(self.timeout, remaining))

    def collect_source(self, source: dict) -> SourceResult:
        result = SourceResult(
            source_id=source["id"],
            source_name=source.get("name", source["id"]),
            region=source.get("region", "world"),
        )
        kind = source.get("kind", "feed")
        try:
            if kind == "feed":
                self._collect_feed(source, result)
            elif kind == "html":
                self._collect_html(source, result)
            elif kind == "watch":
                self._collect_watch(source, result)
            else:
                result.error = f"未知の kind: {kind}"
        except FetchError as exc:
            result.error = f"取得失敗: {exc}"
        except feeds.FeedParseError as exc:
            result.error = f"解析失敗: {exc}"
        except Exception as exc:  # noqa: BLE001 - 1 ソースの失敗で全体を止めない
            result.error = f"{type(exc).__name__}: {exc}"

        self.state.record_result(
            source["id"], error=result.error, used_url=result.used_url
        )
        if result.ok:
            self._apply_limits(source, result)
        return result

    # --- kind 別の処理 --------------------------------------------------
    def _collect_feed(self, source: dict, result: SourceResult) -> None:
        try:
            response = fetch_first(
                source["urls"],
                timeout=self._remaining_timeout(),
                retries=self.retries,
                accept="application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
            )
            result.used_url = response.url
            entries = feeds.parse_feed(response.text())
        except (FetchError, feeds.FeedParseError) as exc:
            fallback = source.get("html_fallback")
            if not fallback:
                raise
            # フィードが無くなった／壊れている場合は HTML から拾い直す
            result.error = None
            html_source = dict(source)
            html_source.update(fallback)
            html_source["urls"] = [fallback["url"]]
            try:
                self._collect_html(html_source, result)
            except (FetchError, feeds.FeedParseError) as fallback_exc:
                # どこを直せばいいか分かるように、両方の理由を残す
                raise FetchError(
                    f"フィード({exc}) / HTML({fallback_exc}) のどちらも取得できません"
                ) from fallback_exc
            result.error = None
            return

        for entry in entries:
            if not entry.url:
                continue
            url = scrape.normalize_url(entry.url)
            if not _passes_link_filters(url, source):
                continue
            title = entry.title.strip() or scrape.filename_title(url)
            if not _passes_title_filters(title, source):
                continue
            if _too_old(entry.published, self.max_age_days):
                continue

            haystack = " ".join([title, " ".join(entry.categories), url])
            item = self._build_item(source, result, title, url, entry.published, haystack)
            if item:
                item.native_id = entry.native_id or url
                result.items.append(item)

        result.empty = not result.items and not entries

    def _collect_html(self, source: dict, result: SourceResult) -> None:
        response = fetch_first(
            source["urls"], timeout=self._remaining_timeout(), retries=self.retries
        )
        result.used_url = response.url
        page = scrape.parse_page(response.text(), response.url)

        for link in page.links:
            if not _passes_link_filters(link.url, source):
                continue
            title = link.text.strip() or scrape.filename_title(link.url)
            if not _passes_title_filters(title, source):
                continue
            if _too_old(link.date, self.max_age_days):
                continue

            haystack = f"{title} {link.url}"
            item = self._build_item(source, result, title, link.url, link.date, haystack)
            if item:
                item.native_id = link.url
                result.items.append(item)

        result.empty = not page.links

    def _collect_watch(self, source: dict, result: SourceResult) -> None:
        source_id = source["id"]
        etag, last_modified = self.state.conditional_headers(source_id)

        response = None
        errors: list[str] = []
        for url in source["urls"]:
            try:
                response = fetch(
                    url,
                    timeout=self._remaining_timeout(),
                    retries=self.retries,
                    etag=etag,
                    last_modified=last_modified,
                )
            except FetchError as exc:
                errors.append(str(exc))
                continue
            if response.not_modified or (response.status == 200 and response.body):
                break
            errors.append(f"{url}: HTTP {response.status}")
            response = None

        if response is None:
            raise FetchError("; ".join(errors) or "候補URLが空です")

        result.used_url = response.url
        if response.not_modified:
            # サーバーが「変わっていない」と言っているので中身は見ない
            return

        page = scrape.parse_page(response.text(), response.url)
        # 見える文字だけを対象にする（スクリプト内の乱数やトークンで誤検知しないため）
        new_hash = _hash_text(page.visible_text or response.body.hex())
        old_hash = self.state.content_hash(source_id)

        current_links = [
            link.url for link in page.links if _passes_link_filters(link.url, source)
        ]
        known = self.state.known_links(source_id)
        new_links = [url for url in current_links if url not in known]

        self.state.record_fetch(
            source_id,
            etag=response.header("ETag"),
            last_modified=response.header("Last-Modified"),
            content_hash=new_hash,
        )
        self.state.set_known_links(source_id, current_links)

        if old_hash is None:
            # 初回は基準を作るだけ（変更ではない）
            return
        if old_hash == new_hash:
            return

        notes = [
            f"新しいリンク: [{scrape.filename_title(url)}]({url})"
            for url in new_links[:MAX_NEW_LINKS_REPORTED]
        ]
        if len(new_links) > MAX_NEW_LINKS_REPORTED:
            notes.append(f"ほか {len(new_links) - MAX_NEW_LINKS_REPORTED} 件のリンク追加")

        title = f"ページが更新されました: {source.get('name', source_id)}"
        item = self._build_item(
            source,
            result,
            title,
            response.url,
            datetime.now(timezone.utc),
            f"{title} {page.title}",
        )
        if item:
            item.notes = notes
            # 変更ごとに 1 回だけ通知されるよう、ハッシュを識別子に含める
            item.native_id = f"watch:{new_hash[:16]}"
            result.items.append(item)

    # --- 共通処理 -------------------------------------------------------
    def _build_item(
        self,
        source: dict,
        result: SourceResult,
        title: str,
        url: str,
        published: datetime | None,
        haystack: str,
    ) -> Item | None:
        include_general = source.get(
            "include_general", self.options.get("include_general", True)
        )
        keep, leagues, is_general = self.classifier.decide(
            haystack,
            forced_leagues=source.get("forced_leagues"),
            include_general=include_general,
        )
        if not keep:
            return None
        return Item(
            source_id=source["id"],
            source_name=source.get("name", source["id"]),
            region=source.get("region", "world"),
            title=title,
            url=url,
            published=published,
            leagues=leagues,
            is_general=is_general,
        )

    def _apply_limits(self, source: dict, result: SourceResult) -> None:
        """新しい順に並べ、1 ソースの件数上限を適用する。"""
        limit = int(
            source.get(
                "max_items_per_source", self.options.get("max_items_per_source", 8)
            )
        )
        items = _dedupe(result.items)
        items.sort(key=_sort_key, reverse=True)
        if limit > 0 and len(items) > limit:
            result.truncated = len(items) - limit
            items = items[:limit]
        result.items = items


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _sort_key(item: Item) -> datetime:
    return item.published or _EPOCH


def _dedupe(items: list[Item]) -> list[Item]:
    """同一ページへの複数リンク（画像＋文字リンクなど）を 1 件に寄せる。"""
    result: list[Item] = []
    seen: set[str] = set()
    for item in items:
        key = item.native_id or item.url
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result
