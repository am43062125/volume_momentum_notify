# 売買代金モメンタム検証

売買代金の継続的な増加が、その後の株価パフォーマンスの先行指標になるかを検証するイベントスタディ用ツールです。

本ツールは売買ルール、損切、利確、ポジション管理、資金管理の検証は行いません。資金流入イベントを検出し、その後のリターン、リスク指標、ベンチマーク超過リターンを集計します。

## セットアップ

Python 3.11 以降を使用します。

```powershell
python -m pip install -e .
```

Codex 同梱 Python を使う場合は、環境に合わせて Python 実行ファイルを指定してください。

## 設定ファイル

設定は [config.example.json](C:\WORK\05_投資\volumetest\config.example.json) で管理します。

主な設定項目:

| 区分 | 項目 |
| --- | --- |
| backtest | バックテスト年数、評価期間、再登録禁止期間 |
| data | SQLite DB、yfinanceキャッシュ、JPX上場銘柄一覧URL、ベンチマーク |
| universe | 現在時価総額フィルタの有効・無効、時価総額下限 |
| signal | 売買代金平均日数、資金流入比率、継続日数、最低売買代金、移動平均 |
| report | レポート出力先、言語 |
| daily | 日次通知の有効・無効、監視対象営業日数、日次レポート出力先、差分更新日数 |
| email | Gmail SMTP設定、参照する環境変数名 |

Markdown レポートは必ず日本語で出力します。

## 検証対象ユニバース

本ツールは、特定の銘柄をユーザーが指定して検証するものではありません。

検証対象は、JPX公式の上場銘柄一覧から取得した日本株ユニバースです。設定ファイルの `data.jpx_listed_issues_url` で指定されたJPX公式Excelを読み込み、Yahoo Finance形式のティッカー、例: `7203.T`、に変換して使用します。

ETF、ETN、REIT、インフラファンドなどは除外し、内国株式などの株式銘柄を対象にします。`--limit` は動作確認用の上限であり、本番の銘柄選択条件ではありません。

本フェーズでは、JPX公式一覧に含まれる現在上場中の銘柄を対象にします。過去5年間に上場廃止した銘柄は対象外となるため、サバイバーシップバイアスが含まれる可能性があります。

## データ取得

SQLite DB を初期化し、JPX公式一覧から全上場株式ユニバースを取得して登録します。

```powershell
python -m volume_momentum.cli --config config.example.json fetch-data
```

実際に yfinance から価格データと現在時価総額を取得する場合:

```powershell
python -m volume_momentum.cli --config config.example.json fetch-data --market-caps --download
```

動作確認だけ小さく行う場合:

```powershell
python -m volume_momentum.cli --config config.example.json fetch-data --limit 5 --market-caps --download
```

本番検証では `--limit` を付けずに実行します。

一度取得した価格データは SQLite に保存され、以後の分析で再利用されます。

`fetch-data --download` を再実行した場合、保存済み価格データがある銘柄は既定でスキップします。明示的に取り直す場合のみ `--refresh` を指定します。

```powershell
python -m volume_momentum.cli --config config.example.json fetch-data --download --refresh
```

## 現在時価総額フィルタ

現在時価総額フィルタは、低位株や極端な小型株を除外するためのユニバースフィルタです。

これは現在時点の情報を使う近似フィルタであり、過去イベント日時点の厳密な時価総額条件ではありません。使用有無は設定ファイルで切り替えられます。

```json
"universe": {
  "enable_current_market_cap_filter": true,
  "min_current_market_cap": 30000000000
}
```

フィルタを有効にする場合は、事前に `fetch-data --market-caps` で現在時価総額を取得してください。

## シグナル条件

売買代金増加に加えて、急落時の出来高増を除外するために終値上昇条件を使用します。既定では、イベント日の調整後終値が前営業日および5営業日前の調整後終値を上回る必要があります。

```json
"signal": {
  "require_close_above_previous": true,
  "close_lookback_days": 5
}
```

## バックテスト

保存済みデータからイベント検出と評価集計を実行します。

```powershell
python -m volume_momentum.cli --config config.example.json backtest
```

分析対象期間は、保存済み価格データの最新日を基準に `backtest.period_years` で絞り込みます。移動平均や売買代金平均の計算に必要なウォームアップ期間は、分析開始日より前から追加で読み込みます。

CSV と日本語 Markdown レポートも出力する場合:

```powershell
python -m volume_momentum.cli --config config.example.json backtest --write-reports
```

保存済みデータからレポートを生成する場合:

```powershell
python -m volume_momentum.cli --config config.example.json report
```

## 日次イベント通知

日次通知は、JPX公式一覧から取得した全上場株式ユニバースを対象に、最新取引日の新規ヒットと過去63営業日以内の監視中銘柄を抽出します。

日次レポートだけを出力するドライラン:

```powershell
python -m volume_momentum.cli --config config.example.json daily-notify --dry-run
```

価格データも差分更新してから日次レポートを出力する場合:

```powershell
python -m volume_momentum.cli --config config.example.json daily-notify --download --market-caps --dry-run
```

