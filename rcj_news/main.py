"""エントリポイント。

    python -m rcj_news.main --dry-run     # 送信せずに内容だけ確認
    python -m rcj_news.main               # Discord へ送信
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import render
from .classify import normalize
from .collect import Collector
from .discord import DiscordError, post_messages
from .models import Item, SourceResult
from .state import State

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "sources.json"
DEFAULT_STATE = REPO_ROOT / "state" / "seen.json"

#: webhook URL を探す環境変数（上から順に見る）
WEBHOOK_ENV_NAMES = (
    "DISCORD_WEBHOOK_URL",
    "DISCORD_WEBHOOK",
    "RCJ_DISCORD_WEBHOOK_URL",
    "WEBHOOK_URL",
)


def load_config(path: Path) -> dict:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"設定ファイルが見つかりません: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"設定ファイルの JSON が壊れています: {path}: {exc}") from None

    if not isinstance(config.get("sources"), list) or not config["sources"]:
        raise SystemExit(f"sources が定義されていません: {path}")

    ids = [source.get("id") for source in config["sources"]]
    if len(ids) != len(set(ids)):
        duplicates = {name for name in ids if ids.count(name) > 1}
        raise SystemExit(f"sources の id が重複しています: {sorted(duplicates)}")
    for source in config["sources"]:
        if not source.get("id") or not source.get("urls"):
            raise SystemExit(f"id と urls は必須です: {source}")
    return config


def find_webhook_url(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit.strip()
    for name in WEBHOOK_ENV_NAMES:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


def dedupe_across_sources(items: list[Item], config: dict) -> list[Item]:
    """同じお知らせが複数の情報源に出た場合、1 件だけ残す。

    千葉ノードの記事が関東ブロックのサイトにも載る（URL は別）ことがあるため、
    タイトルで重ね合わせ、より地元の地域（設定の並び順が先）を残す。
    """
    order = {
        region["id"]: index for index, region in enumerate(config.get("regions", []))
    }
    best: dict[str, Item] = {}
    for item in items:
        key = normalize(item.title)
        if not key:
            best[item.url] = item
            continue
        current = best.get(key)
        if current is None or order.get(item.region, 999) < order.get(current.region, 999):
            best[key] = item

    kept = {id(item) for item in best.values()}
    return [item for item in items if id(item) in kept]


def select_new_items(
    results: list[SourceResult],
    state: State,
    options: dict,
    *,
    force: bool,
) -> tuple[list[Item], int]:
    """未読の項目だけを取り出し、既読として記録する。

    初回実行のソースは、過去記事が一気に流れないよう最新数件に絞る。
    """
    first_run_limit = int(options.get("first_run_items_per_source", 3))
    max_total = int(options.get("max_items_total", 40))

    selected: list[Item] = []
    for result in results:
        if not result.ok:
            continue
        is_first_run = state.is_new_source(result.source_id)
        seen = set() if force else state.seen_ids(result.source_id)
        fresh = [item for item in result.items if item.uid not in seen]

        if is_first_run and not force and first_run_limit >= 0:
            # 新しい順に並んでいるので先頭だけ投稿し、残りは既読にする
            state.mark_seen(result.source_id, [item.uid for item in result.items])
            fresh = fresh[:first_run_limit]
        else:
            state.mark_seen(result.source_id, [item.uid for item in result.items])

        selected.extend(fresh)

    # 日付があるものを新しい順に、日付不明のものは後ろにまとめる
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    selected.sort(
        key=lambda item: (item.published is not None, item.published or oldest),
        reverse=True,
    )

    dropped = 0
    if max_total > 0 and len(selected) > max_total:
        dropped = len(selected) - max_total
        selected = selected[:max_total]
    return selected, dropped


def write_step_summary(text: str) -> None:
    """GitHub Actions の実行サマリに結果を残す（ログを追わずに状況が分かるように）。"""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    except OSError:
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ロボカップジュニアのニュース／ルール情報を集めて Discord に送る"
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="設定ファイル")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE, help="既読状態ファイル")
    parser.add_argument("--webhook", default=None, help="Discord webhook URL（環境変数より優先）")
    parser.add_argument(
        "--dry-run", action="store_true", help="送信せずに標準出力へ表示する"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="既読を無視して取得できた全件を対象にする（動作確認用）",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="送信せず既読状態だけを作る（初回のまとめ投稿を避けたいとき）",
    )
    parser.add_argument(
        "--no-save", action="store_true", help="既読状態を書き込まない"
    )
    parser.add_argument("--only", default=None, help="指定した id のソースだけ処理する（カンマ区切り）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    config = load_config(args.config)
    options = config.get("options", {})
    tz = ZoneInfo(config.get("timezone", "Asia/Tokyo"))
    now = datetime.now(tz)

    sources = config["sources"]
    if args.only:
        wanted = {name.strip() for name in args.only.split(",") if name.strip()}
        sources = [source for source in sources if source.get("id") in wanted]
        if not sources:
            print(f"該当するソースがありません: {sorted(wanted)}", file=sys.stderr)
            return 2

    state = State.load(args.state)
    collector = Collector(config, state)
    results = collector.collect(sources)

    prefix_note = None
    if not state.existed and not args.force:
        prefix_note = "（初回実行のため、各情報源の最新数件のみ表示しています）"

    items, dropped = select_new_items(results, state, options, force=args.force)
    items = dedupe_across_sources(items, config)

    truncated_note = None
    source_truncated = sum(result.truncated for result in results)
    if dropped or source_truncated:
        parts = []
        if dropped:
            parts.append(f"全体上限で {dropped} 件省略")
        if source_truncated:
            parts.append(f"情報源ごとの上限で {source_truncated} 件省略")
        truncated_note = " / ".join(parts)

    failed = [result for result in results if not result.ok]
    summary_lines = [
        f"### 収集結果 {now:%Y-%m-%d %H:%M} JST",
        "",
        f"- 新規: **{len(items)}** 件",
        f"- 情報源: {len(results)} 件中 {len(failed)} 件が取得失敗",
    ]
    for result in results:
        status = "OK" if result.ok else f"NG ({result.error})"
        summary_lines.append(f"  - `{result.source_id}` {len(result.items)}件 {status}")

    if args.dry_run or args.seed:
        print(render.render_plain(items, results, tz))
        if args.seed:
            print("\n--seed: 送信せず既読状態のみ更新します")
        if not args.no_save and args.seed:
            state.prune({source["id"] for source in config["sources"]})
            state.save()
        write_step_summary("\n".join(summary_lines))
        return 0

    post_when_empty = bool(options.get("post_when_empty", True))
    if not items and not failed and not post_when_empty:
        print("新規なし。post_when_empty=false のため送信しません。")
    else:
        webhook_url = find_webhook_url(args.webhook)
        if not webhook_url:
            print(
                "Discord webhook URL が見つかりません。"
                f"次のいずれかの環境変数を設定してください: {', '.join(WEBHOOK_ENV_NAMES)}",
                file=sys.stderr,
            )
            return 2

        messages = render.build_messages(
            items,
            results,
            config,
            now,
            tz,
            truncated_note=truncated_note,
            prefix_note=prefix_note,
        )
        try:
            sent = post_messages(webhook_url, messages)
        except DiscordError as exc:
            print(f"Discord への送信に失敗しました: {exc}", file=sys.stderr)
            write_step_summary("\n".join(summary_lines + ["", f"⚠️ 送信失敗: {exc}"]))
            return 1
        print(f"Discord へ {sent} 通送信しました（新規 {len(items)} 件）")

    if not args.no_save:
        state.prune({source["id"] for source in config["sources"]})
        state.save()

    write_step_summary("\n".join(summary_lines))
    # 情報源が全滅している場合だけ異常終了にする（一部失敗は通知で分かる）
    if failed and len(failed) == len(results):
        print("すべての情報源が取得に失敗しました。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
