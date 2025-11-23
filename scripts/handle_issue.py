#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Issue から
  - アーティスト
  - 楽曲名
  - YouTube 動画ID
を読み取り、
外部の歌詞データベースから歌詞を取得して

  - GitHub 上に <動画ID> リポジトリを作成（or 更新）
  - README.md に歌詞を書き込む
  - 処理結果を Issue にコメントする

2025/11/23
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from github import Github, Auth, GithubException

# ------------- パス関連 -------------

ROOT_DIR = Path(__file__).resolve().parent.parent

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


# ---------- Issue 本文パース ----------

ISSUE_VIDEO_ID_PATTERNS = [
    r"^動画\s*ID\s*[:：]\s*([0-9A-Za-z_-]{8,})$",
    r"(?:youtube\.com/watch\?v=|youtu\.be/)([0-9A-Za-z_-]{8,})",
]

# 1行目が「アーティスト - タイトル」想定
def parse_issue_body(body: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    フォーマット:
        1行目: "アーティスト - タイトル"
        2行目以降: 任意。YouTube の URL / 動画ID 行 があれば video_id を取得する。

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
        if " - " in line:
            left, right = line.split(" - ", 1)
            artist = left.strip() or None
            title = right.strip() or None
            break

    # ---- 2. 本文全体から YouTube の video_id を取得 ----
    video_id = extract_video_id_from_text(body)

    return artist, title, video_id


def extract_video_id_from_text(text: str) -> Optional[str]:
    """
    本文中から YouTube 動画ID らしき文字列を探す。
      - 「動画ID: xxxx」
      - YouTube URL (youtube.com/watch?v= / youtu.be/)
    """
    for pat in ISSUE_VIDEO_ID_PATTERNS:
        m = re.search(pat, text, flags=re.MULTILINE)
        if m:
            vid = m.group(1).strip()
            if vid:
                return vid
    return None


# ---------- 歌詞フォーマット（LRC 系） ----------

from typing import TypedDict


class Cue(TypedDict):
    start: float
    end: float
    text: str


LRC_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?]")


def parse_lrc(text: str) -> List[Cue]:
    """
    [mm:ss.xx] な LRC をざっくりパース。
    """
    cues: List[Cue] = []
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
    # end を次の start に合わせて調整
    for i in range(len(cues) - 1):
        nxt = cues[i + 1]
        cues[i]["end"] = max(cues[i]["start"] + 0.1, nxt["start"] - 0.05)
    return cues


BRACKET_LRC_RE = re.compile(r"^\s*\[(\d{1,2}):(\d{2})\.(\d{1,3})]")


def parse_bracket_lrc(text: str) -> Optional[List[Cue]]:
    """
    [mm:ss.cc] （小数2桁など）形式へのフォールバック。
    """
    import itertools

    cues: List[Cue] = []
    for line in text.splitlines():
        m = BRACKET_LRC_RE.match(line)
        if not m:
            continue
        mm, ss, cs = map(int, m.groups())
        if cs < 10:
            cs *= 10
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


# ---------- 外部 歌詞 API (サービス名はコメントに出さない) ----------

LRC_LIB_BASE = "https://lrclib.net"


def _nf_lrc(s: str) -> str:
    import unicodedata as u

    t = u.normalize("NFKC", s or "")
    return re.sub(r"\s+", " ", t).strip().lower()


