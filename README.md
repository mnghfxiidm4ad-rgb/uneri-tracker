# uneri-tracker

楽天証券のお気に入りCSVを取り込み、GitHub Actions で定期実行する株価トラッカーです。

- 上岡式テクニカルシグナル（25日移動平均乖離、RSI(14)、出来高急増）
- 登録日からの騰落率、登録来高値・安値
- iPhone 向け HTML メール（朝・夕・週末）
- GitHub Pages 向けのスマホ対応ダッシュボード

リポジトリ: https://github.com/mnghfxiidm4ad-rgb/uneri-tracker

## ディレクトリ

```
.
├── .github/workflows/stock_tracker.yml
├── data/00ファイル.csv              # 楽天証券のエクスポート（CP932）
├── data/watchlist_tracker.csv       # 追跡マスター
├── data/latest_summary.json
├── docs/index.html                  # ダッシュボード
├── docs/latest_summary.json         # Pages が配信する集計
├── scripts/migrate_rakuten_csv.py
├── scripts/daily_tracker.py
└── requirements.txt
```

同一銘柄が複数グループにある場合、行はグループごとに残します。株価の取得はティッカー単位で1回です。日本株は `6501.T`、米国株は `AAPL` のように変換します。英字入りの東証コード（例: `202A`）は `202A.T` です。

## ローカルセットアップ

```powershell
cd "D:\For_work01\楽天証券登録銘柄情報"
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python scripts\migrate_rakuten_csv.py
python scripts\daily_tracker.py --mode evening
```

ダッシュボードの確認:

```powershell
cd docs
python -m http.server 8000
```

ブラウザで http://127.0.0.1:8000/ を開きます。`file://` で HTML を開くと JSON を読めません。

`migrate_rakuten_csv.py` は楽天CSVとの差分同期です。キーはグループ名と銘柄コードです。両方にある行は登録日、基準株価、高値、安値、ステータス、strategy、source を引き継ぎます。楽天CSVにだけある行は、追加日を当日、基準株価を空、ステータスを「監視中」にして足します。楽天CSVから消えた行は追跡リストから外します。`--archive` または環境変数 `SYNC_ARCHIVE=1` のときは、消えた行を `archived=1` で末尾に残します。出力順は最新の楽天CSVの並びです。楽天CSVから銘柄が1件も読めないときは、追跡リストを書き換えません。基準株価だけ取り直すときは、その行の `base_price` を空にして `daily_tracker.py` を再実行します。

YouTube 由来の棚卸し対象にするときは、`source` または `strategy` に `YouTube`（または `ユーチューブ`）を入れます。登録から 30 日たつと「棚卸し候補（30日経過）」になります。

## GitHub へ送る

このフォルダを https://github.com/mnghfxiidm4ad-rgb/uneri-tracker の `main` に push すると、Actions の画面に `uneri-tracker` が出ます。初回は **Run workflow** で手動実行できます。

`data/00ファイル.csv` を `main` に push すると、`sync-watchlist` が差分同期して追跡CSVを更新します。Actions の **sync-watchlist** を手動実行すると、同じ同期をその場で行えます。手動実行で「消えた銘柄をアーカイブして残す」をオンにすると、削除せず `archived=1` で残します。

## GitHub Secrets

リポジトリの Settings → Secrets and variables → Actions に追加します。

| 名前 | 内容 |
| --- | --- |
| `MAIL_USER` | 送信に使う Gmail アドレス |
| `MAIL_PASS` | Google のアプリパスワード（表示の空白はあってもそのまま登録してよい） |
| `MAIL_TO` | 宛先。複数はカンマ区切り |
| `MAIL_FROM` | 任意。空なら `MAIL_USER` を From にする |
| `PAGES_URL` | 任意。空なら `https://mnghfxiidm4ad-rgb.github.io/uneri-tracker/` |

Gmail は通常のログインパスワードでは SMTP に通りません。Google アカウントの 2 段階認証を有効にし、アプリパスワードを発行して `MAIL_PASS` に入れます。送信は `smtp.gmail.com` の SSL 465 番です。Secrets が空のときはメールだけスキップし、株価データの保存は続けます。

