#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
GitHub Actions: Issue body → 歌詞自動取得 → コメント返信
2025-11-23
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, List

import requests
from github import Github

# ---------- GitHub Event ----------


def load_github_event() -> Dict[str, Any]:
    """
    Actions から渡される GITHUB_EVENT_PATH から event JSON を読み込む。
    """
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        raise RuntimeError("環境変数 GITHUB_EVENT_PATH が設定されていません。")

    with open(event_path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------- Issue body parsing ----------

YOUTUBE_ID_PATTERNS = [
    # 「動画 ID: XXXXXXXX」
    re.compile(r"^動画\s*ID\s*[:：]\s*([0-9A-Za-z_-]{8,})\s*$", re.MULTILINE),
    # https://www.youtube.com/watch?v=XXXXXXXX
    re.compile(
        r"(?:https?://)?(?:www\.)?youtube\.com/watch\?[^ \n\r\t]*v=([0-9A-Za-z_-]{8,})"
    ),
    # https://youtu.be/XXXXXXXX
    re.compile(r"(?:https?://)?(?:www\.)?youtu\.be/([0-9A-Za-z_-]{8,})"),
]


def extract_video_id_from_text(text: str) -> Optional[str]:
    """
    本文から YouTube の video_id をゆるく抽出する。
    """
    if not text:
        return None
    for pat in YOUTUBE_ID_PATTERNS:
        m = pat.search(text)
        if m:
            vid = (m.group(1) or "").strip()
            if vid:
                return vid
    return None


def parse_issue_body(body: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    パターンA専用パーサー

    フォーマット例:
        1行目: "アーティスト - タイトル"
        2行目以降: 任意（YouTube URL やメモなど）
    """
    artist: Optional[str] = None
    title: Optional[str] = None

    # 行に分割して前後の空白を落とす
    lines = [line.strip() for line in (body or "").splitlines()]

    # ---- 1. 1 行目（または最初に見つかった行）から「アーティスト - タイトル」を取得 ----
    for line in lines:
        if not line:
            continue
        if " - " in line:
            left, right = line.split(" - ", 1)
            left, right = left.strip(), right.strip()
            if left or right:
                artist = left or None
                title = right or None
                break

    # ---- 2. 本文全体から YouTube の video_id を取得 ----
    video_id = extract_video_id_from_text(body or "")

    return artist, title, video_id


# ---------- Lyrics API (LrcLib 互換) ----------

LRC_LIB_BASE = "https://lrclib.net"


def _nf(s: str) -> str:
    """
    簡易正規化（NFKC + 小文字 + 連続空白の圧縮）。
    """
    import unicodedata as u

    t = u.normalize("NFKC", s or "")
    t = re.sub(r"\s+", " ", t)
    return t.strip().lower()


@dataclass
class LyricsRecord:
    id: int
    track_name: str
    artist_name: str
    album_name: Optional[str]
    duration: Optional[float]
    instrumental: bool
    plain_lyrics: Optional[str]
    synced_lyrics: Optional[str]


def lrclib_search(
    track_name: Optional[str] = None,
    artist_name: Optional[str] = None,
) -> Optional[LyricsRecord]:
    """
    歌詞 API /api/search を叩いて、最もそれっぽい 1 件を返す。
    （サービス名はコメントには出さない）
    """
    params: Dict[str, str] = {}
    if track_name:
        params["track_name"] = track_name
    if artist_name:
        params["artist_name"] = artist_name

    # どちらも無い場合は検索できない
    if not params:
        return None

    try:
        r = requests.get(f"{LRC_LIB_BASE}/api/search", params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[lyrics-api] search error: {e}")
        return None

    if not isinstance(data, list) or not data:
        return None

    # track_name / artist_name が両方ある場合は簡易スコアリング
    try:
        from rapidfuzz import fuzz  # type: ignore
    except Exception:
        fuzz = None  # type: ignore

    def score(rec: Dict[str, Any]) -> int:
        if not fuzz:
            # fuzzy が無ければ単純一致ボーナスだけ
            s = 0
            if track_name and rec.get("trackName"):
                s += 100 if _nf(track_name) == _nf(rec["trackName"]) else 0
            if artist_name and rec.get("artistName"):
                s += 100 if _nf(artist_name) == _nf(rec["artistName"]) else 0
            return s

        s = 0
        if track_name and rec.get("trackName"):
            s += fuzz.ratio(_nf(track_name), _nf(rec["trackName"]))
        if artist_name and rec.get("artistName"):
            s += fuzz.ratio(_nf(artist_name), _nf(rec["artistName"]))
        return s

    best = max(data, key=score)

    try:
        return LyricsRecord(
            id=int(best.get("id")),
            track_name=str(best.get("trackName") or best.get("name") or ""),
            artist_name=str(best.get("artistName") or ""),
            album_name=str(best["albumName"]) if best.get("albumName") else None,
            duration=float(best["duration"]) if best.get("duration") is not None else None,
            instrumental=bool(best.get("instrumental", False)),
            plain_lyrics=(best.get("plainLyrics") or None),
            synced_lyrics=(best.get("syncedLyrics") or None),
        )
    except Exception as e:
        print(f"[lyrics-api] parse record error: {e}")
        return None


# ---------- Build comment body ----------


def build_comment_body(
    artist: Optional[str],
    title: Optional[str],
    video_id: Optional[str],
    rec: Optional[LyricsRecord],
) -> str:
    lines: List[str] = []

    lines.append("自動歌詞登録の結果をお知らせします 🤖\n")

    # ---- 解析結果 ----
    lines.append("### 解析結果")
    lines.append(f"- アーティスト: **{artist}**" if artist else "- アーティスト: (未入力)")
    lines.append(f"- 楽曲名: **{title}**" if title else "- 楽曲名: (未入力)")
    if video_id:
        lines.append(f"- 動画 ID: `{video_id}`")

    # ---- 歌詞登録結果 ----
    lines.append("\n### 歌詞登録結果")

    if rec is None:
        lines.append("- ステータス: 歌詞を自動取得できませんでした。")
        if artist or title:
            used: List[str] = []
            if artist:
                used.append(f"artist='{artist}'")
            if title:
                used.append(f"title='{title}'")
            lines.append("- 使用情報: " + ", ".join(used))
        else:
            lines.append("- 使用情報: (なし / 解析失敗)")
    else:
        has_plain = bool(rec.plain_lyrics)
        has_synced = bool(rec.synced_lyrics)

        if has_synced:
            status = "Auto/同期あり"
        elif has_plain:
            status = "Auto/同期なし"
        else:
            status = "歌詞情報なし"

        lines.append(f"- ステータス: {status}")
        # サービス名は出さず、使ったメタだけ表示
        used_parts: List[str] = []
        if rec.artist_name:
            used_parts.append(f"artist='{rec.artist_name}'")
        if rec.track_name:
            used_parts.append(f"track='{rec.track_name}'")
        lines.append("- 検索に使用した情報: " + (", ".join(used_parts) or "(不明)"))

        # 追加のメタ情報
        extra_meta: List[str] = []
        if rec.album_name:
            extra_meta.append(f"album='{rec.album_name}'")
        if rec.duration:
            extra_meta.append(f"duration={rec.duration:.1f}s")
        if rec.instrumental:
            extra_meta.append("instrumental=true")
        if extra_meta:
            lines.append("- 付加情報: " + ", ".join(extra_meta))

        # ---- 歌詞データ本体（折りたたみ） ----
        if has_plain:
            lines.append("\n<details><summary>テキスト歌詞（plainLyrics）を表示</summary>\n")
            lines.append("```text")
            lines.append(rec.plain_lyrics or "")
            lines.append("```")
            lines.append("</details>")

        if has_synced:
            lines.append("\n<details><summary>同期付き歌詞（syncedLyrics）を表示</summary>\n")
            lines.append("```lrc")
            lines.append(rec.synced_lyrics or "")
            lines.append("```")
            lines.append("</details>")

    lines.append("\n---")
    lines.append(
        "※ このコメントは GitHub Actions の自動処理で追加されています。"
        " / フォーマット不備などでうまく登録できない場合があります。"
    )

    return "\n".join(lines)


# ---------- GitHub helpers ----------


def comment_to_issue(
    repo,
    issue_number: int,
    body: str,
) -> None:
    issue = repo.get_issue(number=issue_number)
    issue.create_comment(body)


# ---------- main ----------


def main() -> None:
    token = os.environ.get("GITHUB_TOKEN")
    repo_name = os.environ.get("GITHUB_REPOSITORY")

    if not token:
        raise RuntimeError("環境変数 GITHUB_TOKEN が設定されていません。")
    if not repo_name:
        raise RuntimeError("環境変数 GITHUB_REPOSITORY が設定されていません。")

    gh = Github(token)
    # /user を触らず、直接リポジトリだけ取るので 403 を回避できる
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

    # Issue 本文を解析
    artist, title, video_id = parse_issue_body(issue_body)
    print(f"parsed: artist={artist}, title={title}, video_id={video_id}")

    # 歌詞検索（動画 ID は不要。タイトル/アーティストだけで探す）
    rec = lrclib_search(track_name=title, artist_name=artist)
    if rec:
        print(
            "lyrics hit: "
            f"id={rec.id}, track={rec.track_name!r}, artist={rec.artist_name!r}"
        )
    else:
        print("lyrics not found.")

    # 結果をコメントとして Issue に投稿
    comment_body = build_comment_body(artist, title, video_id, rec)
    comment_to_issue(repo, issue_number, comment_body)

    print("処理が完了しました。")


if __name__ == "__main__":
    main()
