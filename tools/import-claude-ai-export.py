#!/usr/bin/env python3
"""
Claude.ai / Cowork のデータエクスポートを raw/ に取り込む。

Claude Code のログと違い、これは自動では取れない。claude.ai の
「設定 → プライバシー → データをエクスポート」で申請すると、
ダウンロードURLがメールで届く（各URLは1回しか使えない）。
zipを展開したフォルダを指定して、手動で実行する。

    python tools/import-claude-ai-export.py <展開したフォルダ>
    python tools/import-claude-ai-export.py <フォルダ> --dry-run

方針は tools/import-claude-sessions.py と同じ:
  - 人間とClaudeの「発言」は原文のまま残す
  - ツール操作は「何をしたか」の1行だけ（中身は残さない）
  - ツールの出力・Claudeの思考ログ（thinking）は取り込まない
    ※ thinking はClaudeの思考であって本人の思考ではないため。
      実測で全体の27%を占めるが、クローンAIの「能力」には寄与しない
  - 認証情報らしき文字列は自動で伏字にし、実行時に必ず報告する
"""

import argparse
import datetime
import glob
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(REPO, "raw", "sessions")
MEM_DIR = os.path.join(REPO, "raw", "memory", "claude-ai")

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

redactions = []


def redact(text, tag):
    for label, pat in SECRET_PATTERNS:
        def sub(m, _l=label, _t=tag):
            redactions.append((_t, _l))
            return "[伏字:" + _l + "]"
        text = pat.sub(sub, text)
    return text


def to_local(ts):
    if not ts:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone()


def fmt_ts(ts):
    dt = to_local(ts)
    return dt.strftime("%Y-%m-%d %H:%M") if dt else str(ts or "")[:16]


def day_of(ts):
    dt = to_local(ts)
    return dt.strftime("%Y-%m-%d") if dt else "0000-00-00"


def tool_line(block):
    """tool_use を『何をしたか』の1行にする。入力の中身は残さない。"""
    name = block.get("name") or block.get("tool_identifier") or "?"
    msg = block.get("message")
    if isinstance(msg, str) and msg.strip():
        arg = " ".join(msg.split())
        return "`{}` — {}".format(name, arg[:110] + ("…" if len(arg) > 110 else ""))
    inp = block.get("input")
    if isinstance(inp, dict):
        for key in ("title", "command", "query", "id", "path", "file_name", "prompt"):
            v = inp.get(key)
            if isinstance(v, str) and v.strip():
                arg = " ".join(v.split())
                return "`{}` — {}".format(name, arg[:110] + ("…" if len(arg) > 110 else ""))
    return "`" + name + "`"


