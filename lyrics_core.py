# lyrics_core.py
# GitHub 歌詞リポジトリ + LrcLib + PetitLyrics + YouTube メタまわりの共通モジュール

from __future__ import annotations

import os
import re
import base64
import itertools
import unicodedata
from typing import Optional, List, Dict, Tuple

import requests
from bs4 import BeautifulSoup
from rapidfuzz import fuzz
from github import Github, GithubException

# ─────────────────────────────────────────
# GitHub クライアント (歌詞保存用)
# ─────────────────────────────────────────

_TOKEN = os.getenv("LYRICS_GH_TOKEN") or os.getenv("GITHUB_TOKEN")
if not _TOKEN:
    raise RuntimeError("lyrics_core: LYRICS_GH_TOKEN か GITHUB_TOKEN を環境変数で渡してください")

_GH = Github(_TOKEN)
_GH_USER = _GH.get_user()

# ─────────────────────────────────────────
# GitHub 歌詞 helpers
# ─────────────────────────────────────────

FENCE_RE = re.compile(r"^```.*?$|^```$", re.M)


def _unfence(text: str) -> str:
    """``` で囲まれている場合は外す。"""
    return re.sub(FENCE_RE, "", text).strip()


def github_get_lyrics(repo_name: str) -> Optional[str]:
    """
    PAT のユーザー直下にある <repo_name> リポジトリの README.md から歌詞テキストを取得する。
    見つからない場合は None。
    """
    try:
        repo = _GH_USER.get_repo(repo_name)
        readme = repo.get_readme()
        raw = readme.decoded_content.decode("utf-8", "ignore")
        return _unfence(raw) or None
    except GithubException:
        return None
    except Exception:
        return None


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

        # ← バックスラッシュを含む処理は f-string の外でやる
        text = (e.get("text") or "").replace("\n", " ").strip()
        out.append(f"{stamp} {text}")

        prev_end = e["end"]

    return "\n".join(out)



