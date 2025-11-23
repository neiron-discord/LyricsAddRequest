#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import re
import sys
from typing import Tuple

from github import Github

from lyrics_core import (
    search_lyrics_candidates,
    register_lyrics_from_request,
    format_lyrics_for_issue_body,
)


def load_event() -> dict:
    """GitHub Actions から渡される event payload を読む"""
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path or not os.path.exists(path):
        print("GITHUB_EVENT_PATH が見つかりません", file=sys.stderr)
        sys.exit(1)

    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _search_first(text: str, patterns) -> str | None:
    for p in patterns:
        m = re.search(p, text, flags=re.MULTILINE)
        if m:
            return m.group(1).strip()
    return None


def parse_issue_fields(title: str, body: str) -> Tuple[str, str, str]:
    """
    Issue のタイトル＋本文から
    - アーティスト
    - タイトル
    - 動画 ID
    を抜き出す。
    想定テンプレート:

      アーティスト: xxx
      タイトル: yyy
      動画 ID: by4SYYWlhEs

    ※「Artist:」「Title:」「Video ID:」でも OK
    """
    text = f"{title}\n{body}"

    artist = _search_first(
        text,
        [
            r"^アーティスト\s*[:：]\s*(.+)$",
            r"^Artist\s*[:：]\s*(.+)$",
        ],
    )

    song_title = _search_first(
        text,
        [
            r"^タイトル\s*[:：]\s*(.+)$",
            r"^曲名\s*[:：]\s*(.+)$",
            r"^Title\s*[:：]\s*(.+)$",
        ],
    )

    # 動画 ID or URL から ID を取る
    video_id = _search_first(
        text,
        [
            r"^(?:動画ID|動画 ID)\s*[:：]\s*([0-9A-Za-z_-]{6,})$",
            r"^Video ID\s*[:：]\s*([0-9A-Za-z_-]{6,})$",
            r"\b(?:https?://)?(?:www\.)?youtu(?:\.be/|be\.com/watch\?v=)([0-9A-Za-z_-]{6,})",
        ],
    )

    missing = []
    if not artist:
        missing.append("アーティスト")
    if not song_title:
        missing.append("タイトル")
    if not video_id:
        missing.append("動画 ID / Video ID")

    if missing:
        raise ValueError("必須項目が読み取れませんでした: " + ", ".join(missing))

    return artist, song_title, video_id


def _get_github():
    token = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GH_PAT または GITHUB_TOKEN が設定されていません", file=sys.stderr)
        sys.exit(1)
    return Github(token)


def main() -> None:
    event = load_event()
    issue = event.get("issue") or {}
    issue_number = issue.get("number")
    issue_title = issue.get("title") or ""
    issue_body = issue.get("body") or ""
    repo_name = os.environ.get("GITHUB_REPOSITORY")

    if not issue_number or not repo_name:
        print("GITHUB_REPOSITORY または issue 番号が取得できませんでした", file=sys.stderr)
        sys.exit(1)

    gh = _get_github()
    repo = gh.get_repo(repo_name)
    issue_obj = repo.get_issue(number=issue_number)

    # 1. Issue から必要情報をパース
    try:
        artist, title, video_id = parse_issue_fields(issue_title, issue_body)
    except Exception as e:
        issue_obj.create_comment(f"入力の解析に失敗しました。\n\n```\n{e}\n```")
        raise

    # 2. YouTube 自動字幕から歌詞を取得
    try:
        lyrics, vid, info = register_lyrics_from_request(artist, title, video_id)
    except Exception as e:
        issue_obj.create_comment(f"歌詞の取得に失敗しました。\n\n```\n{e}\n```")
        raise

    video_url = ""
    if isinstance(info, dict):
        video_url = info.get("url", "")
    if not video_url:
        video_url = f"https://www.youtube.com/watch?v={vid}"

    # 3. コメント本文を整形
    comment_body = format_lyrics_for_issue_body(artist, title, lyrics, video_url)

    # 4. Issue にコメント
    issue_obj.create_comment(comment_body)

    # 5. 任意: ラベルを付ける（無ければ無視）
    try:
        issue_obj.add_to_labels("lyrics-added")
    except Exception:
        pass


if __name__ == "__main__":
    main()
