# scripts/handle_issue.py
# LyricsAddRequest の Issue を受けて歌詞を登録する GitHub Actions 用スクリプト

from __future__ import annotations
import os
from typing import Tuple

from github import Github
import yt_dlp

from lyrics_core import (
    github_get_lyrics,
    github_save_lyrics,
    song_only,
    lrclib_search,
    lrclib_to_lyrics,
    pl_search_smart,
    pl_fetch,
    _canon_music_meta,
    _display_title_for,
)

# ─────────────────────────────────────────
# YouTube 検索 helper
# ─────────────────────────────────────────

YTDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "nocheckcertificate": True,
    "restrictfilenames": True,
    "default_search": "ytsearch1",  # 1件だけ
    "noplaylist": True,
    "cachedir": False,
    "source_address": "0.0.0.0",
    "socket_timeout": 7,
}


def search_youtube_by_artist_title(artist: str, title: str) -> dict:
    """
    アーティスト + 曲名で YouTube を検索し、1件目の info dict を返す。
    """
    query = f"{artist} {title} official audio"
    try:
        with yt_dlp.YoutubeDL(YTDL_OPTS) as ydl:
            info = ydl.extract_info(query, download=False)
    except Exception as e:
        raise RuntimeError(f"YouTube 検索に失敗しました: {e}") from e

    if isinstance(info, dict) and info.get("entries"):
        info = info["entries"][0]

    if not isinstance(info, dict) or not info.get("id"):
        raise RuntimeError("YouTube 検索で動画が見つかりませんでした")

    return info


# ─────────────────────────────────────────
# 歌詞取得ロジック
# ─────────────────────────────────────────

def fetch_lyrics_for_info(info: dict) -> Tuple[object, object, object]:
    """
    YouTube info から、LrcLib → PetitLyrics の順に歌詞を探す。
    戻り値: (plain, cues, source_code)
      - plain: プレーン歌詞 or None
      - cues:  同期歌詞 list[dict] or None
      - source_code: 1=LrcLib, 3=PetitLyrics, None=何もなし
    """
    display, meta = _canon_music_meta(info or {})
    info["_music_meta"] = meta

    yt_full_title = info.get("title") or display or ""
    track_name = meta.get("track") or song_only(yt_full_title)

    # 1) LrcLib
    rec = lrclib_search(track_name) if track_name else None
    if rec:
        plain, cues = lrclib_to_lyrics(rec)
        if cues:
            return plain, cues, 1
        if plain:
            return plain, None, 1

    # 2) PetitLyrics（曲名オンリー）
    base_title = track_name or yt_full_title
    if base_title:
        pid = pl_search_smart(base_title)
        if pid:
            plain = pl_fetch(pid)
            if plain:
                return plain, None, 3

    # 3) 何も見つからない
    return None, None, None


def register_lyrics_from_request(artist: str, title: str):
    """
    Issue から渡された (artist, title) を元に
      - YouTube 検索
      - 既存 GitHub 歌詞リポジトリの有無チェック
      - 歌詞取得 & 新規登録
    を行う。

    戻り値: (result, vid, info)
      result:
        "already"   … 既に歌詞リポジトリあり
        "ok"        … 歌詞ありで新規登録
        "no_lyrics" … 歌詞見つからず『歌詞の登録なし』として新規登録
    """
    info = search_youtube_by_artist_title(artist, title)
    vid = info.get("id")
    if not vid:
        raise RuntimeError("動画IDが取得できませんでした")

    # 既存チェック
    existing = github_get_lyrics(vid)
    if existing:
        return "already", vid, info

    # 歌詞取得
    plain, cues, src = fetch_lyrics_for_info(info)

    display_title = _display_title_for(info)
    yt_full_title = info.get("title") or display_title
    meta = info.get("_music_meta") or {}

    if cues:
        status = "Auto/同期あり"
    elif plain:
        status = "Auto/同期なし"
    else:
        status = "歌詞の登録なし"

    github_save_lyrics(
        repo_name=vid,
        title=display_title,
        status=status,
        plain=plain,
        cues=cues,
        source_code=src,
        yt_full_title=yt_full_title,
        music_meta=meta,
    )

    if plain or cues:
        return "ok", vid, info
    else:
        return "no_lyrics", vid, info


# ─────────────────────────────────────────
# GitHub Actions エントリポイント
# ─────────────────────────────────────────

def main():
    # PAT（Issue にコメントする用。歌詞保存用は lyrics_core 側も同じ環境変数を見ている）
    token = os.environ.get("LYRICS_GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("LYRICS_GH_TOKEN または GITHUB_TOKEN が設定されていません")

    gh = Github(token)

    repo_name = os.environ["GITHUB_REPOSITORY"]          # 例: "neiron-discord/LyricsAddRequest"
    issue_number = int(os.environ["ISSUE_NUMBER"])       # トリガーになった Issue 番号

    repo = gh.get_repo(repo_name)
    issue = repo.get_issue(number=issue_number)

    # Issue 本文パース（1行目=アーティスト、2行目=曲名）
    lines = [l.strip() for l in (issue.body or "").splitlines() if l.strip()]
    if len(lines) < 2:
        issue.create_comment(
            "フォーマットは以下の2行で入力してください：\n"
            "```text\nアーティスト名\n曲名\n```"
        )
        issue.edit(state="closed")
        return

    artist, title = lines[0], lines[1]

    try:
        result, vid, info = register_lyrics_from_request(artist, title)
    except Exception as e:
        issue.create_comment(
            "歌詞登録中にエラーが発生しました。\n"
            "```\n"
            f"{e}\n"
            "```"
        )
        raise

    user = gh.get_user()
    lyrics_repo_url = f"https://github.com/{user.login}/{vid}"

    if result == "already":
        issue.create_comment(
            f"この曲の歌詞は既に登録されています。\n{lyrics_repo_url}"
        )
    elif result == "ok":
        issue.create_comment(
            f"歌詞の登録に成功しました。\n{lyrics_repo_url}"
        )
    else:  # "no_lyrics"
        issue.create_comment(
            "歌詞を見つけられなかったため、『歌詞の登録なし』として登録しました。\n"
            f"{lyrics_repo_url}"
        )

    issue.edit(state="closed")


if __name__ == "__main__":
    main()
