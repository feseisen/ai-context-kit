#!/usr/bin/env python3
"""
受け取ったキットを、その場で使える状態にする（下書き）。

ZIP を展開したフォルダで1回走らせると、README の手順1〜5のうち、手でやると
間違えやすいところをまとめて済ませる。何度走らせても同じ結果になる。

やること:
  1. フォルダの骨組みをそろえる（raw/ と areas/ の各フォルダ）      キットの中
  2. git の準備（git init と、秘密情報を止めるフックの有効化）       キットの中
  3. Claude Code の共通の指示（CLAUDE.md）に、このキットへの入口を書く  キットの外
  4. Claude Code の設定（settings.json）に、会話を取り込むフックを足す  キットの外

キットの外（3と4）は、書く前に必ず
  - 何がどう変わるかを見せ、
  - y/N で聞き（Enter だけなら書かない）、
  - 元のファイルを同じ場所に写してから書く。
既にある設定は消さない。足すだけ（動かなくなった古い登録だけは外す。それも見せてから）。

使い方:
    python tools/setup.py            # 実行
    python tools/setup.py --dry-run  # 何も書かずに、やることだけ見せる

ネットワークには出ない。標準ライブラリだけで動く。
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
SYNC = KIT / "tools" / "auto-sync.py"

FOLDERS = [
    "raw/sessions",
    "raw/memory",
    "raw/attachments",
    "areas/self",
    "areas/practice",
    "areas/dev-environment",
    "areas/people",
    "areas/glossary",
    "areas/threads",
]

BEGIN = "<!-- ai-context-kit:begin -->"
END = "<!-- ai-context-kit:end -->"
HOOK_EVENTS = ["Stop", "SessionEnd"]
STATUS_MESSAGE = "会話ログを外部脳に同期中"


def posix(p: Path | str) -> str:
    """設定に書くパスは `/` 区切りにする。Windows でも、フックを走らせる shell を選ばない。"""
    return str(p).replace("\\", "/")


def claude_dir() -> Path:
    """Claude Code の設定の置き場。CLAUDE_CONFIG_DIR があればそちらを使う。"""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def say(mark: str, msg: str) -> None:
    print(f"  [{mark}] {msg}")


def ask(question: str) -> bool:
    try:
        answer = input(f"  {question} [y/N]: ")
    except EOFError:
        print()
        return False
    return answer.strip().lower() in ("y", "yes")


def backup(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = path.with_name(f"{path.name}.bak-{stamp}")
    n = 1
    while dst.exists():
        dst = path.with_name(f"{path.name}.bak-{stamp}-{n}")
        n += 1
    shutil.copy2(path, dst)
    return dst


def show_diff(path: Path, old: str, new: str) -> None:
    diff = difflib.unified_diff(
        old.splitlines(),
        new.splitlines(),
        fromfile=f"{path}（いま）",
        tofile=f"{path}（書いたあと）",
        lineterm="",
    )
    for line in diff:
        print(f"    {line}")


def write_outside(path: Path, old: str | None, new: str, dry_run: bool) -> bool:
    """キットの外のファイルを書く。変更を見せ、聞き、写してから書く。"""
    show_diff(path, old or "", new)
    if dry_run:
        say("予定", f"{path} を書き換える（--dry-run なので書かない）")
        return False
    if not ask(f"{path} をこの内容で書きますか"):
        say(
            "飛ばす", "書きませんでした。あとで手で足すなら README の手順を見てください"
        )
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        say("写し", f"元のファイルを {backup(path).name} に写しました")
    path.write_text(new, encoding="utf-8", newline="\n")
    say("書いた", str(path))
    return True


# ---------------------------------------------------------------------------
# 1. フォルダ
# ---------------------------------------------------------------------------
def step_folders(dry_run: bool) -> None:
    print("\n1. フォルダの骨組み")
    missing = [f for f in FOLDERS if not (KIT / f).is_dir()]
    if not missing:
        say("済", "そろっています")
        return
    for f in missing:
        if dry_run:
            say("予定", f"{f}/ を作る")
        else:
            (KIT / f).mkdir(parents=True, exist_ok=True)
            say("作った", f"{f}/")


# ---------------------------------------------------------------------------
# 2. git
# ---------------------------------------------------------------------------
def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(KIT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def step_git(dry_run: bool) -> bool:
    print("\n2. git の準備")
    if shutil.which("git") is None:
        say(
            "飛ばす",
            "git が見つかりません。入れてから、もう一度このセットアップを走らせてください",
        )
        return False

    top = git("rev-parse", "--show-toplevel")
    here = top.returncode == 0 and Path(top.stdout.strip()).resolve() == KIT
    if top.returncode == 0 and not here:
        # 別のリポジトリの中に展開された。そこで init すると入れ子になり、フックの設定も
        # 外側のリポジトリに書いてしまうので、何もしない
        say(
            "止める",
            f"このフォルダは別の git リポジトリ（{top.stdout.strip()}）の中にあります",
        )
        say("止める", "キットのフォルダを、git 管理の外に移してから走らせてください")
        return False
    if not here:
        if dry_run:
            say("予定", "git init")
        else:
            r = git("init")
            if r.returncode != 0:
                say("失敗", (r.stderr or r.stdout).strip())
                return False
            say("作った", "git init")
            here = True
    else:
        say("済", "git リポジトリになっています")

    hook = KIT / ".githooks" / "pre-commit"
    if not hook.is_file():
        say(
            "飛ばす",
            ".githooks/pre-commit がありません。キットが欠けていないか確かめてください",
        )
        return here
    # ZIP で渡ると実行の権限が落ちることがある（Windows の git は見ないので、macOS・Linux 向け）
    if os.name != "nt" and not os.access(hook, os.X_OK):
        if dry_run:
            say("予定", ".githooks/pre-commit に実行の権限を付ける")
        else:
            hook.chmod(hook.stat().st_mode | 0o111)
            say("直した", ".githooks/pre-commit に実行の権限を付けました")

    current = git("config", "--local", "core.hooksPath").stdout.strip() if here else ""
    if current == ".githooks":
        say("済", "秘密情報を止めるフックは有効です")
    elif dry_run:
        say("予定", "git config core.hooksPath .githooks")
    else:
        git("config", "core.hooksPath", ".githooks")
        say("作った", "秘密情報を止めるフックを有効にしました（core.hooksPath）")

    if here and not git("remote").stdout.strip():
        say(
            "注意",
            "push 先（remote）がありません。無くても動きます（push だけ失敗し、止まらない）",
        )
    return True


# ---------------------------------------------------------------------------
# 3. 共通の指示（CLAUDE.md）
# ---------------------------------------------------------------------------
def entry_block() -> str:
    return (
        f"{BEGIN}\n"
        f"判断基準の正典は {posix(KIT)} にある。\n"
        "作業を始める前に、そこの CLAUDE.md → AGENTS.md → areas/self/vision.md → index.md の順に読む。\n"
        f"{END}\n"
    )


def step_claude_md(dry_run: bool) -> None:
    path = claude_dir() / "CLAUDE.md"
    print(f"\n3. Claude Code の共通の指示（{path}）")
    old = path.read_text(encoding="utf-8-sig") if path.is_file() else None
    block = entry_block()
    text = old or ""
    if BEGIN in text and END in text:
        start = text.index(BEGIN)
        stop = text.index(END, start) + len(END)
        if stop < len(text) and text[stop] == "\n":
            stop += 1
        if text[start:stop] == block:
            say("済", "このキットへの入口は書いてあります")
            return
        say(
            "更新",
            "前に書いた入口の場所が違うので、書き直します（キットを動かした場合）",
        )
        new = text[:start] + block + text[stop:]
    else:
        sep = (
            ""
            if not text or text.endswith("\n\n")
            else ("\n" if text.endswith("\n") else "\n\n")
        )
        new = text + sep + block
    write_outside(path, old, new, dry_run)


# ---------------------------------------------------------------------------
# 4. 設定（settings.json）
# ---------------------------------------------------------------------------
def hook_command() -> str:
    # 走らせている Python をそのまま使う。`python` という名前が通らない端末でも動く
    return f'"{posix(sys.executable)}" "{posix(SYNC)}"'


def is_sync_command(cmd: str) -> bool:
    return "tools/auto-sync.py" in posix(cmd)


def sync_target(cmd: str) -> Path | None:
    """登録済みの命令文から auto-sync.py のパスを取り出す。"""
    for part in cmd.split('"'):
        if posix(part).endswith("tools/auto-sync.py"):
            return Path(part)
    return None


def merge_hooks(settings: dict) -> tuple[dict, list[str]]:
    """フックを足した設定と、やったことの一覧を返す。既存の登録は消さない（動かないものを除く）。"""
    notes: list[str] = []
    merged = json.loads(json.dumps(settings))
    hooks = merged.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("settings.json の hooks が想定と違う形です")
    mine = posix(SYNC)
    for event in HOOK_EVENTS:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"settings.json の hooks.{event} が想定と違う形です")
        found = False
        emptied = []
        for group in groups:
            kept = []
            for h in group.get("hooks", []) if isinstance(group, dict) else []:
                cmd = h.get("command", "") if isinstance(h, dict) else ""
                if isinstance(cmd, str) and is_sync_command(cmd):
                    target = sync_target(cmd)
                    if target and posix(target) == mine:
                        found = True
                    elif target and not target.exists():
                        notes.append(f"{event}: 動かなくなった登録を外す -> {cmd}")
                        continue
                    else:
                        notes.append(
                            f"{event}: 別の場所の取り込みが登録されています（残します） -> {cmd}"
                        )
                kept.append(h)
            if isinstance(group, dict) and "hooks" in group:
                if group["hooks"] and not kept:
                    emptied.append(id(group))
                group["hooks"] = kept
        hooks[event] = [g for g in groups if id(g) not in emptied]
        if found:
            continue
        hooks[event].append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": hook_command(),
                        "async": True,
                        "timeout": 120,
                        "statusMessage": STATUS_MESSAGE,
                    }
                ]
            }
        )
        notes.append(f"{event}: 取り込みのフックを足す")
    return merged, notes


def step_settings(dry_run: bool) -> None:
    path = claude_dir() / "settings.json"
    print(f"\n4. Claude Code の設定（{path}）")
    if not SYNC.is_file():
        say(
            "飛ばす",
            "tools/auto-sync.py がありません。キットが欠けていないか確かめてください",
        )
        return
    old = path.read_text(encoding="utf-8-sig") if path.is_file() else None
    try:
        settings = json.loads(old) if old and old.strip() else {}
        if not isinstance(settings, dict):
            raise ValueError("settings.json の一番外側がオブジェクトではありません")
        merged, notes = merge_hooks(settings)
    except (json.JSONDecodeError, ValueError) as e:
        say(
            "止める",
            f"読めませんでした（{e}）。書き換えずに進みます。README の手順4を手で行ってください",
        )
        return
    if merged == settings:
        say("済", "取り込みのフックは登録されています")
        for n in notes:
            say("注意", n)
        return
    for n in notes:
        say("変更", n)
    say(
        "説明",
        "このフックは、どのフォルダの Claude Code のセッションでも、応答の区切りで会話をここへ取り込みます",
    )
    new = json.dumps(merged, ensure_ascii=False, indent=2) + "\n"
    write_outside(path, old, new, dry_run)


# ---------------------------------------------------------------------------
def next_steps() -> None:
    print("\n次にやること")
    print(f"  1. Claude Code を、このフォルダ（{KIT}）で開く")
    print("     入口の CLAUDE.md が自動で読まれます")
    print(
        "  2. 最初に areas/self/vision.md を書く（AI に喋って書き起こさせるのが早い）"
    )
    print("  3. 会話を取り込んでみる:")
    print(f'       "{sys.executable}" "{KIT / "tools" / "import-claude-sessions.py"}"')
    print("     Claude Code をまだ使っていなければ、会話してからにしてください")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:
            pass
    parser = argparse.ArgumentParser(description="キットを、この場で使える状態にする")
    parser.add_argument(
        "--dry-run", action="store_true", help="何も書かずに、やることだけ見せる"
    )
    args = parser.parse_args(argv)

    print(f"キットの場所: {KIT}")
    print(f"Claude Code の設定の場所: {claude_dir()}")
    if args.dry_run:
        print("--dry-run: 何も書きません")
    step_folders(args.dry_run)
    step_git(args.dry_run)
    step_claude_md(args.dry_run)
    step_settings(args.dry_run)
    next_steps()
    return 0


if __name__ == "__main__":
    sys.exit(main())