## GitHub Pages

1. リポジトリの Settings → Pages を開く
2. Build and deployment の Source を **GitHub Actions** にする
3. Actions の `uneri-tracker` を手動実行する

ワークフローは `docs/` を Pages にデプロイし、同じ実行で `watchlist_tracker.csv` と `latest_summary.json` を `[skip ci]` 付きでコミットします。トリガーは `schedule` と `workflow_dispatch` だけなので、データ更新の push では再実行しません。

## 実行スケジュール

時刻は日本時間です。GitHub の cron は UTC で、混雑時には遅れることがあります。

| モード | 時刻 | cron (UTC) |
| --- | --- | --- |
| 朝 `morning` | 平日 8:15 | `15 23 * * 0-4` |
| 夕 `evening` | 平日 16:00 | `0 7 * * 1-5` |
| 週末 `weekend` | 土曜 10:00 | `0 1 * * 6` |

- 朝: 前夜の米国株、過熱警戒、直近セッションのギャップダウン（始値が前日終値比 -2% 以下）
- 夕: うねり底値、ブレイク、過熱、登録来の上下
- 週末: 登録後 30 日超の棚卸し、YouTube 棚卸し、週間騰落

ダッシュボードとメールの銘柄名・コードは Yahoo!ファイナンスを開きます。日本株は finance.yahoo.co.jp、米国株・ETFは finance.yahoo.com です。カードの StockChronicle ボタンは https://stockchronicle.app/?code={code} のままです。

## シグナル

| 名前 | 条件 |
| --- | --- |
| 25日乖離 | `(終値 - 25日移動平均) / 25日移動平均 × 100` |
| RSI | 14日、Wilder |
| 出来高倍率 | 当日出来高 ÷ 直前5営業日の平均出来高 |
| うねり底値シグナル | RSI 30 以下 かつ 25日乖離 -5% 以下 |
| 過熱警戒（寄り天・押し目待ち） | 25日乖離 +10% 以上 |
| 出来高急増 | 出来高倍率 2.0 以上 |
| ブレイク | 終値が25日線の上、出来高倍率 1.5 以上、乖離 +10% 未満 |
| ギャップダウン | 始値が前日終値比 -2% 以下 |
| 利確目標到達 | 登録来騰落 +15% 以上 |
| 損切り警戒 | 登録来騰落 -8% 以下 |
| 棚卸し候補（30日経過） | グループが「YouTube・新規」、または source / strategy に YouTube を含み、登録から 30 日以上 |

ステータスは上の表のうち、利確 → 損切り → 底値 → 過熱 → 棚卸し の順で最初に一致したものを入れます。一致しなければ「監視中」です。出来高急増・ブレイク・ギャップダウンは `signals` に併記します。

騰落率は `((現在値 - base_price) / base_price) × 100` です。`base_price` が空の初回は、その回の終値を基準にします。高値・安値は登録日以降の日足から更新します。勝率は、価格取得済みユニーク銘柄のうち騰落率がプラスの割合です。

株価は約3か月の日足を使います。25日線と RSI の計算に必要な本数を確保するためです。取得は 25 銘柄ずつ行い、バッチの間で 1.5 秒、個別の再取得の前に 0.3 秒待ちます。429 を検知したら 30 秒待って残りを打ち切ります。失敗した銘柄はスキップし、前回の価格を残します。

## うまく動かないとき

- メールが来ない: Actions のログに「メール送信をスキップ」または SMTP エラーが出ていないか確認する。アプリパスワードと `MAIL_TO` を見直す。
- ダッシュボードが空: Pages の Source が GitHub Actions になっているか、手動実行が成功しているかを確認する。
- 文字化け: 楽天CSVは CP932 のまま `data/00ファイル.csv` に置く。追跡CSVは UTF-8 BOM で保存する。
- 一部銘柄だけ空欄: 上場廃止や Yahoo 側の欠損です。`fetch_status` が `no_data` の行は次回も再取得します。
- 朝の日本株に当日ギャップがない: 8:15 時点では東証が開いていないため、直近の確定セッションを使います。
