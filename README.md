# wl-sns-autopost

wardrobe-lab.jp の記事を、毎日1本 Threads に自動投稿する（GitHub Actions・無料枠内）。

- 実行: 毎日 12:07 JST（`.github/workflows/threads.yml`）
- 記事の選び方: RSS に未投稿の新着があればそれ、なければ過去記事を1本
- 文面: タイトル＋リード1文＋URL＋文字タグ。トピックタグを記事カテゴリから1つ自動付与
- 投稿済みの記録: `state.json`（ワークフローが自動コミット）
- 延長したトークン: `token.enc`（暗号化済み。鍵は Secrets のトークン）

## 初回設定（1回だけ）

1. Threads の長期アクセストークンとユーザーIDを取得する（手順は Mac の `~/Documents/wl-threads/setup.md` の「1.」）
2. このリポジトリの **Settings → Secrets and variables → Actions → New repository secret** で2つ登録
   - `THREADS_USER_ID` … ユーザーID（数字）
   - `THREADS_ACCESS_TOKEN` … 長期アクセストークン
3. **Actions → Threads auto post → Run workflow**（dry_run にチェック）で文面を確認
4. 問題なければ dry_run を外して1回実行 → 以後は毎日自動

Secrets が未登録の間は、毎日の実行は何もせずに正常終了する。

## よくある操作

- 時刻を変える: `threads.yml` の `cron`（UTC表記。JST−9時間）
- 止める: Actions → Threads auto post → 「…」→ Disable workflow
- 特定の記事をもう一度流す: `state.json` の `posted` からそのハンドルの行を消す
- トークンを取り直したとき: `THREADS_ACCESS_TOKEN` を上書きし、`token.enc` を削除
