# 双六小屋チェッカー GitHub Actions 版 v7

PCを切っても、GitHub Actions上で双六小屋の予約状況を確認し、LINEへ通知します。

## 動く内容

- `sugoroku_10min_watch.yml`
  - 10分ごとに `2026-08-19 / 双六小屋 / 一般室` を確認
  - `○` または数字ならLINE通知
  - `満` のときは通知なし

- `sugoroku_month_12h.yml`
  - 12時間ごとに `2026年8月 / 双六小屋 / 一般室` の一覧をLINE通知

## GitHubで使う手順

1. GitHubで新しいリポジトリを作る
2. このフォルダ内のファイルを全部アップロードする
   - `.github/workflows/` フォルダも必ず含める
3. GitHubのリポジトリで `Settings` → `Secrets and variables` → `Actions`
4. `New repository secret` で以下2つを登録する
   - `LINE_CHANNEL_ACCESS_TOKEN`
   - `LINE_USER_ID`
5. `Actions` タブを開く
6. `Sugoroku 10min Watch` を選び、`Run workflow` で手動テスト
7. LINEに通知が来るか確認
8. 問題なければ放置でOK

## 絶対に注意

- 自分の本物の `line_config.json` はGitHubへアップロードしないでください。
- LINEのトークンやUSER IDは、GitHub Secretsに入れてください。
- `.gitignore` に `line_config.json` は入れてありますが、手動アップロード時は自分でも確認してください。

## 予約判定

- `○` → 予約できる可能性あり
- 数字 → 予約できる可能性あり
- `満` → 満室
- `/` → 不可
- `℡` → 電話確認

## 注意

GitHub Actionsのスケジュール実行は、必ず秒単位で正確に動くものではありません。混雑時は数分以上遅れることがあります。
