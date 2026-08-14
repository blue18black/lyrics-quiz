"""
歌詞クイズ: Deezer公開APIを使った曲発見・ランキング。

YouTube Musicのカタログにはミュージックビデオ(MV)やライブ映像が音源と同列に
混ざっており、アルバム/シングル一覧を辿るだけでは全曲を正確に拾えない上、
歌詞の登録有無もアーティストによって偏りがある。Deezerは音楽ストリーミング
サービスの公開API(認証不要・無料)で、動画を一切扱わない音声のみのカタログの
ため曲の取りこぼしが構造的に少なく、各トラックに人気度(rank)が最初から
付与されているためTop25/Top50のランキングも作れる。歌詞データそのものは
Deezer/iTunesどちらの公開APIにも存在しないため、ここで確定した曲一覧を
YouTube Music側で曲名+収録アルバム名/演奏者名で検索してマッチングし、
歌詞を取得する(app.py側のブリッジ処理)。

このモジュールの大部分は、姉妹プロジェクト「イントロドン2」
(../introdon2/deezer_service.py)で実戦投入済みのアーティスト解決・重複排除・
Live/インスト除外ロジックを移植したもの。
"""
import re
import threading
import time
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import requests

_DEEZER_API_BASE = "https://api.deezer.com"
_ITUNES_SEARCH_API_BASE = "https://itunes.apple.com/search"
_ITUNES_LOOKUP_API_BASE = "https://itunes.apple.com/lookup"

_session = requests.Session()
_session.headers.update({"User-Agent": "lyric-quiz/1.0"})


def _get(base, path, params=None, retries=2):
    """Deezer/iTunesどちらも短時間に集中アクセスするとレート制限で一時的に
    失敗することがあるため、間を置いて再試行する。レート制限時はHTTPレベルの
    例外ではなく{"error": ...}という成功扱いのJSON応答が返ってくることが
    あるため、エラー応答も再試行対象に含める。"""
    url = f"{base}{path}"
    for attempt in range(retries + 1):
        try:
            res = _session.get(url, params=params, timeout=10)
            data = res.json()
        except Exception:
            data = None
        if data is not None and not (isinstance(data, dict) and data.get("error")):
            return data
        if attempt < retries:
            time.sleep(0.4)
    return None


# ---- タイトル正規化・版表記フィルタ ----

UNWANTED_VERSION_KEYWORDS = [
    "live", "ライブ", "instrumental", "インスト", "off vocal", "offvocal",
    "オフボーカル", "karaoke", "カラオケ", "backing track", "acapella", "a cappella",
    "アカペラ", "less vocal", "lessvocal", "interlude", "インタールード",
    "english ver", "korean ver",
]

_DECORATIVE_SYMBOLS_RE = re.compile(r"[♡★☆♪♫✩✧]")
_LIVE_TITLE_RE = re.compile(r"\blive\b|ライブ", re.IGNORECASE)
_LIVE_KEYWORD_FALSE_POSITIVES = {"LET'S LIVE！", "LET'S LIVE!", "Let's Live！", "Let's Live!"}
_MEDLEY_TITLE_RE = re.compile(r"\S/\S")
_MEDLEY_NUMBERED_RE = re.compile(r"^メドレー[\d\(（]")
_CONCERT_IN_BRACKETS_RE = re.compile(
    r"[\(\[（【][^)\]）】]*(?:\bconcert\b|コンサート|\btour\b)[^)\]）】]*[\)\]）】]", re.IGNORECASE
)
_YEAR_VERSION_SUFFIX_RE = re.compile(r"\(\d{4}\)(／.+)?$")
_TRAILING_VERSION_SUFFIX_RE = re.compile(r"-\s*.+-\s*$")
_EDITION_SUFFIX_RE = re.compile(r"\s+[ぁ-んァ-ヶー]+盤\s*$")
_TRAILING_LANG_VER_SUFFIX_RE = re.compile(r"\s*-\s*[A-Za-z]{2,6}\s*ver\.?\s*$", re.IGNORECASE)

_SMART_PUNCTUATION = "‘’“”–—…"


def _is_ascii_like(ch):
    return ord(ch) <= 127 or ch in _SMART_PUNCTUATION


def _is_ascii_like_text(text):
    return all(_is_ascii_like(ch) for ch in text)


def _is_bracket_balanced(text):
    counts = {"(": 0, ")": 0, "（": 0, "）": 0, "[": 0, "]": 0}
    for ch in text:
        if ch in counts:
            counts[ch] += 1
    return counts["("] == counts[")"] and counts["（"] == counts["）"] and counts["["] == counts["]"]


