#!/usr/bin/env python
import os
import json
import re
from typing import Tuple, Optional, List

import yt_dlp
import requests
from bs4 import BeautifulSoup
from github import Github

# lyrics_core はこちらで作ったものを使う想定
from lyrics_core import (
    search_lyrics_candidates,
    fetch_lyrics_page,
    choose_best_lyrics,
    build_markdown_comment,
)

# cookie ファイルのパスを渡すための環境変数名
COOKIE_FILE_ENV = "YOUTUBE_COOKIES_FILE"


# ----------------------------------------------------
# YouTube 検索（yt-dlp + cookie 対応）
# ----------------------------------------------------
def search_youtube_by_artist_title(artist: str, title: str) -> dict:
    """
    アーティスト名と曲名から YouTube を検索し、
    一番それっぽい動画の情報を返す。

    戻り値は yt-dlp の info dict。
    """
    query = f"{artist} {title} official audio"
    cookiefile = os.environ.get(COOKIE_FILE_ENV)

    ydl_opts = {
        "quiet": True,
        "skip_download": True,
        "noplaylist": True,
        "default_search": "ytsearch5",
    }

    # cookie ファイルがあれば yt-dlp に渡す
    if cookiefile and os.path.exists(cookiefile):
        ydl_opts["cookiefile"] = cookiefile

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(query, download=False)
        except Exception as e:
            raise RuntimeError(f"YouTube 検索に失敗しました: {e}") from e

    # ytsearch の場合は 'entries' に複数入るので先頭を採用
    if "entries" in info and info["entries"]:
        return info["entries"][0]
    return info


# ----------------------------------------------------
# Issue 本文のパース
# ----------------------------------------------------
def parse_issue_body(body: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Issue 本文から アーティスト / タイトル を抜き出す簡易パーサ。
    """
    artist = None
    title = None

    # よくある書き方:
    # アーティスト: 〜
    # タイトル: 〜
    # Artist: 〜
    # Title: 〜
    patterns = [
        r"アーティスト[:：]\s*(.+)",
        r"タイトル[:：]\s*(.+)",
        r"Artist[:：]\s*(.+)",
        r"Title[:：]\s*(.+)",
    ]

    for line in body.splitlines():
        line = line.strip()
        for p in patterns:
            m = re.match(p, line, flags=re.IGNORECASE)
            if not m:
                continue
            value = m.group(1).strip()
            if "アーティスト" in p or "Artist" in p:
                artist = value
            elif "タイトル" in p or "Title" in p:
                title = value

    return artist, title


# ----------------------------------------------------
# 歌詞登録ロジック（YouTube 検索 → 歌詞サイト検索）
# ----------------------------------------------------
def register_lyrics_from_request(artist: str, title: str):
    """
    アーティスト/タイトル から:
      1. YouTube 動画を検索
      2. 歌詞サイトを検索
      3. 一番よさそうな歌詞を選ぶ
    までをまとめて実行し、結果を返す。
    """
    # 1. YouTube 検索
    yt_info = search_youtube_by_artist_title(artist, title)
    video_id = yt_info.get("id")
    video_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else None

    # 2. 歌詞サイトを検索（lyrics_core 側に任せる前提）
    candidates = search_lyrics_candidates(artist, title)
    if not candidates:
        return "lyrics_not_found", video_id, {"yt_info": yt_info, "lyrics": None}

    # 3. 各候補から歌詞ページを取得
    pages = [fetch_lyrics_page(c["url"]) for c in candidates]

    # 4. 一番よさそうな歌詞を選ぶ
    best = choose_best_lyrics(artist, title, candidates, pages)

    return "ok", video_id, {
        "yt_info": yt_info,
        "lyrics": best,
    }


# ----------------------------------------------------
# GitHub Issue との連携
# ----------------------------------------------------
def load_issue_from_env():
    """
    GITHUB_EVENT_PATH から issue 情報を読み込む。
    """
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path or not os.path.exists(event_path):
        raise RuntimeError("GITHUB_EVENT_PATH が見つかりません")

    with open(event_path, "r", encoding="utf-8") as f:
        event = json.load(f)

    issue = event["issue"]
    repo_full = event["repository"]["full_name"]

    number = issue["number"]
    title = issue["title"]
    body = issue.get("body") or ""
    labels = [lbl["name"] for lbl in issue.get("labels", [])]

    return repo_full, number, title, body, labels


def post_issue_comment(repo, issue_number: int, body: str):
    issue = repo.get_issue(number=issue_number)
    issue.create_comment(body)


# ----------------------------------------------------
# main
# ----------------------------------------------------
def main():
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN が設定されていません")

    # GitHub クライアント
    gh = Github(token)

    repo_name, issue_number, issue_title, issue_body, labels = load_issue_from_env()
    repo = gh.get_repo(repo_name)

    artist, title = parse_issue_body(issue_body)

    if not artist or not title:
        msg = (
            "アーティスト名 / タイトルが読み取れませんでした。\n\n"
            "フォーマットの例:\n"
            "```\n"
            "アーティスト: xxxx\n"
            "タイトル: yyyy\n"
            "```"
        )
        post_issue_comment(repo, issue_number, msg)
        return

    try:
        result, vid, info = register_lyrics_from_request(artist, title)
    except Exception as e:
        post_issue_comment(repo, issue_number, f"処理中にエラーが発生しました: {e}")
        raise

    if result != "ok":
        post_issue_comment(
            repo,
            issue_number,
            f"「{artist} - {title}」の歌詞が見つかりませんでした。",
        )
        return

    # lyrics_core で Markdown コメントを組み立てる想定
    comment_md = build_markdown_comment(artist, title, info)
    post_issue_comment(repo, issue_number, comment_md)


if __name__ == "__main__":
    main()
