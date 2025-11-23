#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Issue 本文からアーティスト/曲名/動画IDを取り出し、
外部歌詞APIから plainLyrics / syncedLyrics を取得して
Issue に結果コメントを追加するだけのスクリプト。

- リポジトリ作成や README 作成はやらない
- 歌詞サービス名はコメントに書かない
- ローカルPC用スクリプトが機械的に拾えるように
  JSON ペイロードもコメントに埋め込む
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests
from github import Github, Auth


ROOT_DIR = Path(__file__).resolve().parent.parent


# ---------- GitHub イベント読み込み ----------

def load_github_event() -> Dict[str, Any]:
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path:
        raise RuntimeError("環境変数 GITHUB_EVENT_PATH が設定されていません。")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------- Issue 本文パース（パターンA） ----------

YOUTUBE_PATTERNS = [
    # https://youtu.be/<id>
    r"(?:https?://)?(?:www\.)?youtu\.be/([0-9A-Za-z_-]{8,})",
    # https://www.youtube.com/watch?v=<id>
    r"(?:https?://)?(?:www\.)?youtube\.com/watch\?v=([0-9A-Za-z_-]{8,})",
    # https://www.youtube.com/shorts/<id>
    r"(?:https?://)?(?:www\.)?youtube\.com/shorts/([0-9A-Za-z_-]{8,})",
]


def extract_video_id_from_text(text: str) -> Optional[str]:
    for pat in YOUTUBE_PATTERNS:
        m = re.search(pat, text)
        if m:
            vid = m.group(1).strip()
            if vid:
                return vid
    return None