def clean_title(title):
    """"ピースサイン - Peace Sign"のように日本語タイトルに英題/ローマ字表記が
    " - "区切りで付与されている場合、非ASCII部分が続く限り残し、純ASCII部分
    (重複表記)が出た時点で切り捨てる。"""
    if not title:
        return title
    parts = [p for p in title.split(" - ") if p.strip()]
    if not parts:
        return title.strip()
    if _is_ascii_like_text(parts[0]):
        return title.strip()
    if not _is_bracket_balanced(parts[0]):
        return title.strip()
    kept = [parts[0]]
    for part in parts[1:]:
        if not _is_ascii_like_text(part):
            kept.append(part)
        else:
            break
    return " - ".join(kept).strip() or title.strip()


def extract_english_title(title):
    """clean_title()が切り捨てる" - "以降の英題/ローマ字表記部分を取り出す。
    YouTube Music側に日本語タイトルではなくこちらでしか登録されていない曲の
    検索フォールバック用。無ければNone。"""
    if not title:
        return None
    parts = [p for p in title.split(" - ") if p.strip()]
    if len(parts) < 2:
        return None
    if _is_ascii_like_text(parts[0]):
        return None
    kept_count = 1
    for part in parts[1:]:
        if not _is_ascii_like_text(part):
            kept_count += 1
        else:
            break
    dropped = parts[kept_count:]
    return " - ".join(dropped).strip() or None


def _has_unwanted_keyword(text):
    if not text:
        return False
    if text.strip() in _LIVE_KEYWORD_FALSE_POSITIVES:
        return False
    if _LIVE_TITLE_RE.search(text):
        return True
    t = text.lower()
    return any(k in t for k in UNWANTED_VERSION_KEYWORDS)


def _has_concert_in_brackets(title):
    return bool(_CONCERT_IN_BRACKETS_RE.search(title or ""))


def _has_year_version_suffix(title):
    return bool(_YEAR_VERSION_SUFFIX_RE.search(title or ""))


def _album_indicates_live(album_title):
    """曲名自体には版表記が無くても、収録アルバムがライブ盤/コンサート・
    ツアー音源であることがある。"""
    if not album_title:
        return False
    if _LIVE_TITLE_RE.search(album_title):
        return True
    if re.search(r"\btour\b", album_title, re.IGNORECASE):
        return True
    return _has_concert_in_brackets(album_title)


def is_medley_title(title):
    """「曲A/曲B」のように1トラックに複数曲がまとまっているものを除外する。"""
    title = title or ""
    return bool(_MEDLEY_TITLE_RE.search(title)) or bool(_MEDLEY_NUMBERED_RE.match(title))


def strip_trailing_version_suffix(title):
    if not title:
        return title
    for pattern in (_TRAILING_VERSION_SUFFIX_RE, _EDITION_SUFFIX_RE, _TRAILING_LANG_VER_SUFFIX_RE):
        m = pattern.search(title)
        if m:
            return title[: m.start()].strip()
    return title


def title_has_embedded_version_marker(title):
    if not title:
        return False
    if strip_trailing_version_suffix(title) != title.strip():
        return True
    return bool(re.search(r"[\(\[（【].*?[\)\]）】]", title))


def normalize_key(title_short):
    """重複検出用の正規化キー。括弧内容ごと取り除くため、同一アーティストの
    版違い(インスト・アニメver.等)をまとめる用途には適するが、他アーティスト
    の原曲と誤って同一視してしまうリスクがある(検索結果の一致確認には
    search_match_keyを使うこと)。"""
    t = unicodedata.normalize("NFC", title_short or "").lower()
    t = re.sub(r"[\(\[（【].*?[\)\]）】]", "", t)
    t = re.sub(r"feat\.?.*", "", t)
    t = strip_trailing_version_suffix(t)
    t = _DECORATIVE_SYMBOLS_RE.sub("", t)
    t = re.sub(r"(\d)(ver)\b", r"\1 \2", t)
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def search_match_key(title_short):
    """外部サービス(YouTube Music)の検索結果が探している曲と本当に一致するか
    の確認専用の正規化キー。normalize_keyと違い、括弧内の版表記は保持したまま
    比較する。"""
    t = unicodedata.normalize("NFC", title_short or "").lower()
    t = re.sub(r"feat\.?.*", "", t)
    t = strip_trailing_version_suffix(t)
    t = _DECORATIVE_SYMBOLS_RE.sub("", t)
    t = re.sub(r"(\d)(ver)\b", r"\1 \2", t)
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