def render(conv):
    msgs = conv.get("chat_messages") or []
    human = [m for m in msgs if m.get("sender") == "human"]
    asst = [m for m in msgs if m.get("sender") == "assistant"]
    uid = (conv.get("uuid") or "")[:8]
    title = (conv.get("name") or "").strip() or "(タイトルなし)"

    L = ["---",
         "title: " + title,
         "area: raw",
         "source: claude-ai",
         "session_id: " + (conv.get("uuid") or ""),
         "project: claude-ai",
         "messages: user {} / assistant {}".format(len(human), len(asst)),
         "created: " + day_of(conv.get("created_at")),
         "imported: " + datetime.date.today().isoformat(),
         "tags: [session-log, claude-ai]",
         "---",
         "",
         "# " + title,
         "",
         "> Claude.ai のデータエクスポートから**会話だけ**を抽出したもの。",
         "> 発言は原文のまま。ツール操作は「何をしたか」の1行に畳み、ツールの出力・",
         "> Claudeの思考ログ（thinking）・添付ファイルの中身は取り込んでいない。",
         "> 期間: {} 〜 {}".format(fmt_ts(conv.get("created_at")), fmt_ts(conv.get("updated_at"))),
         ""]

    summary = " ".join((conv.get("summary") or "").split())
    if summary:
        L += ["<details><summary>Claude.ai が付けた要約</summary>", "", summary, "", "</details>", ""]

    for m in msgs:
        blocks = m.get("content") or []
        said, did = [], []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                said.append(b.get("text") or "")
            elif t == "tool_use":
                did.append(tool_line(b))
            # thinking / tool_result は取り込まない
        body = "\n".join(said).strip() or (m.get("text") or "").strip()

        files = [f.get("file_name") for f in (m.get("files") or []) + (m.get("attachments") or [])
                 if isinstance(f, dict) and f.get("file_name")]

        if m.get("sender") == "human":
            L += ["## 👤 " + fmt_ts(m.get("created_at")), ""]
            if body:
                L += [body, ""]
            if files:
                L += ["> 添付: " + ", ".join(files), ""]
        else:
            L += ["### 🤖 Claude", ""]
            if body:
                L += [body, ""]
            if did:
                L += ["<details><summary>この間に実行した操作</summary>", ""]
                L += ["- " + x for x in did]
                L += ["", "</details>", ""]
    return "\n".join(L).rstrip() + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("export_dir", help="zipを展開したフォルダ（conversations.json がある場所）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--exclude", action="append", default=[],
                    help="タイトルにこの文字列を含む会話を除外（複数指定可）")
    args = ap.parse_args()

    conv_path = os.path.join(args.export_dir, "conversations.json")
    if not os.path.exists(conv_path):
        print("conversations.json が見つかりません: " + conv_path)
        return 1

    convs = json.load(open(conv_path, encoding="utf-8"))
    if not args.dry_run:
        os.makedirs(OUT_DIR, exist_ok=True)
    print("会話 {} 件\n".format(len(convs)))

    total_out = 0
    for conv in sorted(convs, key=lambda c: c.get("created_at") or ""):
        title = (conv.get("name") or "").strip()
        if any(x in title for x in args.exclude):
            print("  除外: {}".format(title))
            continue
        uid = (conv.get("uuid") or "")[:8]
        md = redact(render(conv), uid)
        out = os.path.join(OUT_DIR, "{}_claude-ai_{}.md".format(day_of(conv.get("created_at")), uid))
        total_out += len(md.encode())
        n = len(conv.get("chat_messages") or [])
        print("  {:6.0f}KB  {:3}往復  {}".format(len(md.encode()) / 1024.0, n // 2, title))
        if not args.dry_run:
            with open(out, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(md)

    # Claude.ai の記憶
    mem_files = glob.glob(os.path.join(args.export_dir, "memories", "*.json"))
    written = 0
    for f in mem_files:
        j = json.load(open(f, encoding="utf-8"))
        for mf in j.get("memory_files", []):
            if not isinstance(mf, dict):
                continue
            path = mf.get("path") or ""
            name = re.sub(r"[^A-Za-z0-9_.-]", "_", path.strip("/")) or "memory"
            if not name.endswith(".md"):
                name += ".md"
            body = ["---",
                    "title: Claude.ai の記憶 " + path,
                    "area: raw",
                    "source: claude-ai-memory",
                    "original_path: " + path,
                    "updated: " + (mf.get("updated_at") or ""),
                    "imported: " + datetime.date.today().isoformat(),
                    "tags: [memory, claude-ai]",
                    "---",
                    "",
                    "# Claude.ai の記憶: `" + path + "`",
                    "",
                    "> claude.ai が会話から自動生成した記憶。原文のまま。",
                    "",
                    "```",
                    (mf.get("content") or "").strip(),
                    "```"]
            text = redact("\n".join(body) + "\n", "memory")
            written += 1
            print("  記憶: {}".format(path))
            if not args.dry_run:
                os.makedirs(MEM_DIR, exist_ok=True)
                with open(os.path.join(MEM_DIR, name), "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(text)

    print("\n合計: 会話 {:.2f}MB / 記憶 {} 件".format(total_out / 1024.0 / 1024, written))
    if redactions:
        print("\n!! REDACTED (要確認) !!")
        seen = {}
        for tag, label in redactions:
            seen[(tag, label)] = seen.get((tag, label), 0) + 1
        for k in sorted(seen):
            print("  {}: {} x{}".format(k[0], k[1], seen[k]))
    else:
        print("no secrets detected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
