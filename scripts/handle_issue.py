#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
LyricsAddRequest: Issue から LrcLib 経由で歌詞を登録するスクリプト
2025-11-23

・前提
  - GitHub Actions 上で実行
  - 環境変数:
      GITHUB_TOKEN        … PAT or Actions のトークン
      GITHUB_REPOSITORY   … neiron-discord/LyricsAddRequest など
      GITHUB_EVENT_PATH   … issue イベント JSON のパス（Actions がセット）
  - Issue 本文フォーマット（目安）:
      1行目: "アーティスト - 曲名"
      それ以降: YouTube URL か 動画ID（どこかに含まれていればOK）

・やること
  1. Issue から artist, title, video_id を取り出す
  2. title / artist から LrcLib を叩いて歌詞を取得
  3. GitHub ユーザー neiron-discord の "<video_id>" リポジトリに
     YoutubeGlbot.py と同じ形式で README.md を生成
  4. Issue に結果をコメントして終了
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from rapidfuzz import fuzz

from github import Github, Auth, GithubException

# ─────────────────────────────────────────
# 共通設定
# ─────────────────────────────────────────

ROOT_DIR = Path(__file__).resolve().parent.parent


def load_github_event() -> Dict[str, Any]:
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path:
        raise RuntimeError("環境変数 GITHUB_EVENT_PATH がありません")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ─────────────────────────────────────────
# Issue 本文パース
# ─────────────────────────────────────────

YOUTUBE_ID_RE = re.compile(r"(?:youtu\.be/|v=)([0-9A-Za-z_-]{8,})")
RAW_ID_RE = re.compile(r"\b([0-9A-Za-z_-]{8,})\b")


def extract_video_id_from_text(text: str) -> Optional[str]:
    """
    本文中から YouTube 動画IDを探す。
      - https://youtu.be/<id>
      - https://www.youtube.com/watch?v=<id>
      - 裸の ID (英数_- 8文字以上) も一応許容
    """
    m = YOUTUBE_ID_RE.search(text)
    if m:
        return m.group(1)

    # URL 形式が無くても、"by4SYYWlhEs" みたいなのがあれば拾う
    for m in RAW_ID_RE.finditer(text):
        vid = m.group(1)
        # あまり長いと別物の可能性があるので 32 くらいで打ち切り
        if 8 <= len(vid) <= 32:
            return vid
    return None