_UNIT_ALBUM_TITLE_MARKER = "ユニットアルバム"
_UNIT_ALBUM_CREDIT_SUFFIX_RE = re.compile(r"^(.+?)/\S.*$")


def _strip_unit_album_credit_suffix(title):
    m = _UNIT_ALBUM_CREDIT_SUFFIX_RE.match(title or "")
    return m.group(1).strip() if m else title


def _clean_titles_in_place(tracks):
    for t in tracks:
        raw_title_short = unicodedata.normalize("NFC", t.get("title_short") or "")
        t["_english_title"] = extract_english_title(raw_title_short)
        title = clean_title(raw_title_short)
        if _UNIT_ALBUM_TITLE_MARKER in (t.get("_album_title") or ""):
            title = _strip_unit_album_credit_suffix(title)
        t["title_short"] = title


def _track_is_usable(track):
    if not track.get("id") or not track.get("title_short"):
        return False
    if _has_unwanted_keyword(track.get("title_version")):
        return False
    if _has_unwanted_keyword(track.get("title_short")):
        return False
    if is_medley_title(track.get("title_short")):
        return False
    if _has_concert_in_brackets(track.get("title_short")):
        return False
    if _has_year_version_suffix(track.get("title_short")):
        return False
    if _album_indicates_live(track.get("_album_title")):
        return False
    return True


def _pick_winner(group):
    """同じ曲の重複候補から1つを選ぶ。版表記の無い素のタイトルを優先し、
    その中では人気度(rank)が高い方を優先する。"""
    def sort_key(t):
        title = t.get("title_short") or ""
        plain = 1 if not t.get("title_version") and not title_has_embedded_version_marker(title) else 0
        return (plain, t.get("rank") or 0)
    return max(group, key=sort_key)


def _dedupe_tracks(tracks):
    groups = defaultdict(list)
    for t in tracks:
        key = normalize_key(t.get("title_short"))
        groups[key].append(t)

    winners = []
    for group in groups.values():
        unique_by_id = list({t["id"]: t for t in group}.values())
        winners.append(_pick_winner(unique_by_id) if len(unique_by_id) > 1 else unique_by_id[0])

    seen_ids = set()
    result = []
    for t in winners:
        if t["id"] in seen_ids:
            continue
        seen_ids.add(t["id"])
        result.append(t)
    return result


# ---- アーティスト解決 ----

def _search_artists(query, limit=10):
    data = _get(_DEEZER_API_BASE, "/search/artist", {"q": query, "limit": limit})
    return (data or {}).get("data", [])


def find_target_artist(artist_name):
    """アーティスト名からDeezerのartist_idを解決する。完全一致であっても
    アルバム数が0の空スタブは信用せず、アルバム数が最大の候補を採用する。"""
    results = _search_artists(artist_name, limit=10)
    if not results:
        return None
    query_norm = artist_name.strip().lower()
    exact_populated = [
        r for r in results
        if (r.get("name") or "").strip().lower() == query_norm and r.get("nb_album", 0) > 0
    ]
    if exact_populated:
        candidates = exact_populated
    else:
        populated = [r for r in results if r.get("nb_album", 0) > 0]
        candidates = populated if populated else results
    best = max(candidates, key=lambda r: (r.get("nb_album", 0), r.get("nb_fan", 0)))
    return best["id"]


