"""Discord へ送る本文の組み立て。

方針: 記事の要約はしない。タイトル・日付・リンク・リーグ札だけを並べる。
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .models import Item, SourceResult

#: Discord の制限（余裕を持たせた値）
EMBED_DESCRIPTION_LIMIT = 3800
EMBEDS_PER_MESSAGE = 8
CONTENT_LIMIT = 1900

#: 情報源の状態に並べる行数の上限（毎朝の通知が埋まらないように）
MAX_HEALTH_LINES = 6

_WEEKDAYS_JA = ("月", "火", "水", "木", "金", "土", "日")

#: Markdown のリンク表記を壊す文字を無効化する
_MD_ESCAPE = re.compile(r"([\[\]\*_`~|])")


def escape_markdown(text: str) -> str:
    return _MD_ESCAPE.sub(r"\\\1", text)


def format_date(value: datetime | None, tz: ZoneInfo) -> str:
    if value is None:
        return ""
    local = value.astimezone(tz)
    return f"{local.month}/{local.day}"


def header_text(now: datetime) -> str:
    weekday = _WEEKDAYS_JA[now.weekday()]
    return (
        f"## ☀️ ロボカップジュニア 最新情報 "
        f"{now.year}/{now.month:02d}/{now.day:02d}({weekday})"
    )


def _league_badge(item: Item, config: dict) -> str:
    leagues = config.get("leagues", {})
    badges = []
    for league_id in item.leagues:
        league = leagues.get(league_id, {})
        emoji = league.get("emoji", "")
        label = league.get("label", league_id)
        badges.append(f"{emoji}{label}".strip())
    if not badges:
        general = config.get("general", {})
        badges.append(f"{general.get('emoji', '📢')}{general.get('label', '全般')}")
    return " ".join(badges)


def item_line(item: Item, config: dict, tz: ZoneInfo) -> str:
    """1 件を 1〜数行のテキストにする。"""
    badge = _league_badge(item, config)
    date = format_date(item.published, tz)
    title = escape_markdown(item.title.strip())
    if len(title) > 180:
        title = title[:179] + "…"

    meta = " ・ ".join(part for part in (date, item.source_name) if part)
    line = f"{badge} **[{title}]({item.url})**"
    if meta:
        line += f"\n　　{meta}"
    for note in item.notes:
        line += f"\n　　└ {note}"
    return line


def _chunk_lines(lines: list[str], limit: int) -> list[str]:
    """行を結合しつつ、上限文字数で分割する。"""
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for line in lines:
        addition = len(line) + 1
        if current and length + addition > limit:
            chunks.append("\n".join(current))
            current, length = [], 0
        current.append(line)
        length += addition
    if current:
        chunks.append("\n".join(current))
    return chunks


def build_embeds(
    items: list[Item],
    config: dict,
    tz: ZoneInfo,
    *,
    truncated_note: str | None = None,
) -> list[dict]:
    """地域ごとに embed を作る。"""
    regions = config.get("regions", [])
    order = {region["id"]: index for index, region in enumerate(regions)}
    by_region: dict[str, list[Item]] = {}
    for item in items:
        by_region.setdefault(item.region, []).append(item)

    embeds: list[dict] = []
    for region_id in sorted(by_region, key=lambda key: order.get(key, 999)):
        region = next((r for r in regions if r["id"] == region_id), {})
        region_items = by_region[region_id]
        lines = [item_line(item, config, tz) for item in region_items]
        for index, chunk in enumerate(_chunk_lines(lines, EMBED_DESCRIPTION_LIMIT)):
            emoji = region.get("emoji", "")
            label = region.get("label", region_id)
            title = f"{emoji} {label}".strip()
            if index > 0:
                title += " (続き)"
            else:
                title += f"（{len(region_items)}件）"
            embeds.append(
                {
                    "title": title,
                    "description": chunk,
                    "color": region.get("color", 5793266),
                }
            )

    if truncated_note and embeds:
        embeds[-1].setdefault("footer", {})["text"] = truncated_note
    return embeds


def health_embed(results: list[SourceResult]) -> dict | None:
    """取得できなかった情報源を知らせる embed（問題がある時だけ作る）。"""
    failed = [result for result in results if not result.ok]
    stale = [result for result in results if result.ok and result.empty]
    if not failed and not stale:
        return None

    lines: list[str] = []
    for result in failed:
        reason = (result.error or "").replace("\n", " ")
        if len(reason) > 160:
            reason = reason[:159] + "…"
        lines.append(f"❌ **{result.source_name}** — {reason}")
    for result in stale:
        lines.append(f"⚠️ **{result.source_name}** — 取得はできたが項目が 0 件")

    if len(lines) > MAX_HEALTH_LINES:
        hidden = len(lines) - MAX_HEALTH_LINES
        lines = lines[:MAX_HEALTH_LINES] + [f"…ほか {hidden} 件の情報源で問題あり"]

    description = "\n".join(lines)[:EMBED_DESCRIPTION_LIMIT]
    return {
        "title": "🔧 情報源の状態",
        "description": description,
        "color": 15105570,
        "footer": {"text": "sources.json の urls を直すと復活します"},
    }


def build_messages(
    items: list[Item],
    results: list[SourceResult],
    config: dict,
    now: datetime,
    tz: ZoneInfo,
    *,
    truncated_note: str | None = None,
    prefix_note: str | None = None,
) -> list[dict]:
    """Discord webhook へ渡す payload のリスト（長い場合は複数通に分ける）。"""
    embeds = build_embeds(items, config, tz, truncated_note=truncated_note)

    header = header_text(now)
    if prefix_note:
        header += f"\n{prefix_note}"
    if not items:
        header += "\n新しいお知らせはありませんでした。"
    header = header[:CONTENT_LIMIT]

    health = health_embed(results)
    if health:
        embeds.append(health)

    if not embeds:
        return [{"content": header}]

    messages: list[dict] = []
    for index in range(0, len(embeds), EMBEDS_PER_MESSAGE):
        batch = embeds[index:index + EMBEDS_PER_MESSAGE]
        payload: dict = {"embeds": batch}
        if index == 0:
            payload["content"] = header
        messages.append(payload)
    return messages


def render_plain(items: list[Item], results: list[SourceResult], tz: ZoneInfo) -> str:
    """端末で内容を確認するための簡易表示（--dry-run 用）。"""
    lines: list[str] = []
    for item in items:
        date = format_date(item.published, tz) or "----"
        leagues = ",".join(item.leagues) or "general"
        lines.append(f"[{item.region:5}] {date:>5} ({leagues}) {item.title} -> {item.url}")
        for note in item.notes:
            lines.append(f"          └ {note}")
    lines.append("")
    lines.append("--- 情報源の状態 ---")
    for result in results:
        if not result.ok:
            status = f"NG   {result.error}"
        elif result.empty:
            status = "空   項目が 0 件"
        else:
            status = f"OK   {len(result.items)}件"
        lines.append(f"{result.source_id:32} {status}")
    return "\n".join(lines)