def github_save_lyrics(
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
    GLBot 互換フォーマットで lyrics repo を作成/保存する。

    - repo_name … 通常 YouTube の video ID
    - README.md に 歌詞/ステータス を書き込み
    - 既に README.md がある場合は「手動編集優先」として何もしない
    - source_code … 1=LrcLib, 2=YouTube 字幕, 3=PetitLyrics など
    """
    body = _serialize_lyrics(plain, cues)

    artist = None
    track_name = None
    if music_meta:
        artist = (music_meta.get("artist") or "").strip()
        track_name = (music_meta.get("track") or "").strip()

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
        # リポジトリ取得 or 作成
        try:
            repo = _GH_USER.get_repo(repo_name)
            # 既に README があるなら何もしない
            try:
                if any(f.name.lower() == "readme.md" for f in repo.get_contents("")):
                    return
            except GithubException:
                pass
        except GithubException:
            repo = _GH_USER.create_repo(
                repo_name, description=desc, private=False, auto_init=False
            )

        # Description 更新
        try:
            if (repo.description or "") != desc:
                repo.edit(description=desc)
        except GithubException:
            pass

        # README 作成（※存在チェック済みなので create_file 一択）
        repo.create_file("README.md", "Add lyrics", content, branch="main")

        # 取得コード用の数値ファイル
        if source_code is not None:
            code_name = str(source_code)
            content_code = code_name + "\n"

            # 他のコードファイルを削除
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

    except Exception:
        # 失敗しても上には例外を投げない（Actions 側で処理を止めたくない）
        return


# ─────────────────────────────────────────
# LRC / bracket LRC パーサ（LrcLib 用）
# ─────────────────────────────────────────

LRC_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?]")


def parse_lrc(text: str) -> List[Dict]:
    cues: List[Dict] = []
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


LRC_BRACKET = re.compile(r"^\s*\[(\d{1,2}):(\d{2})\.(\d{1,3})]")


def parse_bracket_lrc(text: str) -> Optional[List[Dict]]:
    cues: List[Dict] = []
    for line in text.splitlines():
        m = LRC_BRACKET.match(line)
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


# ─────────────────────────────────────────
# LrcLib helpers
# ─────────────────────────────────────────

LRC_LIB_BASE = "https://lrclib.net"


def _nf_lrc(s: str) -> str:
    t = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"\s+", " ", t).strip().lower()


def lrclib_search(
    track_name: Optional[str] = None,
    artist_name: Optional[str] = None,
    q: Optional[str] = None,
) -> Optional[dict]:
    """
    LrcLib /api/search を叩いて最も良さそうな 1 件を返す。
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
    except Exception:
        return None

    if not isinstance(data, list) or not data:
        return None

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


def lrclib_to_lyrics(rec: dict) -> Tuple[Optional[str], Optional[List[Dict]]]:
    """
    LrcLib レコード → (plain 歌詞, 同期歌詞 cues)
    """
    plain = rec.get("plainLyrics") or None
    synced = rec.get("syncedLyrics") or None
    cues: Optional[List[Dict]] = None

    if synced:
        cues = parse_lrc(synced) or parse_bracket_lrc(synced)

    return plain, cues


# ─────────────────────────────────────────
# PetitLyrics helpers（曲名オンリー検索）
# ─────────────────────────────────────────

PL_BASE = "https://petitlyrics.com"
HEADERS = {"User-Agent": "Mozilla/5.0"}

TITLE_TRIM_PAT = re.compile(
    r"""(?ix)
        (\s*[\(\[]\s*
            (official(?:\s*music)?\s*video|mv|lyric(?:s|\s*video)?|audio|teaser|short|pv|
             full|ver\.?|version|remix|edit|live|acoustic|prod\.?.*?|performance|
             music\s*video|color\s*coded|dance\s*practice|practice|choreo(?:graphy)?|
             official\s*audio|visualizer|sped\s*up|slowed\s*reverb)
        \s*[\)\]]\s*)$
    """
)
JP_CHAR = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")
SEP_ANY = re.compile(r"\s*(?:-+|–|—|/|／|\||｜|•|・|~|〜)\s*")


def _clean_title_for_song(title: str) -> str:
    t = (title or "").strip()
    for _ in range(5):
        nt = TITLE_TRIM_PAT.sub("", t).strip()
        if nt == t:
            break
        t = nt
    return re.sub(r"\s{2,}", " ", t)


def _pick_japanese_segment_safe(text: str) -> Optional[str]:
    if not text:
        return None
    m = re.findall(
        r"[\u3040-\u30ff\u4e00-\u9fff]+(?:\s*[\u3040-\u30ff\u4e00-\u9fff]+)*", text
    )
    if not m:
        return None
    cand = max(m, key=len)
    cand = re.sub(r"\s*[\(\[【（〈「『<].*?[\)】］）〉』」>]\s*$", "", cand).strip()
    return cand or None


def song_only(text: str) -> str:
    """
    '米津玄師 - かいじゅうのマーチ' → 'かいじゅうのマーチ'
    """
    if not text:
        return ""
    t = _clean_title_for_song(text)
    jp = _pick_japanese_segment_safe(t)
    tgt = jp or t
    parts = [p.strip() for p in SEP_ANY.split(tgt) if p.strip()]
    return parts[-1] if len(parts) >= 2 else tgt


def _nf(s: str) -> str:
    t = unicodedata.normalize("NFKC", s or "").lower()
    return re.sub(r"\s+", " ", t).strip()


def pl_search(title: str) -> Optional[int]:
    try:
        r = requests.get(
            f"{PL_BASE}/search_lyrics",
            params={"title": title},
            headers=HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        a = BeautifulSoup(r.text, "html.parser").find(
            "a", href=re.compile(r"^/lyrics/\d+")
        )
        if not a:
            return None
        m = re.match(r"^/lyrics/(\d+)", a["href"])
        return int(m.group(1)) if m else None
    except Exception:
        return None


def pl_search_fuzzy(title: str, *, score_cutoff: int = 85) -> Optional[int]:
    try:
        r = requests.get(
            f"{PL_BASE}/search_lyrics",
            params={"title": title},
            headers=HEADERS,
            timeout=10,
        )
        r.raise_for_status()
    except Exception:
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    cand = []
    for a in soup.find_all("a", href=re.compile(r"^/lyrics/\d+")):
        m = re.match(r"^/lyrics/(\d+)", a.get("href", ""))
        if not m:
            continue
        rid = int(m.group(1))
        row = (a.find_parent() or a).get_text(" ", strip=True)
        cand.append((rid, row))
    if not cand:
        return None

    q = _nf(title)
    best_id, best_score = None, -1.0
    for rid, row in cand:
        left = re.split(
            r"\s*(?:/|／|\||｜| - |–|—)\s*", _nf(row), 1
        )[0]  # タイトル側だけ使う
        score = fuzz.token_set_ratio(q, left)
        if score > best_score:
            best_score, best_id = score, rid

    return best_id if best_score >= score_cutoff else None


def pl_fetch(lyid: int) -> Optional[str]:
    try:
        sess = requests.Session()
        page = sess.get(f"{PL_BASE}/lyrics/{lyid}", headers=HEADERS, timeout=10)
        page.raise_for_status()
        js = sess.get(f"{PL_BASE}/lib/pl-lib.js", headers=HEADERS, timeout=10)
        js.raise_for_status()
        m = re.search(
            r"setRequestHeader\('X-CSRF-Token', '([0-9a-f]+)'\)", js.text
        )
        if not m:
            return None
        token = m.group(1)
        ajax = sess.post(
            f"{PL_BASE}/com/get_lyrics.ajax",
            data={"lyrics_id": lyid},
            headers={
                **HEADERS,
                "X-CSRF-Token": token,
                "X-Requested-With": "XMLHttpRequest",
                "Referer": page.url,
                "Origin": PL_BASE,
            },
            timeout=10,
        )
        ajax.raise_for_status()
        parts = ajax.json()
        chunks = []
        for p in parts:
            html = base64.b64decode(p["lyrics"]).decode("utf-8", "ignore")
            html = re.sub(r"<br\s*/?>", "\n", html)
            chunks.append(BeautifulSoup(html, "html.parser").get_text().strip())
        return "\n".join(chunks).strip() or None
    except Exception:
        return None


def pl_search_smart(raw_title: str) -> Optional[int]:
    q = song_only(raw_title)
    if not q:
        return None
    pid = pl_search(q)
    if pid:
        return pid
    return pl_search_fuzzy(q, score_cutoff=82)


# ─────────────────────────────────────────
# YouTube メタ推定 helpers
# ─────────────────────────────────────────

def _nf2(x: str) -> str:
    return re.sub(
        r"\s+",
        " ",
        unicodedata.normalize("NFKC", (x or "")).strip(),
    )


def _clean_channel_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    n = _nf2(name)
    n = re.sub(r"\s*[-–—]\s*topic$", "", n, flags=re.I)
    n = re.sub(
        r"(?i)\b(official|offical|オフィシャル|公式|vevo)\b", "", n
    )
    n = re.sub(r"\s+", " ", n).strip(" -–—|｜・")
    return n or None


SEP_META = re.compile(
    r"\s*(?:-|–|—|/|／|\||｜|:|：|•|・|~|〜)\s*"
)


def _guess_from_title_and_channel(
    title: str, channel: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    if not title:
        return (None, None)
    t = _nf2(title)
    t = re.sub(
        r"[\(\[【（〈「『<].*?[\)】］）〉』」>]\s*$", "", t
    ).strip()
    parts = [p for p in SEP_META.split(t) if p]
    cand_artist, cand_track = None, None

    if len(parts) >= 2:
        left, right = parts[0], parts[-1]
        cand_track = right
        cand_artist = left

    jp = re.findall(
        r"[\u3040-\u30ff\u4e00-\u9fff]+(?:\s*[\u3040-\u30ff\u4e00-\u9fff]+)*",
        t,
    )
    jp_long = max(jp, key=len) if jp else None
    if not cand_track and jp_long:
        cand_track = re.sub(
            r"[\s　]*(?:/|／|\||｜|-|–|—|:|：).*$", "", jp_long
        ).strip()

    ch = _clean_channel_name(channel or "")
    if ch and cand_artist:
        if (
            fuzz.token_set_ratio(
                _nf2(ch).lower(), _nf2(cand_artist).lower()
            )
            < 70
        ):
            if (
                len(parts) >= 2
                and fuzz.token_set_ratio(
                    _nf2(ch).lower(), _nf2(parts[-1]).lower()
                )
                >= 70
            ):
                cand_artist, cand_track = parts[-1], parts[0]

    if ch and not cand_artist:
        cand_artist = ch

    def _trim_noise(s: Optional[str]) -> Optional[str]:
        if not s:
            return None
        s = re.sub(
            r"(?i)\b(official(?: music)? video|mv|lyric(?:s|\s*video)?|audio|teaser|short|pv|full|ver\.?|version|remix|edit|live|acoustic|prod\.?.*?|performance|visualizer|sped\s*up|slowed\s*reverb)\b",
            "",
            s,
        )
        s = re.sub(r"\s+", " ", s).strip(" -–—/／|｜•・~〜")
        return s or None

    return _trim_noise(cand_artist), _trim_noise(cand_track)


def _parse_provided_block(
    desc: Optional[str],
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    if not desc:
        return (None, None, None)
    L = [_nf2(l) for l in desc.splitlines() if _nf2(l)]
    if not L:
        return (None, None, None)

    for i, line in enumerate(L):
        if "provided to youtube by" in line.lower():
            for j in range(i + 1, min(i + 6, len(L))):
                if " · " in L[j]:
                    left, right = [s.strip() for s in L[j].split(" · ", 1)]
                    if left and right:
                        return (right, left, None)

    SONG_KEYS = {"song", "楽曲", "曲", "タイトル"}
    ARTIST_KEYS = {"artist", "アーティスト"}
    ALBUM_KEYS = {"album", "アルバム"}

    def pick_after(keys: set[str]) -> Optional[str]:
        for k in range(len(L) - 1):
            if L[k].lower() in keys:
                v = L[k + 1].strip()
                if not re.search(
                    r"(licensed to youtube|auto-generated by youtube)",
                    v,
                    flags=re.I,
                ):
                    return v
        return None

    track = pick_after(SONG_KEYS)
    artist = pick_after(ARTIST_KEYS)
    album = pick_after(ALBUM_KEYS)
    return (artist, track, album)


def _guess_meta_when_missing(info: dict) -> tuple[Optional[str], Optional[str], Optional[str]]:
    a, t, al = _parse_provided_block(info.get("description"))
    if a or t:
        return a, t, al

    a2, t2 = _guess_from_title_and_channel(
        info.get("title") or "", info.get("uploader") or info.get("channel")
    )
    if a2 or t2:
        return a2, t2, None

    chs = info.get("chapters") or []
    if chs:
        first = chs[0].get("title") if isinstance(chs[0], dict) else None
        if first and len(first) <= 80:
            return (None, _nf2(first), None)

    return (None, None, None)


BAD_ARTISTS = {
    "topic",
    "various artists",
    "auto-generated by youtube",
    "unknown artist",
    "v.a.",
}


def _canon_music_meta(info: dict) -> tuple[Optional[str], dict]:
    """
    yt_dlp の info dict から (表示用タイトル, 正規化メタ) を返す。
    """
    artist = info.get("artist")
    if isinstance(artist, list):
        artist = ", ".join(a for a in artist if a)
    track = info.get("track")
    album = info.get("album")
    year = info.get("release_year")

    if not (artist and track):
        a2, t2, al2 = _guess_meta_when_missing(info or {})
        artist = artist or a2
        track = track or t2
        album = album or al2

    def _nf(x: str) -> str:
        return re.sub(
            r"\s+",
            " ",
            unicodedata.normalize("NFKC", (x or "")).lower(),
        ).strip()

    if artist and _nf(artist) in BAD_ARTISTS:
        artist = None

    display = f"{artist} – {track}" if (artist and track) else (track or None)
    return display, {
        "artist": artist,
        "track": track,
        "album": album,
        "release_year": year,
    }


def _display_title_for(info: dict) -> str:
    """
    表示に使うタイトル。
    公式メタがあればそちらを優先し、それがなければ YouTube タイトル。
    """
    display, meta = _canon_music_meta(info or {})
    if meta:
        info["_music_meta"] = meta
    return display or (info.get("title") or "(no title)")
