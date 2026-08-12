"""既読状態の保存（同じ記事を毎朝送らないための仕組み）。

state/seen.json をリポジトリにコミットして持ち回る。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

STATE_VERSION = 1

#: 1 ソースあたりに覚えておく既読 ID の数（古いものから捨てる）
MAX_SEEN_PER_SOURCE = 400
#: watch ソースが覚えておくリンク数
MAX_KNOWN_LINKS = 400


class State:
    def __init__(self, path: Path, data: dict | None = None) -> None:
        self.path = path
        self.data = data or {"version": STATE_VERSION, "sources": {}}
        self.data.setdefault("sources", {})
        #: 保存済みの state が存在したか（初回実行の判定に使う）
        self.existed = bool(data)

    # --- 読み書き -------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> State:
        path = Path(path)
        if not path.exists():
            return cls(path, None)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # 壊れていたら作り直す（履歴が消えるだけで動作は続く）
            return cls(path, None)
        if not isinstance(raw, dict):
            return cls(path, None)
        return cls(path, raw)

    def save(self) -> None:
        self.data["version"] = STATE_VERSION
        self.data["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True)
        self.path.write_text(payload + "\n", encoding="utf-8")

    # --- ソース単位のアクセス -------------------------------------------
    def source(self, source_id: str) -> dict:
        return self.data["sources"].setdefault(source_id, {})

    def is_new_source(self, source_id: str) -> bool:
        """このソースをまだ一度も処理していない（初回）か。"""
        return "seen" not in self.source(source_id)

    def seen_ids(self, source_id: str) -> set[str]:
        return set(self.source(source_id).get("seen", []))

    def mark_seen(self, source_id: str, uids: list[str]) -> None:
        entry = self.source(source_id)
        seen: list[str] = list(entry.get("seen", []))
        known = set(seen)
        for uid in uids:
            if uid not in known:
                seen.append(uid)
                known.add(uid)
        entry["seen"] = seen[-MAX_SEEN_PER_SOURCE:]

    def known_links(self, source_id: str) -> set[str]:
        return set(self.source(source_id).get("known_links", []))

    def set_known_links(self, source_id: str, urls: list[str]) -> None:
        entry = self.source(source_id)
        # 新しいものを優先して残す
        deduped: list[str] = []
        for url in urls:
            if url not in deduped:
                deduped.append(url)
        entry["known_links"] = deduped[:MAX_KNOWN_LINKS]

    def conditional_headers(self, source_id: str) -> tuple[str | None, str | None]:
        entry = self.source(source_id)
        return entry.get("etag"), entry.get("last_modified")

    def record_fetch(
        self,
        source_id: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        content_hash: str | None = None,
    ) -> None:
        entry = self.source(source_id)
        if etag is not None:
            entry["etag"] = etag
        if last_modified is not None:
            entry["last_modified"] = last_modified
        if content_hash is not None:
            entry["content_hash"] = content_hash

    def content_hash(self, source_id: str) -> str | None:
        return self.source(source_id).get("content_hash")

    def record_result(self, source_id: str, *, error: str | None, used_url: str | None) -> None:
        entry = self.source(source_id)
        now = datetime.now(timezone.utc).isoformat()
        if error:
            entry["last_error"] = error
            entry["last_error_at"] = now
        else:
            entry["last_ok"] = now
            entry.pop("last_error", None)
            entry.pop("last_error_at", None)
        if used_url:
            entry["used_url"] = used_url

    def prune(self, valid_source_ids: set[str]) -> None:
        """設定から消えたソースの記録を捨てる。"""
        for source_id in list(self.data["sources"]):
            if source_id not in valid_source_ids:
                del self.data["sources"][source_id]
