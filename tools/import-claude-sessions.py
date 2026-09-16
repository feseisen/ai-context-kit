#!/usr/bin/env python3
"""
Claude Code のセッションログ (~/.claude/projects/**/*.jsonl) を、
second-brain の raw/sessions/ に「会話だけ」のMarkdownとして取り込む。

方針（AGENTS.md の「rawは原文のまま」を、ノイズ除去と両立させる）:
  - 人間とClaudeの「発言」は一字一句そのまま残す（ここが知識の本体）
  - ツール操作は「何をしたか」の1行だけ残す（コマンドやファイル名。中身は残さない）
  - ツールの出力・スクリーンショット・システム挿入文は捨てる（再現可能なノイズ）
  - 認証情報らしき文字列は自動で伏字にし、実行時に必ず画面へ報告する

使い方:
    python tools/import-claude-sessions.py            # 取り込み実行
    python tools/import-claude-sessions.py --dry-run  # 書き込まずに結果だけ表示
"""

import json
import os
import re
import sys
import glob
import argparse
import datetime

CLAUDE_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(REPO, "raw", "sessions")
MEM_DIR = os.path.join(REPO, "raw", "memory")

# .githooks/pre-commit と同じ検出パターン（伏字にする対象）
SECRET_PATTERNS = [
    ("GitHubトークン", re.compile(r"gh[pousr]_[A-Za-z0-9]{36}")),
    ("Anthropicキー", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("OpenAIキー", re.compile(r"sk-(?:proj-)?[A-Za-z0-9]{32,}")),
    ("AWSアクセスキー", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Google APIキー", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("Slackトークン", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("秘密鍵", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("JWT", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.")),
]

# システムが会話に差し込むノイズ（人間もClaudeも書いていない文字列）
NOISE = [
    re.compile(r"<system-reminder>.*?</system-reminder>", re.S),
    re.compile(r"<local-command-stdout>.*?</local-command-stdout>", re.S),
    re.compile(r"<local-command-caveat>.*?</local-command-caveat>", re.S),
    # スラッシュコマンドの実行記録（/model など）
    re.compile(r"<command-name>.*?</command-(?:name|message|args)>\s*", re.S),
    re.compile(r"<command-(?:message|args)>.*?</command-(?:message|args)>\s*", re.S),
    # 各種コマンドテンプレートの本文（例: <create-pr-command>...）
    re.compile(r"<[a-z-]+-command>.*?</[a-z-]+-command>", re.S),
]

redactions = []  # (セッション, 種別) の記録

# 画像から読み取った情報。画像そのものは保存しないが、そこに写っていた
# 判断材料はここに書き起こして残す（raw/attachments/descriptions.json）。
DESCRIPTIONS = {}


