#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import tempfile
from typing import Any, Dict, List, Tuple

import yt_dlp


def _cookie_file() -> str | None:
    """
    YT の cookie ファイルのパスを推測する。
    優先順位:
      1. 環境変数 YT_COOKIES_FILE
      2. リポジトリルートの youtube_cookies.txt
    """
    path = os.environ.get("YT_COOKIES_FILE")
    if path and os.path.exists(path):
        return path

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path2 = os.path.join(repo_root, "youtube_cookies.txt")
    if os.path.exists(path2):
        return path2

    return None


def _base_ydl_opts() -> Dict[str, Any]:
    """共通の yt-dlp オプション"""
    opts: Dict[str, Any] = {
        "quiet": True,
        "skip_download": True,
        "ignoreerrors": True,
        "nocheckcertificate": True,
        "noplaylist": True,
    }
    cookie = _cookie_file()
    if cookie:
        opts["cookiefile"] = cookie
    return opts


def _download_auto_sub_srt(video_id: str) -> str:
    """
    yt-dlp を使って自動生成字幕を SRT 形式で一時ディレクトリに保存し、そのパスを返す。
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    tmp_dir = tempfile.gettempdir()
    out_path = os.path.join(tmp_dir, f"{video_id}.srt")

    ydl_opts = _base_ydl_opts()
    # SRT で自動生成字幕だけを保存
    ydl_opts.update(
        {
            "writeautomaticsub": True,
            "subtitlesformat": "srt",
            "skip_download": True,
            "outtmpl": out_path,
        }
    )

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        # list で渡す必要がある
        ydl.download([url])

    if not os.path.exists(out_path):
        raise RuntimeError(f"SRT 字幕ファイルが生成されませんでした: {out_path}")

    return out_path


def _srt_to_lyrics(path: str) -> str:
    """
    SRT ファイルから歌詞テキストだけを抜き出す (タイムスタンプと番号行は削除)。
    """
    lines: List[str] = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            if line.isdigit():
                # インデックス番号
                continue
            if "-->" in line:
                # タイムスタンプ行
                continue
            lines.append(line)

    # 同じ行が連続していることがあるので軽く除去
    merged: List[str] = []
    last: str | None = None
    for line in lines:
        if line == last:
            continue
        merged.append(line)
        last = line

    return "\n".join(merged)


def search_lyrics_candidates(*args, **kwargs) -> List[Dict[str, Any]]:
    """
    互換性用のダミー実装。
    もし別サイトから歌詞候補を検索したくなったら、ここに実装を追加する。
    今は常に空リストを返す。
    """
    return []


def register_lyrics_from_request(
    artist: str,
    title: str,
    video_id: str,
) -> Tuple[str, str, Dict[str, Any]]:
    """
    必須: YouTube の動画 ID。
    自動生成字幕から歌詞を取得して (lyrics, video_id, info) を返す。
    """
    srt_path = _download_auto_sub_srt(video_id)
    lyrics = _srt_to_lyrics(srt_path)

    info: Dict[str, Any] = {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "artist": artist,
        "title": title,
    }
    return lyrics, video_id, info


def format_lyrics_for_issue_body(
    artist: str,
    title: str,
    lyrics: str,
    video_url: str | None = None,
) -> str:
    """
    GitHub Issue のコメント用に歌詞を整形する。
    """
    header = f"**{artist} - {title}**"
    if video_url:
        header += f"\n\n[YouTube]({video_url})"

    body = f"{header}\n\n```text\n{lyrics}\n```"
    return body