def lrclib_search(
    track_name: Optional[str] = None,
    artist_name: Optional[str] = None,
) -> Optional[dict]:
    """
    外部の歌詞APIを叩いて、最も良さそうな 1 件を返す。
    ※ track_name は必須。artist_name があればスコアリングで優先。
    """
    if not track_name:
        return None

    params: Dict[str, str] = {"track_name": track_name}
    if artist_name:
        params["artist_name"] = artist_name

    try:
        r = requests.get(f"{LRC_LIB_BASE}/api/search", params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[LyricsAPI] search error: {e}")
        return None

    if not isinstance(data, list) or not data:
        return None

    if artist_name:
        # 簡易スコアリング
        from rapidfuzz import fuzz

        def _score(rec: dict) -> int:
            s = 0
            if rec.get("trackName"):
                s += fuzz.ratio(_nf_lrc(track_name), _nf_lrc(rec["trackName"]))
            if rec.get("artistName") and artist_name:
                s += fuzz.ratio(_nf_lrc(artist_name), _nf_lrc(rec["artistName"]))
            return s

        best = max(data, key=_score)
        return best

    return data[0]


def lrclib_to_lyrics(rec: dict) -> Tuple[Optional[str], Optional[List[Cue]]]:
    """
    レコード → (plain 歌詞, 同期歌詞 cues) に変換。
    syncedLyrics は LRC 形式前提。
    """
    plain = rec.get("plainLyrics") or None
    synced = rec.get("syncedLyrics") or None
    cues: Optional[List[Cue]] = None

    if synced:
        cues = parse_lrc(synced) or parse_bracket_lrc(synced)

    return plain, cues


# ---------- GitHub 歌詞リポジトリ操作 ----------

FENCE_RE = re.compile(r"^```.*?$|^```$", re.M)


def _unfence(text: str) -> str:
    return re.sub(FENCE_RE, "", text).strip()


def _serialize_lyrics(plain: Optional[str], cues: Optional[List[Cue]]) -> str:
    """
    plain （そのまま） or cues → テキスト化。
    """
    if plain:
        return plain.strip()
    if not cues:
        return ""
    out: List[str] = []
    prev_end = 0.0
    for e in cues:
        if e["start"] - prev_end >= 4.0 and out:
            out.append("")
        mm, ss = divmod(int(e["start"]), 60)
        cs = int(round((e["start"] - int(e["start"])) * 100))
        stamp = f"[{mm:02d}:{ss:02d}.{cs:02d}]"
        # ← f-string の中で \n を直接書くとエスケープが面倒なので一旦変数化
        text_line = e["text"].replace("\n", " ").strip()
        out.append(f"{stamp} {text_line}")
        prev_end = e["end"]
    return "\n".join(out)


def github_save_lyrics(
    gh_user,
    repo_name: str,
    title: str,
    status: str,
    plain: Optional[str],
    cues: Optional[List[Cue]],
    source_code: Optional[int] = None,
    track_name: Optional[str] = None,
    artist_name: Optional[str] = None,
) -> str:
    """
    <repo_name> リポジトリ（1動画=1リポジトリ）に README.md を作成/更新。
    戻り値: リポジトリ URL
    """
    body = _serialize_lyrics(plain, cues)

    # Description 用タイトル
    if artist_name and track_name:
        desc_main = f"{artist_name} – {track_name}"
    else:
        desc_main = title

    desc = desc_main

    if not body:
        status = "歌詞の登録なし"
        body = ""

    # 見出し（ステータス + 取得コード）
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
            # 既存なら Description だけ合わせておく
            try:
                if (repo.description or "") != desc:
                    repo.edit(description=desc)
            except GithubException as e:
                print(f"[GitHub] update description error: {e}")
        except GithubException:
            # 新規作成
            repo = gh_user.create_repo(
                repo_name,
                description=desc,
                private=False,
                auto_init=False,
            )
            print(f"[GitHub] created repo {repo.full_name}")

        # README を作成（すでにあれば何もしない = 手動編集優先）
        try:
            contents = repo.get_contents("")
            has_readme = any(f.name.lower() == "readme.md" for f in contents)
        except GithubException:
            has_readme = False

        if not has_readme:
            repo.create_file("README.md", "Add lyrics", content, branch="main")
            print(f"[GitHub] added lyrics to {repo_name}")
        else:
            # 自動で上書きはしない
            print(f"[GitHub] README.md already exists in {repo_name}, skipped auto-write")

        # 歌詞なしのときスターを付ける（お好みで）
        if status == "歌詞の登録なし":
            try:
                repo.add_star()
            except Exception as e:
                print(f"[GitHub] star error: {e}")

        # 歌詞取得コード用の数値ファイル (1/2/3...) を書き込み
        if source_code is not None:
            code_name = str(source_code)
            content_code = code_name + "\n"
            # 他コードファイルを削除
            for n in ("1", "2", "3"):
                if n == code_name:
                    continue
                try:
                    old = repo.get_contents(n)
                    repo.delete_file(
                        n, "Remove old lyrics source flag", old.sha, branch="main"
                    )
                except GithubException:
                    pass
            try:
                f = repo.get_contents(code_name)
                if f.decoded_content.decode("utf-8", "ignore") != content_code:
                    repo.update_file(
                        code_name,
                        "Set lyrics source",
                        content_code,
                        f.sha,
                        branch="main",
                    )
            except GithubException:
                repo.create_file(
                    code_name, "Set lyrics source", content_code, branch="main"
                )

        return repo.html_url

    except GithubException as e:
        print(f"[GitHub] save error: {e}")
        raise


# ---------- Issue コメント本文 ----------


def build_comment_body(
    artist: Optional[str],
    title: Optional[str],
    video_id: Optional[str],
    status: str,
    source_label: str,
    repo_url: Optional[str],
    detail: Optional[str],
) -> str:
    """
    Issue へ投稿するコメント本文。
    ※ 外部サービス名は出さない。
    """
    lines: List[str] = []
    lines.append("自動歌詞登録の結果をお知らせします 🤖\n")

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
    else:
        lines.append("- 動画 ID: (取得できませんでした)")

    lines.append("\n### 歌詞登録結果")
    lines.append(f"- ステータス: **{status}**")
    lines.append(f"- 取得元: {source_label}")
    if repo_url:
        lines.append(f"- 歌詞リポジトリ: {repo_url}")

    if detail:
        lines.append(f"- 詳細: {detail}")

    lines.append(
        "\n※ このコメントは GitHub Actions の自動処理で追加されています。"
        " / フォーマット不備などでうまく登録できない場合があります。"
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
    gh_user = gh.get_user()

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

    # 動画IDが取れないと、歌詞リポジトリ名が決まらないのでここで終了
    if not video_id:
        msg = (
            "自動歌詞登録の結果をお知らせします 🤖\n\n"
            "動画 ID が本文から取得できませんでした。\n\n"
            "- YouTube の URL を本文に含める\n"
            "- もしくは `動画ID: <ID>` の形式で書く\n\n"
            "のどちらかで動画IDを指定してください。"
        )
        comment_to_issue(repo, issue_number, msg)
        print("動画ID なしのため終了しました。")
        return

    # ---- 歌詞検索（外部API） ----
    status = "歌詞の登録なし"
    source_label = "（該当なし）"
    repo_url: Optional[str] = None
    detail: Optional[str] = None

    plain: Optional[str] = None
    cues: Optional[List[Cue]] = None

    # track_name には楽曲名を想定
    track_name = title or ""
    artist_name = artist or ""

    try:
        rec = lrclib_search(track_name=track_name, artist_name=artist_name)
    except Exception as e:
        rec = None
        detail = f"歌詞取得中にエラーが発生しました: {e}"

    if rec:
        plain, cues = lrclib_to_lyrics(rec)

        # どんな情報が返ってきたかだけ detail に書く（サービス名は出さない）
        t = rec.get("trackName")
        a = rec.get("artistName")
        if t or a:
            detail = f"外部歌詞データベースから曲情報を取得しました（track='{t or ''}', artist='{a or ''}'）。"

        if cues:
            status = "Auto/同期あり"
            source_label = "外部歌詞データベース（同期歌詞）"
        elif plain:
            status = "Auto/同期なし"
            source_label = "外部歌詞データベース（テキスト歌詞）"
        else:
            status = "歌詞の登録なし"
            source_label = "外部歌詞データベース（該当なし）"

        # メタ情報
        track_meta = rec.get("trackName") or title or video_id
        artist_meta = rec.get("artistName") or artist or None

        # 歌詞を GitHub リポジトリへ保存（source_code=1 は「外部歌詞DB」扱い）
        repo_url = github_save_lyrics(
            gh_user=gh_user,
            repo_name=video_id,
            title=track_meta or video_id,
            status=status,
            plain=plain,
            cues=cues,
            source_code=1,
            track_name=track_meta,
            artist_name=artist_meta,
        )
    else:
        # レコードが一切見つからなかった場合も「空の歌詞リポジトリ」は作る
        try:
            repo_url = github_save_lyrics(
                gh_user=gh_user,
                repo_name=video_id,
                title=title or video_id,
                status="歌詞の登録なし",
                plain=None,
                cues=None,
                source_code=None,
                track_name=title or None,
                artist_name=artist or None,
            )
            status = "歌詞の登録なし"
            source_label = "外部歌詞データベース（該当なし）"
            if not detail:
                detail = "外部歌詞データベースから該当する歌詞を見つけられませんでした。"
        except Exception as e:
            detail = f"歌詞リポジトリの作成に失敗しました: {e}"
            repo_url = None

    # ---- 結果を Issue にコメント ----
    comment_body = build_comment_body(
        artist=artist,
        title=title,
        video_id=video_id,
        status=status,
        source_label=source_label,
        repo_url=repo_url,
        detail=detail,
    )
    comment_to_issue(repo, issue_number, comment_body)

    print("処理が完了しました。")


if __name__ == "__main__":
    main()
