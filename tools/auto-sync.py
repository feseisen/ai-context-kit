#!/usr/bin/env python3
"""
Claude Code のフックから呼ばれ、会話ログを取り込んで自動でコミット・プッシュする。

タイミングは「一つの対応の流れが一旦途切れたとき」（Stop フック）と
「セッションが終わったとき」（SessionEnd フック）。どちらも ~/.claude/settings.json
に登録する。すべてのプロジェクトのセッションが対象になる。

設計上の約束:
  - **raw/ 配下しかコミットしない。** 作業中の他の変更を巻き込まない
  - 変更がなければ何もしない（空コミットを作らない）
  - 失敗してもセッションを止めない（常に exit 0）
  - 二重起動しない（ロックファイル）

手動実行:
    python tools/auto-sync.py          # 通常
    python tools/auto-sync.py --verbose # 何をしたか表示
"""

import json
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMPORTER = os.path.join(REPO, "tools", "import-claude-sessions.py")
LOCK = os.path.join(REPO, ".git", "auto-sync.lock")
LOCK_STALE_SEC = 300
TRACKED = ["raw/sessions", "raw/memory"]

VERBOSE = "--verbose" in sys.argv


def log(msg):
    if VERBOSE:
        print(msg)


def git(*args, check=False):
    r = subprocess.run(["git", "-C", REPO, *args],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip())
    return r


def acquire_lock():
    """二重起動を防ぐ。古いロックは無視して奪う。"""
    try:
        if os.path.exists(LOCK):
            if time.time() - os.path.getmtime(LOCK) < LOCK_STALE_SEC:
                return False
            os.remove(LOCK)
        with open(LOCK, "w") as fh:
            fh.write(str(os.getpid()))
        return True
    except OSError:
        return False


def release_lock():
    try:
        os.remove(LOCK)
    except OSError:
        pass


def summarize(changed):
    """変更されたファイル名から、コミットメッセージ用の要約を作る。"""
    projects, memory = [], 0
    for path in changed:
        base = os.path.basename(path)
        if "/memory/" in path.replace("\\", "/"):
            memory += 1
        elif base.count("_") >= 2:
            # YYYY-MM-DD_プロジェクト名_セッションID.md
            proj = base.split("_", 1)[1].rsplit("_", 1)[0]
            if proj not in projects:
                projects.append(proj)
    parts = []
    if projects:
        parts.append(" / ".join(projects[:3]))
        if len(projects) > 3:
            parts.append("ほか{}件".format(len(projects) - 3))
    if memory:
        parts.append("メモリ{}件".format(memory))
    return "、".join(parts) or "会話ログ"


def main():
    if not os.path.isdir(os.path.join(REPO, ".git")):
        log("second-brain がGitリポジトリではないため中止")
        return 0
    if not acquire_lock():
        log("別の取り込みが実行中のためスキップ")
        return 0

    try:
        # 1. 会話ログを取り込む
        r = subprocess.run([sys.executable, IMPORTER],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", cwd=REPO)
        if r.returncode != 0:
            log("取り込みに失敗:\n" + (r.stderr or r.stdout))
            return 0
        log(r.stdout)

        redacted = "REDACTED" in (r.stdout or "")

        # 2. raw/ 配下だけをステージする（作業中の他の変更は触らない）
        git("add", "--", *TRACKED)
        staged = git("diff", "--cached", "--name-only", "--", *TRACKED)
        changed = [p for p in staged.stdout.splitlines() if p.strip()]
        if not changed:
            log("変更なし")
            return 0

        # 3. コミット
        summary = summarize(changed)
        body = ["会話ログを自動更新: " + summary, ""]
        body.append("Claude Code のフックによる自動コミット（{}ファイル）。".format(len(changed)))
        body.append("発言は原文のまま、ツール出力とコードの本文は除外している。")
        if redacted:
            body.append("")
            body.append("※ 認証情報らしき文字列を伏字にした。実物かどうか要確認。")
        body.append("")
        body.append("Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>")

        c = git("commit", "-m", "\n".join(body), "--", *TRACKED)
        if c.returncode != 0:
            log("コミット失敗: " + (c.stderr or c.stdout))
            return 0
        log("コミット: " + summary)

        # 4. プッシュ（失敗しても次回リトライされるので握りつぶす）
        p = git("push", "origin", "HEAD")
        log("プッシュ " + ("成功" if p.returncode == 0 else "失敗（次回再試行）"))

        # 伏字が出たときだけ、Claude Code の画面に通知する
        if redacted:
            print(json.dumps({
                "systemMessage": "会話ログの取り込み中に認証情報らしき文字列を伏字にしました。"
                                 "実物だった場合は発行元での失効・再発行が必要です。",
                "suppressOutput": True,
            }, ensure_ascii=False))
    except Exception as e:  # フックがセッションを壊さないよう、必ず握りつぶす
        log("例外: {}".format(e))
    finally:
        release_lock()
    return 0


if __name__ == "__main__":
    sys.exit(main())