def load_descriptions():
    path = os.path.join(REPO, "raw", "attachments", "descriptions.json")
    if not os.path.exists(path):
        return
    try:
        data = json.load(open(path, encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    for k, v in data.items():
        if not k.startswith("_") and isinstance(v, str):
            DESCRIPTIONS[k] = v


def describe(sid, ts):
    return DESCRIPTIONS.get("{}|{}".format(sid[:8], (ts or "")[:19]))


def redact(text, sid):
    for label, pat in SECRET_PATTERNS:
        def sub(m, _label=label, _sid=sid):
            redactions.append((_sid, _label))
            return "[伏字:" + _label + "]"
        text = pat.sub(sub, text)
    return text


def clean(text):
    for pat in NOISE:
        text = pat.sub("", text)
    return text.strip()


def tool_line(block):
    """tool_use ブロックを『何をしたか』の1行にする。中身（コード等）は残さない。"""
    name = block.get("name", "?")
    inp = block.get("input", {}) or {}
    for key in ("description", "file_path", "path", "pattern", "command",
                "query", "url", "prompt"):
        val = inp.get(key)
        if isinstance(val, str) and val.strip():
            arg = " ".join(val.split())
            if len(arg) > 110:
                arg = arg[:110] + "…"
            return "`" + name + "` — " + arg
    return "`" + name + "`"


def load_session(path):
    """1セッションのJSONLを読み、メタ情報と会話の流れを返す。"""
    meta = {"title": None, "cwd": None, "model": None, "version": None,
            "start": None, "end": None, "n_user": 0, "n_asst": 0}
    events = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue

        t = d.get("type")
        ts = d.get("timestamp")
        if ts:
            meta["start"] = meta["start"] or ts
            meta["end"] = ts
        meta["cwd"] = d.get("cwd") or meta["cwd"]
        meta["version"] = d.get("version") or meta["version"]

        # セッションのタイトル（人が付けた名前を優先）
        if t == "custom-title" and d.get("content"):
            meta["title"] = d["content"]
        elif t == "ai-title" and not meta["title"] and d.get("content"):
            meta["title"] = d["content"]

        if t not in ("user", "assistant"):
            continue

        # isMeta はシステムが会話に注入した内容（スキルの指示文など）の印。
        # 本人が書いた発言ではないので取り込まない。実測では、これだけで
        # 全体の約半分（250KB）を占めるノイズだった。
        if d.get("isMeta"):
            continue

        msg = d.get("message") or {}
        if not isinstance(msg, dict):
            continue
        meta["model"] = msg.get("model") or meta["model"]
        content = msg.get("content")

        if t == "user":
            # 文字列 = 本人の発言。リスト = ほぼツール結果（＝捨てる）
            if isinstance(content, str):
                body = clean(content)
                if body:
                    meta["n_user"] += 1
                    events.append(("user", ts, body))
            elif isinstance(content, list):
                texts, n_img = [], 0
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text":
                        texts.append(b.get("text", ""))
                    elif b.get("type") == "image":
                        n_img += 1
                body = clean("\n".join(texts))
                # 画像そのものは取り込まないが、「ここに画像があった」ことは残す。
                # 完全に消すと会話の文脈が飛んで読めなくなるため。
                # 画像の中身は、Claude が読み取ってテキスト化したものが
                # 後続の応答に残っている（AGENTS.md の添付ファイルの扱いを参照）。
                if n_img:
                    mark = "> 🖼 画像を添付（{}枚）".format(n_img)
                    desc = describe(os.path.splitext(os.path.basename(path))[0], ts)
                    if desc:
                        mark += "\n>\n> " + desc.replace("\n", "\n> ")
                    body = (body + "\n\n" + mark).strip() if body else mark
                if body:
                    meta["n_user"] += 1
                    events.append(("user", ts, body))
        else:
            said, did = [], []
            for b in (content if isinstance(content, list) else []):
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    said.append(b.get("text", ""))
                elif b.get("type") == "tool_use":
                    did.append(tool_line(b))
            body = clean("\n".join(said))
            if body or did:
                if body:
                    meta["n_asst"] += 1
                events.append(("assistant", ts, body, did))
    return meta, events


def to_local(ts):
    """ログのタイムスタンプはUTC。日本時間など端末のローカル時刻に直す。"""
    if not ts:
        return None
    try:
        dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone()


def fmt_ts(ts):
    dt = to_local(ts)
    return dt.strftime("%Y-%m-%d %H:%M") if dt else (ts or "")[:16]


def day_of(ts):
    dt = to_local(ts)
    return dt.strftime("%Y-%m-%d") if dt else "0000-00-00"


def stale_copies(sid8, keep_path):
    """同じ会話の、別プロジェクト名で書かれた古いファイルを探す。

    Claude Code はセッションの作業フォルダが変わると、そのセッションの JSONL を
    別のプロジェクトディレクトリへ移す。ここではファイル名にプロジェクト名を
    含めているため、移動のたびに別名のファイルが増え、**同じ会話の断片が複数
    残ってしまう**（2026-08-30 に発覚。1つの会話が3ファイルに分裂していた）。

    session_id が同じなら同じ会話なので、新しく書いたものだけを残す。
    ただし新しい方が短い場合は消さない（取りこぼしを防ぐため）。
    """
    found = []
    for path in glob.glob(os.path.join(OUT_DIR, "*_{}.md".format(sid8))):
        if os.path.abspath(path) == os.path.abspath(keep_path):
            continue
        base = os.path.splitext(os.path.basename(path))[0]
        parts = base.split("_", 1)
        name = parts[1].rsplit("_", 1)[0] if len(parts) > 1 else "?"
        found.append((path, name))
    return sorted(found)


def render(meta, events, sid, project, src_path, prev_names=None):
    day = day_of(meta["start"])
    title = meta["title"] or (project + " のセッション")
    L = []
    L.append("---")
    L.append("title: " + title)
    L.append("area: raw")
    L.append("source: claude-code")
    L.append("session_id: " + sid)
    L.append("project: " + project)
    if prev_names:
        L.append("previous_names: " + ", ".join(prev_names))
    L.append("cwd: " + (meta["cwd"] or ""))
    L.append("model: " + (meta["model"] or ""))
    L.append("messages: user {} / assistant {}".format(meta["n_user"], meta["n_asst"]))
    L.append("created: " + day)
    L.append("imported: " + datetime.date.today().isoformat())
    L.append("tags: [session-log, claude-code]")
    L.append("---")
    L.append("")
    L.append("# " + title)
    L.append("")
    L.append("> Claude Code のセッションログ（`" + os.path.basename(src_path)
             + "`）から**会話だけ**を抽出したもの。")
    L.append("> 発言は原文のまま。ツール操作は「何をしたか」の1行に畳み、ツールの出力・")
    L.append("> スクリーンショット・システム挿入文は取り込んでいない（再現可能なノイズのため）。")
    L.append("> 期間: " + fmt_ts(meta["start"]) + " 〜 " + fmt_ts(meta["end"]))
    L.append("")

    for ev in events:
        if ev[0] == "user":
            L.append("## 👤 " + fmt_ts(ev[1]))
            L.append("")
            L.append(ev[2])
            L.append("")
        else:
            _, ts, body, did = ev
            L.append("### 🤖 Claude")
            L.append("")
            if body:
                L.append(body)
                L.append("")
            if did:
                L.append("<details><summary>この間に実行した操作</summary>")
                L.append("")
                for d in did:
                    L.append("- " + d)
                L.append("")
                L.append("</details>")
                L.append("")
    return "\n".join(L).rstrip() + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="書き込まずに結果だけ表示")
    args = ap.parse_args()

    load_descriptions()
    files = sorted(glob.glob(os.path.join(CLAUDE_DIR, "**", "*.jsonl"), recursive=True))
    if not files:
        print("no session files found under " + CLAUDE_DIR)
        return 1

    if not args.dry_run:
        os.makedirs(OUT_DIR, exist_ok=True)
    print("found {} session file(s)\n".format(len(files)))
    total_in = total_out = 0

    for path in files:
        sid = os.path.splitext(os.path.basename(path))[0]
        meta, events = load_session(path)
        project = os.path.basename((meta["cwd"] or "").rstrip("\\/")) or \
            os.path.basename(os.path.dirname(path))
        if not events:
            print("  skip (no conversation): {}/{}".format(project, sid[:8]))
            continue

        day = day_of(meta["start"])
        out = os.path.join(OUT_DIR, "{}_{}_{}.md".format(day, project, sid[:8]))
        stale = stale_copies(sid[:8], out)
        md = redact(render(meta, events, sid, project, path,
                           [n for _, n in stale]), sid[:8])

        size_in, size_out = os.path.getsize(path), len(md.encode())
        total_in += size_in
        total_out += size_out
        pct = size_out * 100.0 / size_in if size_in else 0
        print("  {}/{}  {:8.0f}KB -> {:6.0f}KB ({:4.1f}%)  user {} / asst {}".format(
            project, sid[:8], size_in / 1024.0, size_out / 1024.0, pct,
            meta["n_user"], meta["n_asst"]))

        if not args.dry_run:
            with open(out, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(md)

        # 同じ会話が別名で残っていたら畳む（新しい方が短ければ消さない）
        for old_path, old_name in stale:
            old_size = os.path.getsize(old_path)
            if len(md.encode()) >= old_size:
                print("    畳んだ: {} -> {}".format(
                    os.path.basename(old_path), os.path.basename(out)))
                if not args.dry_run:
                    os.remove(old_path)
            else:
                print("    !! 残した（新しい方が短い）: {} ({}KB) vs {} ({}KB)".format(
                    os.path.basename(old_path), old_size // 1024,
                    os.path.basename(out), len(md.encode()) // 1024))

    # Claude Code のメモリファイルも取り込む（小さく、価値が高い）
    mem = sorted(glob.glob(os.path.join(CLAUDE_DIR, "*", "memory", "*.md")))
    if mem:
        print("\nmemory files: {}".format(len(mem)))
        for m in mem:
            proj = os.path.basename(os.path.dirname(os.path.dirname(m)))
            dest_dir = os.path.join(MEM_DIR, proj)
            print("  {}/{}".format(proj, os.path.basename(m)))
            if not args.dry_run:
                os.makedirs(dest_dir, exist_ok=True)
                body = redact(open(m, encoding="utf-8").read(), "memory")
                with open(os.path.join(dest_dir, os.path.basename(m)), "w",
                          encoding="utf-8", newline="\n") as fh:
                    fh.write(body)

    ratio = total_out * 100.0 / total_in if total_in else 0
    print("\ntotal: {:.2f}MB -> {:.2f}MB ({:.1f}%)".format(
        total_in / 1024.0 / 1024, total_out / 1024.0 / 1024, ratio))

    if redactions:
        print("\n!! REDACTED (要確認) !!")
        seen = {}
        for sid, label in redactions:
            seen[(sid, label)] = seen.get((sid, label), 0) + 1
        for key in sorted(seen):
            print("  session {}: {} x{}".format(key[0], key[1], seen[key]))
        print("  -> 実物だった場合は、発行元での失効・再発行が必要です。")
    else:
        print("\nno secrets detected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