小規模に動作確認する場合:

```powershell
python -m volume_momentum.cli --config config.example.json daily-notify --download --market-caps --limit 5 --dry-run
```

Gmailでメール送信する場合:

```powershell
python -m volume_momentum.cli --config config.example.json daily-notify --download --market-caps --send-email
```

通知対象が0件でも疎通確認メールを送る場合:

```powershell
python -m volume_momentum.cli --config config.example.json daily-notify --send-email --send-empty-email
```

`--dry-run` を付けた場合、メール送信と通知履歴登録は行いません。
`--send-email` を付けて実際にメール送信した場合のみ、通知履歴へ登録します。

初回実行時は、通知履歴が存在しないため、最新取引日を基準として過去63営業日以内にヒットしたイベントも通知対象に含めます。
2回目以降は、最新取引日に新しくヒットした銘柄を新規ヒットとして扱い、過去63営業日以内の既存ヒットを監視中として通知します。

日次レポートは既定で以下に出力します。

```text
reports/daily/YYYY-MM-DD/
```

| ファイル | 内容 |
| --- | --- |
| events.csv | 日次通知対象の銘柄一覧 |
| report.md | 人間が確認するための日本語Markdown日次レポート |
| run.log | 実行日時、対象取引日、件数などの簡易ログ |

## Gmail設定

Gmail送信では、通常のGoogleアカウントパスワードではなく Gmail のアプリパスワードを使用します。
秘密情報は `config.example.json` に直接書かず、環境変数または `.env` に設定します。

ローカル `.env` の例:

```text
GMAIL_SMTP_USER=your-account@gmail.com
GMAIL_APP_PASSWORD=your-app-password
MAIL_FROM=
MAIL_TO=
MAIL_CC=
MAIL_BCC=
```

`MAIL_FROM` と `MAIL_TO` は省略可能です。
未指定の場合は、どちらも `GMAIL_SMTP_USER` と同じメールアドレスを使用します。

## GitHub Actions運用

日次通知用 workflow は [.github/workflows/daily-notify.yml](C:\WORK\05_投資\volumetest\.github\workflows\daily-notify.yml) です。

定期実行は平日20:00 JST相当です。
GitHub Actions の cron は UTC 指定のため、workflow では `0 11 * * 1-5` を使用します。

GitHub リポジトリの Secrets に以下を登録してください。

| Secret | 内容 |
| --- | --- |
| GMAIL_SMTP_USER | Gmail SMTPユーザー |
| GMAIL_APP_PASSWORD | Gmail アプリパスワード |
| MAIL_FROM | 送信元メールアドレス。未指定時は `GMAIL_SMTP_USER` |
| MAIL_TO | 送信先メールアドレス。未指定時は `GMAIL_SMTP_USER` |
| MAIL_CC | CC。不要なら空で可 |
| MAIL_BCC | BCC。不要なら空で可 |

GitHub Actions の実行環境は毎回初期化されるため、workflow では `data/volume_momentum.sqlite3` と `data/yfinance_cache` を `actions/cache` で復元・保存します。
キャッシュが復元できた場合は日次差分更新になり、復元できなかった場合は初回取得に近い動作になります。

手動実行では `limit` を指定して小規模に確認できます。
手動実行の `send_email` を false にした場合、メール送信せず日次レポートだけを artifact として保存します。
通知対象が0件でもメール疎通確認をしたい場合は、`send_email=true` と `send_empty_email=true` を指定します。

## 出力ファイル

既定では `reports` フォルダに出力します。

| ファイル | 内容 |
| --- | --- |
| events.csv | 検出された資金流入イベント |
| evaluations.csv | イベントごとの評価期間別リターン、MFE、MAE、ドローダウン |
| summary.csv | 評価期間別の集計統計 |
| report.md | 人間が確認するための日本語Markdownレポート |

## ベンチマーク

日経平均は `^N225` を使用します。

TOPIX は Yahoo Finance で指数ティッカーを安定取得できなかったため、既定では `1306.T` を TOPIX 連動ETFによる代替ベンチマークとして使用します。

## よくある失敗

| 症状 | 対応 |
| --- | --- |
| 現在時価総額フィルタで対象銘柄が0件になる | `fetch-data --market-caps` を実行するか、設定ファイルでフィルタを無効にします |
| 価格データがありませんと表示される | `fetch-data --download` を実行してください |
| yfinance の内部DBエラーが出る | `data.yfinance_cache_dir` が書き込み可能な場所か確認してください |
| TOPIX指数が取得できない | 既定の `1306.T` を代替ベンチマークとして使用してください |
| 日次通知で同じ銘柄が新規ヒットとして再通知される | SQLite DB の `notification_history` が復元されているか確認してください |
| GitHub Actionsで毎回初回取得のように遅い | `actions/cache` が `data/volume_momentum.sqlite3` と `data/yfinance_cache` を復元できているか確認してください |
| Gmail送信に失敗する | GmailアプリパスワードとSecrets名が `config.example.json` の `email` 設定と一致しているか確認してください |

## テスト

```powershell
python -m unittest discover -s tests
```