def parse_issue_body(body: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    フォーマット:
      1行目: "アーティスト - タイトル"
      2行目以降: どこかに YouTube URL or 動画ID

    戻り値: (artist, title, video_id)
    """
    artist: Optional[str] = None
    title: Optional[str] = None

    lines = [l.strip() for l in body.splitlines() if l.strip()]

    if lines:
        first = lines[0]
        if " - " in first:
            left, right = first.split(" - ", 1)
            artist = (left or "").strip() or None
            title = (right or "").strip() or None

    video_id = extract_video_id_from_text(body)

    return artist, title, video_id


# ─────────────────────────────────────────
# LrcLib helpers（YoutubeGlbot.py から必要部分だけ抽出）
# ─────────────────────────────────────────

LRC_LIB_BASE = "https://lrclib.net"


def _nf_lrc(s: str) -> str:
    import unicodedata as u

    t = u.normalize("NFKC", s or "")
    return re.sub(r"\s+", " ", t).strip().lower()


def lrclib_search(
    track_name: Optional[str] = None,
    artist_name: Optional[str] = None,
    q: Optional[str] = None,
) -> Optional[dict]:
    """
    LrcLib /api/search を叩いて最も良さそうな1件を返す。
    track_name / artist_name があればそれを優先してスコアリング。
    """
    params: Dict[str, str] = {}
    if track_name:
        params["track_name"] = track_name
    if artist_name:
        params["artist_name"] = artist_name
    if q:
        params["q"] = q

    if "q" not in params and "track_name" not in params:
        return None

    try:
        r = requests.get(f"{LRC_LIB_BASE}/api/search", params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[LrcLib] search error: {e}")
        return None

    if not isinstance(data, list) or not data:
        return None

    # メタがあれば簡易スコアリング
    if track_name or artist_name:

        def _score(rec: dict) -> int:
            s = 0
            if track_name and rec.get("trackName"):
                s += fuzz.ratio(_nf_lrc(track_name), _nf_lrc(rec["trackName"]))
            if artist_name and rec.get("artistName"):
                s += fuzz.ratio(_nf_lrc(artist_name), _nf_lrc(rec["artistName"]))
            return s

        return max(data, key=_score)

    return data[0]


LRC_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?]")


def parse_lrc(text: str) -> List[Dict]:
    cues: List[Dict[str, Any]] = []

    for line in text.splitlines():
        m = LRC_RE.match(line)
        if not m:
            continue
        mm, ss, ms = int(m[1]), int(m[2]), int(m[3] or 0)
        ts = mm * 60 + ss + ms / 1000
        body = line[m.end() :].strip()
        if not body:
            continue
        if cues and abs(cues[-1]["start"] - ts) < 1e-3:
            cues[-1]["text"] += "\n" + body
        else:
            cues.append({"start": ts, "end": ts + 4.0, "text": body})

    for i in range(len(cues) - 1):
        cues[i]["end"] = max(cues[i]["start"] + 0.1, cues[i + 1]["start"] - 0.05)
    return cues


BRACKET_LRC_RE = re.compile(r"^\s*\[(\d{1,2}):(\d{2})\.(\d{1,3})]")


def parse_bracket_lrc(text: str) -> Optional[List[Dict]]:
    import itertools

    cues: List[Dict[str, Any]] = []
    for line in text.splitlines():
        m = BRACKET_LRC_RE.match(line)
        if not m:
            continue
        mm, ss, cs = map(int, m.groups())
        cs = cs if cs >= 10 else cs * 10
        ts = mm * 60 + ss + cs / 100
        body = line[m.end() :].strip()
        if not body:
            continue
        cues.append({"start": ts, "end": ts + 4.0, "text": body})

    if not cues:
        return None

    for a, b in itertools.pairwise(cues):
        a["end"] = max(a["start"] + 0.1, b["start"] - 0.05)
    return cues


def lrclib_to_lyrics(rec: dict) -> Tuple[Optional[str], Optional[List[Dict]]]:
    """
    LrcLib レコード → (plain 歌詞, 同期歌詞 cues) に変換。
    ※ syncedLyrics が LRC 形式想定
    """
    plain = rec.get("plainLyrics") or None
    synced = rec.get("syncedLyrics") or None
    cues: Optional[List[Dict]] = None

    if synced:
        cues = parse_lrc(synced) or parse_bracket_lrc(synced)

    return plain, cues


# ─────────────────────────────────────────
# GitHub 歌詞保存（YoutubeGlbot.py と同じ形式）
# ─────────────────────────────────────────

FENCE_RE = re.compile(r"^```.*?$|^```$", re.M)


def _unfence(text: str) -> str:
    return re.sub(FENCE_RE, "", text).strip()


def _serialize_lyrics(plain: Optional[str], cues: Optional[List[Dict]]) -> str:
    if plain:
        return (plain or "").strip()
    if not cues:
        return ""
    out, prev_end = [], 0.0
    for e in cues:
        if e["start"] - prev_end >= 4.0 and out:
            out.append("")
        mm, ss = divmod(int(e["start"]), 60)
        cs = int(round((e["start"] - int(e["start"])) * 100))
        stamp = f"[{mm:02d}:{ss:02d}.{cs:02d}]"

        # ★ ここを分離する
        text = e["text"].replace("\n", " ").strip()
        out.append(f"{stamp} {text}")

        prev_end = e["end"]
    return "\n".join(out)



def github_save_lyrics(
    gh_user,
    repo_name: str,
    title: str,
    status: str,
    plain: Optional[str],
    cues: Optional[List[Dict]],
    source_code: Optional[int] = None,
    yt_full_title: Optional[str] = None,
    music_meta: Optional[Dict[str, Optional[str]]] = None,
) -> None:
    """
    YoutubeGlbot.py 内の github_save_lyrics とほぼ同じだが、
    gh_user を引数で受け取る形にしている。
    """
    body = _serialize_lyrics(plain, cues)

    artist = None
    track_name = None
    if music_meta:
        artist = (music_meta.get("artist") or "").strip() or None
        track_name = (music_meta.get("track") or "").strip() or None

    if artist and track_name:
        desc_main = f"{artist} – {track_name}"
    else:
        desc_main = (yt_full_title or "").strip() or title

    desc = desc_main

    if not body:
        status = "歌詞の登録なし"
        body = ""

    heading_lines = [
        f"# {title}",
        "",
        f"> **歌詞登録ステータス：{status}**",
    ]
    if source_code is not None:
        heading_lines += [
            ">",
            f"> **歌詞取得コード：{source_code}**",
        ]

    heading = "\n".join(heading_lines)

    lang = "lrc" if cues else ""
    content = f"{heading}\n\n```{lang}\n{body}\n```" if body else heading

    try:
        try:
            repo = gh_user.get_repo(repo_name)
            try:
                if any(f.name.lower() == "readme.md" for f in repo.get_contents("")):
                    # 既に README があれば何もしない（手動編集優先）
                    return
            except GithubException:
                pass
        except GithubException:
            # 新規作成
            repo = gh_user.create_repo(
                repo_name,
                description=desc,
                private=False,
                auto_init=False,
            )
            print(f"[GitHub] created repo {repo.full_name}")
            try:
                gh_user.add_to_watched(repo)
            except GithubException as e:
                print(f"[GitHub] watch error: {e}")
        else:
            # 既存リポジトリ: Description 更新
            try:
                if (repo.description or "") != desc:
                    repo.edit(description=desc)
            except GithubException as e:
                print(f"[GitHub] update description error: {e}")

        try:
            repo.create_file("README.md", "Add lyrics", content, branch="main")
            print(f"[GitHub] added lyrics to {repo_name}")

            # 歌詞なしなら Star を付ける（GLBot と揃える）
            if status == "歌詞の登録なし":
                try:
                    repo.add_star()
                except Exception as e:
                    print(f"[GitHub] star error: {e}")

            if source_code is not None:
                code_name = str(source_code)
                content_code = code_name + "\n"
                # 他のコードファイル(1/2/3)があれば削除
                for n in ("1", "2", "3"):
                    if n == code_name:
                        continue
                    try:
                        old = repo.get_contents(n)
                        repo.delete_file(
                            n,
                            "Remove old lyrics source flag",
                            old.sha,
                            branch="main",
                        )
                    except GithubException:
                        pass
                try:
                    f = repo.get_contents(code_name)
                    if (
                        f.decoded_content.decode("utf-8", "ignore")
                        != content_code
                    ):
                        repo.update_file(
                            code_name,
                            "Set lyrics source",
                            content_code,
                            f.sha,
                            branch="main",
                        )
                except GithubException:
                    repo.create_file(
                        code_name,
                        "Set lyrics source",
                        content_code,
                        branch="main",
                    )
                print(f"[GitHub] source flag {code_name} written for {repo_name}")

        except GithubException as e:
            print(f"[GitHub] save error: {e}")

    except GithubException as e:
        print(f"[GitHub] save error: {e}")
    except Exception as e:
        print(f"[GitHub] save error: {e}")


# ─────────────────────────────────────────
# Issue へのコメント
# ─────────────────────────────────────────

def build_issue_comment(
    artist: Optional[str],
    title: Optional[str],
    video_id: Optional[str],
    status: str,
    src_label: str,
    msg: str,
) -> str:
    lines = []
    lines.append("自動歌詞登録の結果をお知らせします :robot:\n")

    lines.append("### 解析結果")
    lines.append(f"- アーティスト: **{artist or '(未入力)'}**")
    lines.append(f"- 楽曲名: **{title or '(未入力)'}**")
    lines.append(f"- 動画 ID: `{video_id or '(不明)'}`")

    lines.append("\n### 歌詞登録結果")
    lines.append(f"- ステータス: **{status}**")
    lines.append(f"- 取得元: **{src_label}**")
    lines.append("")
    lines.append(msg)

    lines.append("\n---")
    lines.append(
        "※ このコメントは GitHub Actions の自動処理で追加されています。"
        " / フォーマット不備などでうまく登録できない場合があります。"
    )
    return "\n".join(lines)


def comment_to_issue(repo, issue_number: int, body: str) -> None:
    issue = repo.get_issue(number=issue_number)
    issue.create_comment(body)


# ─────────────────────────────────────────
# メイン処理
# ─────────────────────────────────────────

def main() -> None:
    token = os.environ.get("GITHUB_TOKEN")
    repo_name = os.environ.get("GITHUB_REPOSITORY")

    if not token:
        raise RuntimeError("GITHUB_TOKEN が設定されていません")
    if not repo_name:
        raise RuntimeError("GITHUB_REPOSITORY が設定されていません")

    gh = Github(auth=Auth.Token(token))
    actions_repo = gh.get_repo(repo_name)
    gh_user = gh.get_user()  # neiron-discord を想定

    event = load_github_event()
    action = event.get("action")
    issue_data = event.get("issue")

    if not issue_data:
        print("issue イベントではないので何もしません")
        return

    issue_number = issue_data["number"]
    issue_body = issue_data.get("body") or ""

    print(f"action={action}, issue_number={issue_number}")

    if action not in {"opened", "edited"}:
        print("opened/edited 以外のアクションなのでスキップします")
        return

    artist, title, video_id = parse_issue_body(issue_body)
    print(f"parsed: artist={artist}, title={title}, video_id={video_id}")

    if not video_id:
        body = build_issue_comment(
            artist,
            title,
            video_id,
            status="失敗",
            src_label="(未実行)",
            msg="本文から動画IDを特定できませんでした。\n"
            "1行目に `アーティスト - 曲名`、本文のどこかに YouTube URL または 動画ID を含めてください。",
        )
        comment_to_issue(actions_repo, issue_number, body)
        return

    # LrcLib 検索
    status = "歌詞の登録なし"
    src_label = "LrcLib"
    msg = ""

    rec = None
    try:
        # track_name だけでも良いが、artist もあればスコアが上がる
        if title or artist:
            print(f"[LrcLib] search track_name={title!r}, artist_name={artist!r}")
        else:
            print(f"[LrcLib] search q={video_id!r}")

        rec = lrclib_search(
            track_name=title or None,
            artist_name=artist or None,
            q=None if (title or artist) else video_id,
        )
    except Exception as e:
        print(f"[LrcLib] unexpected error: {e}")

    plain: Optional[str] = None
    cues: Optional[List[Dict]] = None

    if rec:
        plain, cues = lrclib_to_lyrics(rec)
        if cues:
            status = "Auto/同期あり"
        elif plain:
            status = "Auto/同期なし"
        else:
            status = "歌詞の登録なし"
        msg = f"LrcLib から歌詞情報を取得しました。（track={rec.get('trackName')!r}, artist={rec.get('artistName')!r}）"
    else:
        status = "歌詞の登録なし"
        msg = "LrcLib で該当する歌詞を見つけられませんでした。手動で登録してください。"

    # GitHub へ歌詞登録（動画IDリポジトリ）
    try:
        music_meta = {
            "artist": artist or (rec.get("artistName") if rec else None),
            "track": title or (rec.get("trackName") if rec else None),
            "album": rec.get("albumName") if rec else None,
            "release_year": None,
        }
    except Exception:
        music_meta = {"artist": artist, "track": title, "album": None, "release_year": None}

    try:
        github_save_lyrics(
            gh_user=gh_user,
            repo_name=video_id,
            title=title or (rec.get("trackName") if rec else video_id),
            status=status,
            plain=plain,
            cues=cues,
            source_code=1,  # 1=LrcLib, という意味で GLBot と合わせる
            yt_full_title=None,
            music_meta=music_meta,
        )
    except Exception as e:
        print(f"[GitHub] save_lyrics error: {e}")
        msg += f"\n\nただし GitHub への保存中にエラーが発生しました: `{e}`"

    # Issue にコメント
    body = build_issue_comment(
        artist=artist,
        title=title,
        video_id=video_id,
        status=status,
        src_label=src_label,
        msg=msg,
    )
    comment_to_issue(actions_repo, issue_number, body)

    print("処理完了")


if __name__ == "__main__":
    main()