def parse_issue_body(body: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    パターンA:
      1行目: "アーティスト - タイトル"
      2行目以降: 任意。YouTubeリンクがあれば動画IDを取る。

    戻り値: (artist, title, video_id)
    """
    artist: Optional[str] = None
    title: Optional[str] = None
    video_id: Optional[str] = None

    lines = [line.strip() for line in (body or "").splitlines()]

    # 1行目から「アーティスト - タイトル」を取得
    for line in lines:
        if not line:
            continue
        if " - " in line:
            left, right = line.split(" - ", 1)
            artist = (left or "").strip() or None
            title = (right or "").strip() or None
            break

    # 本文全体から YouTube 動画IDを取得
    video_id = extract_video_id_from_text(body or "")

    return artist, title, video_id


# ---------- 歌詞 API (名前は出さない) ----------

LRC_LIB_BASE = "https://lrclib.net"   # コード内だけで使用。コメントには書かない。


def _nf_lrc(s: str) -> str:
    import unicodedata as u
    t = u.normalize("NFKC", s or "")
    return re.sub(r"\s+", " ", t).strip().lower()


def search_lyrics_by_artist_title(
    artist: Optional[str],
    title: Optional[str],
) -> Optional[Dict[str, Any]]:
    """
    外部歌詞API /api/search を叩いて最も良さそうな1件を返す。
    コメントにサービス名は出さない。
    """
    if not artist and not title:
        return None

    params: Dict[str, str] = {}
    if title:
        params["track_name"] = title
    if artist:
        params["artist_name"] = artist

    # どちらかは必須
    if not params:
        return None

    try:
        r = requests.get(f"{LRC_LIB_BASE}/api/search", params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[lyrics] search error: {e}")
        return None

    if not isinstance(data, list) or not data:
        return None

    # track_name / artist_name がある場合は簡易スコア
    def score(rec: Dict[str, Any]) -> int:
        s = 0
        if title and rec.get("trackName"):
            s += 2 * (100 - abs(len(_nf_lrc(title)) - len(_nf_lrc(rec["trackName"]))))
        if artist and rec.get("artistName"):
            s += 2 * (100 - abs(len(_nf_lrc(artist)) - len(_nf_lrc(rec["artistName"]))))
        return s

    if artist or title:
        best = max(data, key=score)
        return best

    return data[0]


# ---------- コメント生成 ----------

JSON_START = "<!-- LYRICS_API_JSON_START -->"
JSON_END = "<!-- LYRICS_API_JSON_END -->"


def build_comment_body(
    artist: Optional[str],
    title: Optional[str],
    video_id: Optional[str],
    rec: Optional[Dict[str, Any]],
) -> str:
    lines: list[str] = []

    lines.append("自動歌詞登録の結果をお知らせします 🤖\n")

    # 解析結果
    lines.append("### 解析結果")
    lines.append(f"- アーティスト: **{artist}**" if artist else "- アーティスト: (未入力)")
    lines.append(f"- 楽曲名: **{title}**" if title else "- 楽曲名: (未入力)")
    if video_id:
        lines.append(f"- 動画 ID: `{video_id}`")
    else:
        lines.append("- 動画 ID: (未指定)")

    # 歌詞結果
    lines.append("\n### 歌詞登録結果")

    if rec is None:
        lines.append("- ステータス: 歌詞の取得に失敗しました")
        lines.append("- 取得元: 外部歌詞データベース（取得エラー）")
    else:
        plain = (rec.get("plainLyrics") or "").strip()
        synced = (rec.get("syncedLyrics") or "").strip()

        if synced:
            status = "Auto/同期あり"
        elif plain:
            status = "Auto/同期なし"
        else:
            status = "歌詞の登録なし"

        lines.append(f"- ステータス: {status}")
        lines.append("- 取得元: 外部歌詞データベース")
        tn = (rec.get("trackName") or rec.get("name") or "").strip()
        an = (rec.get("artistName") or "").strip()
        detail = []
        if tn:
            detail.append(f"track='{tn}'")
        if an:
            detail.append(f"artist='{an}'")
        if detail:
            lines.append(f"- 取得詳細: {', '.join(detail)}")

        # 人間向けに歌詞本体も（長くなる場合あり）
        if synced:
            lines.append("\n#### syncedLyrics（タイミング付き）")
            lines.append("```lrc")
            lines.append(synced)
            lines.append("```")

        if plain:
            lines.append("\n#### plainLyrics（テキストのみ）")
            lines.append("```text")
            lines.append(plain)
            lines.append("```")

    # ローカルPC用：機械が読み取る JSON ペイロード
    payload: Dict[str, Any] = {
        "videoId": video_id,
        "artist": artist,
        "title": title,
        "sourceRecord": rec,  # None でもそのまま
    }

    lines.append("\n---")
    lines.append("以下はローカルスクリプト用のペイロードです（編集しないでください）。")
    lines.append(JSON_START)
    lines.append("```json")
    lines.append(json.dumps(payload, ensure_ascii=False, indent=2))
    lines.append("```")
    lines.append(JSON_END)

    lines.append("\n※ このコメントは GitHub Actions の自動処理で追加されています。")

    return "\n".join(lines)


def comment_to_issue(repo, issue_number: int, body: str) -> None:
    issue = repo.get_issue(number=issue_number)
    issue.create_comment(body)


# ---------- メイン ----------

def main() -> None:
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

    if not issue_data:
        print("issue イベントではないため何もしません。")
        return

    issue_number = issue_data["number"]
    issue_body = issue_data.get("body") or ""

    print(f"action={action}, issue_number={issue_number}")

    if action not in {"opened", "edited"}:
        print("opened/edited 以外のアクションなのでスキップします。")
        return

    artist, title, video_id = parse_issue_body(issue_body)
    print(f"parsed: artist={artist}, title={title}, video_id={video_id}")

    # 歌詞検索
    rec = search_lyrics_by_artist_title(artist, title)
    if rec:
        print("[lyrics] record found:", rec.get("id"), rec.get("trackName"), rec.get("artistName"))
    else:
        print("[lyrics] no record found")

    comment_body = build_comment_body(artist, title, video_id, rec)
    comment_to_issue(repo, issue_number, comment_body)
    print("comment posted.")


if __name__ == "__main__":
    main()
