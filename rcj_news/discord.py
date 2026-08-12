"""Discord webhook への送信。"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from .fetch import USER_AGENT

#: 連投で 429 を貰わないよう、メッセージ間に置く待ち時間（秒）
POST_INTERVAL = 1.2
MAX_ATTEMPTS = 4


class DiscordError(Exception):
    pass


def _post_once(webhook_url: str, payload: dict, timeout: int) -> tuple[int, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def post_message(webhook_url: str, payload: dict, *, timeout: int = 20) -> None:
    """1 通送る。429 は Retry-After に従って待ってから再送する。"""
    last_detail = ""
    for attempt in range(MAX_ATTEMPTS):
        try:
            status, text = _post_once(webhook_url, payload, timeout)
        except (urllib.error.URLError, OSError) as exc:
            last_detail = str(exc)
            status, text = 0, str(exc)

        if 200 <= status < 300:
            return

        last_detail = f"HTTP {status}: {text[:300]}"

        if status == 429:
            wait = 5.0
            try:
                data = json.loads(text)
                wait = float(data.get("retry_after", wait))
                # 単位がミリ秒で返る場合がある
                if wait > 120:
                    wait = wait / 1000.0
            except (ValueError, TypeError):
                pass
            time.sleep(min(wait + 0.5, 30))
            continue

        # 400 番台（URL 間違い・payload 不正）は再送しても直らない
        if 400 <= status < 500:
            raise DiscordError(last_detail)

        time.sleep(2 ** attempt)

    raise DiscordError(f"送信できませんでした: {last_detail}")


def post_messages(webhook_url: str, payloads: list[dict], *, timeout: int = 20) -> int:
    """複数通を順に送る。送信できた通数を返す。"""
    sent = 0
    for index, payload in enumerate(payloads):
        if index:
            time.sleep(POST_INTERVAL)
        post_message(webhook_url, payload, timeout=timeout)
        sent += 1
    return sent
