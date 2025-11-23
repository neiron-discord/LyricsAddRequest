#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
GitHub Issues から「歌詞追加リクエスト」を受け取り、
アーティスト名・楽曲名・YouTube 動画IDをパースして
必要に応じて YouTube 検索を行い、その結果を Issue にコメントするスクリプト。

2025/11/23 パターンA専用:
  1行目: "アーティスト - タイトル"
  本文どこかに YouTube URL があれば video_id を抽出
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from github import Github, Auth
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


ROOT_DIR = Path(__file__).resolve().parent.parent
WORKDIR = ROOT_DIR

# デフォルトの cookies.txt のパス（ワークフローステップで書き出した想定）
DEFAULT_COOKIES_PATH = ROOT_DIR / "youtube_cookies.txt"


# ---------- GitHub イベント関連 ----------


def load_github_event() -> Dict[str, Any]:
    """
    Actions から渡される GITHUB_EVENT_PATH から event JSON を読み込む。
    """
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        raise RuntimeError("環境変数 GITHUB_EVENT_PATH が設定されていません。")

    with open(event_path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------- 文字列 → YouTube video_id 抽出 ----------


YOUTUBE_URL_PATTERNS = [
    # https://www.youtube.com/watch?v=XXXXXXXXXXX
    r"(?:https?://)?(?:www\.)?youtube\.com/watch\?v=([0-9A-Za-z_-]{8,})",
    # https://youtu.be/XXXXXXXXXXX
    r"(?:https?://)?(?:www\.)?youtu\.be/([0-9A-Za-z_-]{8,})",
]


def extract_video_id_from_text(text: str) -> Optional[str]:
    """
    テキスト全体から YouTube の動画IDを1件だけ抜き出す。

    - https://www.youtube.com/watch?v=...
    - https://youtu.be/...

    の両方に対応。
    """
    for pat in YOUTUBE_URL_PATTERNS:
        m = re.search(pat, text)
        if not m:
            continue
        vid = m.group(1)
        # "&t=123s" などがくっついていた場合に備えて "&" で区切る
        vid = vid.split("&", 1)[0]
        if vid:
            return vid
    return None


# ---------- Issue 本文パース（パターンA専用） ----------


def parse_issue_body(body: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    パターンA専用パーサー。

    フォーマット:
        1行目: "アーティスト - タイトル"
        2行目以降: 任意。YouTube の URL が含まれていれば video_id を取得する。

    戻り値: (artist, title, video_id)
    """
    artist: Optional[str] = None
    title: Optional[str] = None
    video_id: Optional[str] = None

    # 行に分割して前後の空白を落とす
    lines = [line.strip() for line in body.splitlines()]

    # ---- 1. 1行目から「アーティスト - タイトル」を取得 ----
    for line in lines:
        if not line:
            continue
        # " - " で分割（最初に見つかった "-" だけ使う）
        if " - " in line:
            left, right = line.split(" - ", 1)
            artist = left.strip() or None
            title = right.strip() or None
            break

    # ---- 2. 本文全体から YouTube の video_id を取得 ----
    video_id = extract_video_id_from_text(body)

    return artist, title, video_id


# ---------- YouTube 検索 ----------


def search_youtube_by_artist_title(
    artist: str,
    title: str,
    cookies_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    アーティスト + 曲名 から yt-dlp を使って YouTube を検索。
    先頭の 1 件を返す。
    """
    query = f"{artist} {title}"

    ydl_opts: Dict[str, Any] = {
        "quiet": True,
        "skip_download": True,
        "default_search": "ytsearch1",  # 1件だけ検索
    }

    if cookies_path and cookies_path.is_file():
        # --cookies <file> 相当
        ydl_opts["cookiefile"] = str(cookies_path)

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=False)
    except DownloadError as e:
        raise RuntimeError(f"YouTube 検索に失敗しました: {e}") from e

    # ytsearch の場合は entries の中に入っている
    if "entries" in info and isinstance(info["entries"], list) and info["entries"]:
        info = info["entries"][0]

    return info


def ensure_cookies_path() -> Optional[Path]:
    """
    クッキーファイルのパスを決定する。

    - まず環境変数 YT_COOKIES_FILE を見る
    - なければデフォルトの youtube_cookies.txt（ワークフローで書き出した想定）
    - それでもなければ None
    """
    env_path = os.environ.get("YT_COOKIES_FILE")
    if env_path:
        p = Path(env_path)
        if p.is_file():
            return p

    if DEFAULT_COOKIES_PATH.is_file():
        return DEFAULT_COOKIES_PATH

    return None


# ---------- GitHub への反映 ----------


def build_comment_body(
    artist: Optional[str],
    title: Optional[str],
    video_id: Optional[str],
    info: Optional[Dict[str, Any]],
) -> str:
    """
    Issue に書き込むコメント本文を作る。
    """
    lines = []
    lines.append("自動処理結果をお知らせします :robot:\n")

    lines.append("### 解析結果")
    if artist:
        lines.append(f"- アーティスト: **{artist}**")
    else:
        lines.append("- アーティスト: (未入力)")
    if title:
        lines.append(f"- 楽曲名: **{title}**")
    else:
        lines.append("- 楽曲名: (未入力)")

    if video_id:
        lines.append(f"- 動画 ID: `{video_id}`")

    if info:
        vid = info.get("id") or video_id
        url = info.get("webpage_url") or (
            f"https://www.youtube.com/watch?v={vid}" if vid else None
        )
        yt_title = info.get("title")
        duration = info.get("duration")
        lines.append("\n### YouTube 検索結果")
        if yt_title:
            lines.append(f"- 動画タイトル: **{yt_title}**")
        if url:
            lines.append(f"- URL: {url}")
        if duration:
            lines.append(f"- 再生時間: {duration} 秒")

    lines.append("\n---")
    lines.append(
        "※ このコメントは GitHub Actions の自動処理で追加されています。"
    )

    return "\n".join(lines)


def comment_to_issue(
    repo,
    issue_number: int,
    body: str,
) -> None:
    issue = repo.get_issue(number=issue_number)
    issue.create_comment(body)


# ---------- メイン処理 ----------


def main() -> None:
    # GitHub 認証
    token = os.environ.get("GITHUB_TOKEN")
    repo_name = os.environ.get("GITHUB_REPOSITORY")

    if not token:
        raise RuntimeError("環境変数 GITHUB_TOKEN が設定されていません。")
    if not repo_name:
        raise RuntimeError("環境変数 GITHUB_REPOSITORY が設定されていません。")

    gh = Github(auth=Auth.Token(token))
    repo = gh.get_repo(repo_name)

    event = load_github_event()
    action = event.get("action")
    issue_data = event.get("issue")

    # issue イベントでなければスキップ
    if not issue_data:
        print("issue イベントではないため何もしません。")
        return

    issue_number = issue_data["number"]
    issue_body = issue_data.get("body") or ""

    print(f"action={action}, issue_number={issue_number}")

    # opened / edited の時だけ処理する
    if action not in {"opened", "edited"}:
        print("opened/edited 以外のアクションなのでスキップします。")
        return

    # Issue 本文を解析（パターンA）
    artist, title, video_id = parse_issue_body(issue_body)
    print(f"parsed: artist={artist}, title={title}, video_id={video_id}")

    cookies_path = ensure_cookies_path()
    if cookies_path:
        print(f"Using cookies file: {cookies_path}")
    else:
        print("cookies ファイルが見つからなかったので未ログイン状態で実行します。")

    info: Optional[Dict[str, Any]] = None

    # アーティストと曲名がそろっている場合にだけ YouTube 検索
    if artist and title:
        try:
            info = search_youtube_by_artist_title(
                artist=artist,
                title=title,
                cookies_path=cookies_path,
            )
            if not video_id:
                video_id = info.get("id")
        except RuntimeError as e:
            # 検索エラーはそのままコメントに書く
            err_body = (
                f"自動処理で YouTube 検索を試みましたが失敗しました。\n\n"
                f"```\n{e}\n```\n"
            )
            comment_to_issue(repo, issue_number, err_body)
            raise

    # 結果をコメントとして Issue に投稿
    comment_body = build_comment_body(artist, title, video_id, info)
    comment_to_issue(repo, issue_number, comment_body)

    print("処理が完了しました。")


if __name__ == "__main__":
    main()