# 改名・表記ゆれで同一アーティストの曲がDeezer上で複数の別artist_idへ分裂
# 登録されていることがある。ここに正規表示名→統合するartist_idのリストを
# 登録しておくと、いずれかのIDに解決された時点で他のIDの曲もまとめて取得する。
_ARTIST_MERGE_GROUPS = [
    {
        "display_name": "ときめき♡宣伝部",
        "trigger_ids": [238203301, 11300134],
        "fetch_ids": [238203301, 11300134],
    },
    {
        "display_name": "超ときめき♡宣伝部",
        "trigger_ids": [
            229486035,
            334225451,  # 坂井仁香 (超ときめき♡宣伝部)
            293591331,  # 小泉遥香 (超ときめき♡宣伝部)
            339946441,  # 吉川ひより (超ときめき♡宣伝部)
            361398382,  # 菅田愛貴 (超ときめき♡宣伝部)
            323086021,  # 辻野かなみ (超ときめき♡宣伝部)
            295264651,  # 杏ジュリア (超ときめき♡宣伝部)
        ],
        "fetch_ids": [
            229486035, 238203301, 11300134,
            334225451, 293591331, 339946441, 361398382, 323086021, 295264651,
        ],
        "strip_slash_credit": True,
        "prefer_new_ver": True,
        "duplicate_titles": [
            {
                "keep": "超ときめき♡宣伝部のVICTORY STORY",
                "drop": [
                    "ときめき（白抜きのハート記号）宣伝部のVICTORY STORY",
                    "ときめき♡宣伝部のVICTORY STORY",
                ],
            },
        ],
        "manual_extra_tracks": [
            {"title": "ツヨクなる", "artist": "辻野かなみ"},
        ],
    },
    {
        "display_name": "いぎなり東北産",
        "trigger_ids": [345384721, 117762712],
        "fetch_ids": [345384721, 117762712],
    },
    {
        "display_name": "つばきファクトリー",
        "trigger_ids": [375971061, 367607822],
        "fetch_ids": [375971061, 367607822],
    },
    {
        "display_name": "Juice=Juice",
        "trigger_ids": [13261347],
        "fetch_ids": [13261347],
        "exclude_exact_titles": [
            "私が言う前に抱きしめなきゃね",
            "五月雨美女がさ乱れる",
        ],
        "strip_slash_credit": True,
    },
]
_ARTIST_ID_TO_MERGE_GROUP = {
    trigger_id: group
    for group in _ARTIST_MERGE_GROUPS
    for trigger_id in group["trigger_ids"]
}

_SLASH_CREDIT_RE = re.compile(r"^(.+?)[/／](\S.*)$")


def _split_slash_credit(title):
    m = _SLASH_CREDIT_RE.match(title or "")
    if not m:
        return None
    return m.group(1).strip(), m.group(2).strip()


def _search_titles_match(target_title, candidate_title):
    """外部サービス(YouTube Music)の検索結果1件が、探している曲名と本当に
    一致するかを確認する。target_titleに括弧書きの版表記が含まれる場合は
    search_match_key(括弧の中身を保持)で厳密に比較し、含まれない場合は
    normalize_keyで比較する。"""
    def strip_credit(title):
        split = _split_slash_credit(title)
        return split[0] if split else title

    target_title = strip_credit(target_title)
    candidate_title = strip_credit(candidate_title)
    if re.search(r"[\(\[（【]", target_title or ""):
        return search_match_key(target_title) == search_match_key(candidate_title)
    return normalize_key(target_title) == normalize_key(candidate_title)


def _apply_slash_credit_rule(tracks):
    """スラッシュ以降のクレジット部分に"Concert"が含まれる場合はコンサート
    音源(ライブ)とみなして除外し、それ以外はクレジット部分を取り除いて曲名
    のみ残す。"""
    kept = []
    for t in tracks:
        split = _split_slash_credit(t.get("title_short"))
        if split is None:
            kept.append(t)
            continue
        song_title, credit = split
        if "concert" in credit.lower():
            continue
        t["title_short"] = song_title
        kept.append(t)
    return kept


_NEW_VER_RE = re.compile(r"[\s～~\-]+(?:超|\d{4})\s*ver\s*[～~]?\s*$", re.IGNORECASE)


def _apply_prefer_new_ver_rule(tracks):
    """歌い直し版("～超ver～"等)と無印の版が両方存在する曲は、歌い直し版の
    方を正式版として残す。"""
    def base_key(title):
        return normalize_key(_NEW_VER_RE.sub("", title or ""))

    plain_info_by_key = {}
    for t in tracks:
        if not _NEW_VER_RE.search(t.get("title_short") or ""):
            key = base_key(t.get("title_short"))
            if key not in plain_info_by_key:
                plain_info_by_key[key] = {
                    "_album_title": t.get("_album_title"),
                    "artist": t.get("artist"),
                }

    new_ver_keys = set(plain_info_by_key.keys()) & {
        base_key(t.get("title_short"))
        for t in tracks
        if _NEW_VER_RE.search(t.get("title_short") or "")
    }

    kept = []
    for t in tracks:
        title = t.get("title_short") or ""
        if _NEW_VER_RE.search(title):
            plain_info = plain_info_by_key.get(base_key(title))
            if plain_info:
                t["_plain_version_album_title"] = plain_info["_album_title"]
                t["_plain_version_artist"] = plain_info["artist"]
            kept.append(t)
        elif base_key(title) not in new_ver_keys:
            kept.append(t)
    return kept


