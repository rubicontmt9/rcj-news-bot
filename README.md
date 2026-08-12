# rcj-news-bot

ロボカップジュニアの**最新ニュース**と**ルール情報**を毎朝集めて、Discord に送る BOT です。

- 対象地域: **千葉 / 関東 / 日本 / 世界**
- 対象競技: **サッカー / オンステージ**（レスキュー等は除外）
- 送る内容: **タイトル・日付・リンクのみ**（本文の要約はしません）
- 毎朝 **7:00 (JST)** に GitHub Actions が自動実行
- 一度送った記事は再送しません（既読を `state/seen.json` に記録）
- 追加インストール不要（Python 標準ライブラリのみ・依存パッケージゼロ）

## セットアップ（残り 1 手順）

Discord の Webhook URL を **リポジトリの Secret** に登録してください。

1. Discord で対象チャンネル → 「チャンネルの編集」→「連携サービス」→「ウェブフックを作成」→ **ウェブフック URL をコピー**
2. GitHub のこのリポジトリ → **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `DISCORD_WEBHOOK_URL`
   - Secret: コピーした URL
3. **Actions → 毎朝のロボカップジュニア情報配信 → Run workflow** で試し打ちできます

> **Webhook について**
> このリポジトリに GitHub の Webhook（GitHub → Discord の通知連携）が設定済みでも、
> それは「push や PR を Discord に流す」ためのもので、BOT からニュースを投稿することはできません。
> ニュース配信には上記 Secret の登録が別途必要です。
> （`DISCORD_WEBHOOK` という名前で登録しても動きます）

## 手元で試す

```bash
# 送信せずに、集まった内容と各情報源の状態を表示（Webhook 不要）
python -m rcj_news.main --dry-run

# 特定の情報源だけ確認
python -m rcj_news.main --dry-run --only chiba-node-news,japan-docs

# 実際に送る
DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..." python -m rcj_news.main

# テスト（ネットワークに繋がりません）
python -m unittest discover -s tests
```

主なオプション

| オプション | 説明 |
| --- | --- |
| `--dry-run` | 送信せず標準出力に表示する |
| `--force` | 既読を無視して全件を対象にする（表示確認用） |
| `--seed` | 送信せず既読だけ記録する（初回のまとめ投稿を避けたいとき） |
| `--only ID,ID` | 指定した情報源だけ処理する |
| `--webhook URL` | 環境変数の代わりに URL を直接指定する |

## 情報源

すべて `sources.json` に書いてあります。`urls` は**候補リスト**で、上から順に試して最初に成功したものを使います。

| 地域 | 情報源 | 種類 |
| --- | --- | --- |
| 千葉 | 千葉ノードニュース | RSS |
| 関東 | 関東ブロックニュース / お知らせ | RSS |
| 関東 | 競技ルールページの更新監視 | 更新監視 |
| 日本 | ロボカップジュニアジャパン（トップ・競技ルール・公開資料） | HTML |
| 日本 | ロボカップ日本委員会（ジュニア関連のみ） | RSS |
| 世界 | RoboCupJunior International | RSS |
| 世界 | 国際ルール（Soccer / OnStage / General）の更新・公開版 | RSS (GitHub) |
| 世界 | 国際 Soccer / OnStage / General Rules ページの更新監視 | 更新監視 |

種類の違い

- **RSS** … RSS/Atom を読む。フィードが壊れたら `html_fallback` の設定で HTML から拾い直す
- **HTML** … ページ内のリンクを拾う（新しい PDF 資料の検出に強い）
- **更新監視** … ページの**見える文字**のハッシュを比較し、変わったら「更新あり」＋増えたリンクを通知する

国際ルールは [robocup-junior](https://github.com/robocup-junior) の GitHub で管理されているので、
コミットとリリースを読んで**ルールのどこが変わったか**を直接受け取ります。

## 絞り込みの考え方

タイトル・カテゴリ・URL だけを見て判定します（本文は取得しません）。

1. `forced_leagues` が設定された情報源（国際ルール更新など）は必ず採用
   ※ コミットのタイトルに「サッカー」と書かれないため
2. **サッカー**または**オンステージ**の語に一致 → 採用（レスキューにも触れていても採用）
3. どちらにも一致せず、**レスキュー**等の除外語に一致 → 捨てる
4. 残り（大会日程・参加申込など）は「全般」として採用
   ※ `options.include_general` を `false` にすると捨てます

キーワードや除外語は `sources.json` の `leagues` / `exclude_keywords` で調整できます。

## 動作の調整（`sources.json` の `options`）

| 項目 | 既定値 | 説明 |
| --- | --- | --- |
| `max_age_days` | 210 | これより古い記事は送らない |
| `max_items_per_source` | 8 | 1 情報源あたりの 1 回の上限 |
| `max_items_total` | 40 | 1 回の合計上限 |
| `first_run_items_per_source` | 3 | 初回実行時に送る件数（過去記事の大量投稿を防ぐ） |
| `post_when_empty` | true | 新着 0 件でも「新しいお知らせはありません」と送る |
| `include_general` | true | リーグ不明の一般連絡も送る |
| `total_time_budget` | 600 | 収集全体の制限時間（秒）。超えたら残りの情報源を諦めて、集まった分を送る |

配信時刻は `.github/workflows/daily-news.yml` の `cron: "0 22 * * *"`（= 7:00 JST）を変更してください。
GitHub Actions の cron は UTC 指定で、混雑時は数十分遅れることがあります。

## 情報源が壊れたら

サイトの改装で URL が変わることがあります。その場合も BOT は止まらず、
Discord の最後に **🔧 情報源の状態** として失敗した情報源を知らせます。

対処は `sources.json` の該当 `urls` を新しい URL に直すだけです。
`--dry-run --only <id>` で直ったか確認できます。

- ❌ … 取得できなかった（URL が変わった／サイトが落ちている）
- ⚠️ … 取得はできたが項目が 0 件（ページ構造が変わった可能性）

`sources.json` の `2026aichi_rule.html` のような**年度が入った URL** は、
大会年度が変わると変更が必要です（候補リストに次年度の URL を足しておくと自動で切り替わります）。

## 構成

```
rcj_news/
  main.py      … 実行の入口（引数処理・全体の流れ）
  collect.py   … 情報源ごとの収集（RSS / HTML / 更新監視）
  fetch.py     … HTTP 取得・日本語の文字コード判定
  feeds.py     … RSS / Atom / RDF の解析
  scrape.py    … HTML からリンクと日付を抽出
  classify.py  … サッカー／オンステージの判定
  render.py    … Discord へ送る本文の組み立て
  discord.py   … Webhook 送信（レート制限対応）
  state.py     … 既読の記録
sources.json   … 情報源とキーワードの設定（普段直すのはここだけ）
state/seen.json… 既読の記録（Actions が自動更新・自動コミット）
tests/         … 単体テスト（ネットワーク不要）
```
