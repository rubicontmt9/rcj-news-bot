"""アプリ全体で使うデータ構造。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Item:
    """Discord に流す 1 件の情報（ニュース記事・資料・ルール更新など）。"""

    source_id: str
    source_name: str
    region: str
    title: str
    url: str
    published: datetime | None = None
    leagues: list[str] = field(default_factory=list)
    is_general: bool = False
    #: フィードの guid など、サイト側が持つ安定した識別子
    native_id: str | None = None
    #: 「更新されました」系（watch）の項目に付ける補足行
    notes: list[str] = field(default_factory=list)

    @property
    def uid(self) -> str:
        """既読判定に使う一意キー。

        サイト側の guid があればそれを、無ければ URL を使う。どちらも取れない
        場合だけタイトルにフォールバックする（タイトルは編集されうるので最後）。
        """
        basis = self.native_id or self.url or self.title
        digest = hashlib.sha1(f"{self.source_id}\n{basis}".encode()).hexdigest()
        return digest[:20]


@dataclass
class SourceResult:
    """1 つの情報源を処理した結果。健全性表示のためにエラーも持ち回る。"""

    source_id: str
    source_name: str
    region: str
    items: list[Item] = field(default_factory=list)
    used_url: str | None = None
    error: str | None = None
    #: 取得はできたが 1 件も項目が取れなかった（セレクタ腐りの可能性）
    empty: bool = False
    #: 件数上限で切り捨てた数
    truncated: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None