def _apply_duplicate_titles_rule(tracks, duplicate_titles):
    if not duplicate_titles:
        return tracks
    present_titles = {t.get("title_short") for t in tracks}
    drop_titles = set()
    for group in duplicate_titles:
        if group["keep"] in present_titles:
            drop_titles.update(group["drop"])
    if not drop_titles:
        return tracks
    return [t for t in tracks if t.get("title_short") not in drop_titles]


_MANUAL_EXTRA_ID_PREFIX = "manual:"


def _build_manual_extra_tracks(manual_extra_tracks, existing_keys):
    tracks = []
    for i, entry in enumerate(manual_extra_tracks or []):
        title = entry.get("title")
        if not title or normalize_key(title) in existing_keys:
            continue
        tracks.append({
            "id": f"{_MANUAL_EXTRA_ID_PREFIX}{i}",
            "title_short": title,
            "title_version": "",
            "rank": 0,
            "artist": {"name": entry.get("artist")},
            "_album_title": entry.get("album"),
        })
    return tracks


def resolve_artist(artist_name):
    """アーティスト名から、取得対象のartist_idリスト・表示名・グループ設定を
    解決する。"""
    artist_id = find_target_artist(artist_name)
    if artist_id is None:
        return None, None, {}
    group = _ARTIST_ID_TO_MERGE_GROUP.get(artist_id)
    if group:
        return group["fetch_ids"], group["display_name"], group
    return [artist_id], artist_name.strip(), {}


# ---- 全曲取得 ----

def _paginate(path, params, max_items):
    items = []
    index = 0
    limit = min(100, max_items)
    while len(items) < max_items:
        page = _get(_DEEZER_API_BASE, path, {**params, "limit": limit, "index": index})
        if not page or not page.get("data"):
            break
        items.extend(page["data"])
        index += limit
        if not page.get("next") or len(page["data"]) < limit:
            break
    return items[:max_items]


def _fetch_artist_albums(artist_id):
    return _paginate(f"/artist/{artist_id}/albums", {}, max_items=300)


def _fetch_album_tracks(album):
    data = _get(_DEEZER_API_BASE, f"/album/{album['id']}/tracks", {"limit": 100})
    tracks = (data or {}).get("data", [])
    for t in tracks:
        t["_album_title"] = album.get("title")
    return tracks


def _fetch_all_tracks_raw(artist_id):
    albums = _fetch_artist_albums(artist_id)
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(_fetch_album_tracks, albums))

    raw = []
    seen_ids = set()
    for tracks in results:
        for t in tracks:
            if t.get("id") in seen_ids:
                continue
            seen_ids.add(t.get("id"))
            raw.append(t)
    return raw


# ---- iTunesからの補完取得(Deezerに無い曲を埋める) ----

_ITUNES_ID_PREFIX = "itunes:"


def _itunes_get(path_base, params, retries=2):
    for attempt in range(retries + 1):
        try:
            res = _session.get(path_base, params=params, timeout=10)
            if res.status_code != 200:
                raise requests.HTTPError(f"unexpected status {res.status_code}")
            return res.json()
        except Exception:
            if attempt < retries:
                time.sleep(0.4)
    return None


def _itunes_find_artist_id(display_name):
    data = _itunes_get(_ITUNES_SEARCH_API_BASE, {"term": display_name, "entity": "musicArtist", "limit": 1, "country": "JP"})
    results = (data or {}).get("results", [])
    return results[0]["artistId"] if results else None


def _itunes_fetch_artist_albums(artist_id):
    data = _itunes_get(_ITUNES_LOOKUP_API_BASE, {"id": artist_id, "entity": "album", "limit": 200, "country": "JP"})
    results = (data or {}).get("results", [])
    return [r for r in results if r.get("wrapperType") == "collection"]


def _itunes_fetch_album_tracks(album):
    data = _itunes_get(_ITUNES_LOOKUP_API_BASE, {"id": album["collectionId"], "entity": "song", "country": "JP"})
    results = (data or {}).get("results", [])
    tracks = [r for r in results if r.get("wrapperType") == "track"]
    for t in tracks:
        t["_album_title"] = album.get("collectionName")
    return tracks


def _itunes_fetch_all_tracks_raw(artist_id):
    albums = _itunes_fetch_artist_albums(artist_id)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(_itunes_fetch_album_tracks, albums))

    raw = []
    seen_ids = set()
    for tracks in results:
        for t in tracks:
            if t.get("trackId") in seen_ids:
                continue
            seen_ids.add(t.get("trackId"))
            raw.append(t)
    return raw


def _itunes_track_to_raw_shape(t):
    """iTunesの曲データを、Deezer側のフィルタ関数がそのまま使える形に変換する。
    rankに相当する人気度指標は無いため0固定(Deezer由来の曲より必ず後ろに
    並ぶ)。"""
    return {
        "id": f"{_ITUNES_ID_PREFIX}{t['trackId']}",
        "title_short": t.get("trackName"),
        "title_version": "",
        "rank": 0,
        "artist": {"name": t.get("artistName")},
        "_album_title": t.get("_album_title"),
    }


def _fetch_itunes_catalog(canonical_name):
    """アーティストのiTunes上の全曲を、Deezer側と同じ形に整形・フィルタ・
    重複排除した状態で返す。失敗してもDeezerの結果には一切影響させず、空
    リストを返す。"""
    try:
        itunes_artist_id = _itunes_find_artist_id(canonical_name)
        if itunes_artist_id is None:
            return []
        raw = _itunes_fetch_all_tracks_raw(itunes_artist_id)
    except Exception:
        return []

    adapted = [_itunes_track_to_raw_shape(t) for t in raw]
    _clean_titles_in_place(adapted)
    usable = [t for t in adapted if _track_is_usable(t)]
    return _dedupe_tracks(usable)


def _to_quiz_track(raw, artist_name):
    return {
        "title": raw.get("title_short") or raw.get("title") or "不明な曲",
        "artist": (raw.get("artist") or {}).get("name") or artist_name,
        "album": raw.get("_album_title"),
        "rank": raw.get("rank") or 0,
        "english_title": raw.get("_english_title"),
        "plain_album_title": raw.get("_plain_version_album_title"),
        "plain_artist": (raw.get("_plain_version_artist") or {}).get("name"),
    }


def get_ranked_tracks(artist_name):
    """アーティスト名から全曲一覧を取得する。見つからない場合はNoneを返す。
    並び順は常にDeezerの人気度(rank)の高い順(Top25/Top50スコープでの上位
    抽出・不足分の下位順位からの補充は呼び出し側(app.py)の責務)。歌詞・
    再生用の音源は含まないため、呼び出し側がYouTube Music側で曲ごとに検索
    してマッチングすること。"""
    artist_ids, canonical_name, group_config = resolve_artist(artist_name)
    if artist_ids is None:
        return None

    # A merge-group artist (see _ARTIST_MERGE_GROUPS) can mean walking several
    # artist_ids' full discographies -- run those, and the independent iTunes
    # catalog fetch, all concurrently rather than one after another.
    with ThreadPoolExecutor(max_workers=len(artist_ids) + 1) as pool:
        deezer_futures = [pool.submit(_fetch_all_tracks_raw, aid) for aid in artist_ids]
        itunes_future = pool.submit(_fetch_itunes_catalog, canonical_name)
        raw = []
        for f in deezer_futures:
            raw.extend(f.result())
        itunes_catalog = itunes_future.result()

    if group_config.get("strip_slash_credit"):
        raw = _apply_slash_credit_rule(raw)
    _clean_titles_in_place(raw)
    exclude_exact_titles = set(group_config.get("exclude_exact_titles") or [])
    if exclude_exact_titles:
        raw = [t for t in raw if (t.get("title_short") or "") not in exclude_exact_titles]
    usable = [t for t in raw if _track_is_usable(t)]
    picked = _dedupe_tracks(usable)

    existing_keys = {normalize_key(t["title_short"]) for t in picked}
    picked += [t for t in itunes_catalog if normalize_key(t["title_short"]) not in existing_keys]

    if group_config.get("manual_extra_tracks"):
        existing_keys = {normalize_key(t["title_short"]) for t in picked}
        picked += _build_manual_extra_tracks(group_config["manual_extra_tracks"], existing_keys)

    if group_config.get("prefer_new_ver"):
        picked = _apply_prefer_new_ver_rule(picked)

    picked = _apply_duplicate_titles_rule(picked, group_config.get("duplicate_titles"))

    picked.sort(key=lambda t: t.get("rank") or 0, reverse=True)

    quiz_tracks = [_to_quiz_track(t, canonical_name) for t in picked]
    return {"artistName": canonical_name, "tracks": quiz_tracks}
