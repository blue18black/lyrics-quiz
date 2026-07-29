import base64
import random
import re
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, jsonify, render_template, request
from ytmusicapi import YTMusic

app = Flask(__name__)
# English locale is used for all catalog/track fetching -- Japanese-artist track
# titles come through in Japanese either way (with a romanized " - romaji" suffix
# that clean_title() strips), but Japanese locale makes YT Music surface extra
# katakana-transliterated duplicate tracks for non-Japanese artists (e.g. "Blinding
# Lights" also appearing as "ブラインディング・ライツ"). Japanese locale is only used
# separately (yt_ja) to resolve a Japanese artist's native display name, since
# English-locale search/artist results romanize it (e.g. "Cho Tokimeki Sendenbu"
# instead of "超ときめき♡宣伝部").
yt = YTMusic()
yt_ja = YTMusic(language="ja")


def clean_title(title: str) -> str:
    """Collapse "official title - romanized/duplicate title" into just the official title.

    YouTube Music frequently stores non-English titles as "日本語タイトル - Romanized Title".
    We keep segments (split on " - ") up to the first one that's pure ASCII, since that's
    almost always the redundant romanization rather than part of the real title.
    """
    parts = [p for p in title.split(" - ") if p.strip()]
    if not parts:
        return title.strip()

    kept = [parts[0]]
    for part in parts[1:]:
        if any(ord(ch) > 127 for ch in part):
            kept.append(part)
        else:
            break
    return " - ".join(kept).strip() or title.strip()


# Song-fetching / dedup logic below is ported from YouTube Music/playlist_builder.py,
# which handles instrumental filtering and duplicate-version resolution more accurately
# than simple regex heuristics.

INSTRUMENTAL_KEYWORDS = [
    "instrumental", "off vocal", "offvocal", "backing track", "less vocal",
    "インスト", "オフボーカル", "カラオケ", "レスボーカル",
]


def is_instrumental(title: str) -> bool:
    t = title.lower()
    return any(k.lower() in t for k in INSTRUMENTAL_KEYWORDS)


#  Detects trailing remix/version tags that aren't wrapped in brackets, e.g.
#  "夢見る 15歳 PAX JAPONICA GROOVE REMIX".
_TRAILING_VARIANT_RE = re.compile(
    r"\s+(?:[a-z0-9.\-']+\s+)*(remix|re-mix|mix|version|ver\.?|remaster(?:ed)?|type\s*\d*)\s*$",
    re.IGNORECASE,
)
#  "原題 - ローマ字表記" / "曲名 -Live ver.- - Romaji" style suffixes: everything
#  from the first " -" onward is a romanization or version tag, not part of the title.
_SPACE_HYPHEN_RE = re.compile(r"\s-")
#  A "-tag-" pair (hyphens with no space before the first one) marks a version tag,
#  as opposed to the single " - " that separates title from romanization.
_PAIRED_HYPHEN_RE = re.compile(r"-[^-]+-")
_BRACKET_RE = re.compile(r"[\(\[（【]")


def normalize_title(title: str) -> str:
    """Dedup key for grouping different releases of the "same" song."""
    t = title.lower()
    t = re.sub(r"[\(\[（【].*[\)\]）】]", "", t)
    t = re.sub(r"feat\.?.*", "", t)
    t = _SPACE_HYPHEN_RE.split(t, maxsplit=1)[0]
    t = _TRAILING_VARIANT_RE.sub("", t)
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def strip_variant_for_display(title: str) -> str:
    """Same trimming rules as normalize_title, but keeping case/punctuation
    intact so the result is presentable in the quiz UI."""
    t = re.sub(r"[\(\[（【].*[\)\]）】]", "", title)
    t = re.sub(r"feat\.?.*", "", t, flags=re.IGNORECASE)
    t = _SPACE_HYPHEN_RE.split(t, maxsplit=1)[0]
    t = _TRAILING_VARIANT_RE.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t or title.strip()


def _has_variant_qualifier(title: str) -> bool:
    return (
        bool(_BRACKET_RE.search(title))
        or bool(_TRAILING_VARIANT_RE.search(title))
        or bool(_PAIRED_HYPHEN_RE.search(title))
    )


def _year_value(year):
    try:
        return -int(year)
    except (TypeError, ValueError):
        return float("-inf")


def _pick_winner(unique: list[dict]) -> dict:
    """Pick the canonical release among duplicate titles:
    1) prefer releases without a version qualifier (unless every release has one)
    2) prefer the oldest release
    3) prefer single/EP over an album track
    4) prefer the regular edition (通常盤) release
    """
    plain = [t for t in unique if not _has_variant_qualifier(t["title"])]
    candidates = plain if plain else unique
    return max(
        candidates,
        key=lambda t: (
            _year_value(t["_year"]),
            1 if t["_type"] in ("single", "ep") else 0,
            1 if "通常盤" in (t.get("albumName") or "") else 0,
        ),
    )


_DOMINANT_SONG_VOTES = 8


def _is_pure_katakana(s: str) -> bool:
    return bool(s) and all("゠" <= ch <= "ヿ" or ch in " ・-.'" for ch in s)


def _is_ascii(s: str) -> bool:
    return bool(s) and all(ord(ch) < 128 for ch in s)


def find_target_artist(artist: str) -> tuple[str, str] | None:
    """Resolve a (possibly loosely-typed) artist name to a canonical (name, channelId).

    Two independent signals are combined:
    1) Majority vote: the most common primary artist among "songs"-filtered search
       results. YouTube Music often stores names in a different script/romanization
       than what the user typed (e.g. "私立恵比寿中学" is catalogued as "Shiritsu Ebisu
       Chugaku"), so string equality doesn't work -- but this correctly disambiguates
       ambiguous single-word queries like "SEKAI" (-> SEKAI NO OWARI, based on which
       artist's songs actually dominate the results) where YT Music's own artist search
       ranks a same-named but irrelevant channel first.
    2) YT Music's own "artists"-filtered search, top result. This handles nicknames/
       abbreviations well (e.g. "ミスチル" -> Mr.Children) that barely match any song
       titles at all for (1).

    When they agree, great. When they disagree, (1) is only trusted if its winner both
    clears an absolute vote floor AND leads the runner-up by a wide margin -- otherwise
    (2) wins. Two failure modes motivate this:
    - Generic-word queries (e.g. "Mr.Children" typed in English) can make (1) pull back
      a pile of loosely/fuzzily matched, totally unrelated songs (a "Kids" by MGMT, a
      "Mr. Brightside" by The Killers, ...) where the "winning" artist only has a couple
      of scattered votes out of many candidates -- noise that looks like a majority but
      isn't (fails the absolute floor).
    - Genuine name collisions (e.g. "aiko" -> the Japanese singer-songwriter vs. "Jhené
      Aiko") can give two real, both-popular artists close vote counts (19 vs. 17) --
      neither is a clear majority, so a runner-up-margin comparison is a near coin flip
      that flips between calls as YT Music's own result ordering/counts fluctuate.

    To make name collisions like "aiko" deterministic, an *exact* (case-insensitive)
    match between the query and an "artists"-filtered result's name is treated as its
    own strong signal, separate from the runner-up margin above: it wins unless a
    different artist's songs outnumber it by a wide (2x) margin. That margin is checked
    against the exact match's *own* vote count (not the runner-up's), which is a much
    larger, noise-tolerant gap for real collisions -- "aiko" is 17 vs. 19 (~1.1x, exact
    match wins) while "SEKAI" (which exactly matches a small, unrelated channel) is 4
    vs. SEKAI NO OWARI's 10 (2.5x, song votes win).
    """
    def _search_songs():
        return yt.search(artist, filter="songs", limit=25)

    def _search_artists():
        try:
            return yt.search(artist, filter="artists", limit=10)
        except Exception:
            return []

    # These two lookups are independent -- run them concurrently instead of back to
    # back, since each is a separate network round-trip to YT Music.
    with ThreadPoolExecutor(max_workers=2) as pool:
        songs_future = pool.submit(_search_songs)
        artists_future = pool.submit(_search_artists)
        results = songs_future.result()
        artist_results = artists_future.result()

    counts: dict[str, int] = {}
    for r in results:
        artists = r.get("artists") or []
        if not artists:
            continue
        artist_id = artists[0].get("id")
        if artist_id:
            counts[artist_id] = counts.get(artist_id, 0) + 1
    sorted_counts = sorted(counts.values(), reverse=True)
    song_vote_id = max(counts, key=counts.get) if counts else None
    song_vote_count = sorted_counts[0] if sorted_counts else 0
    runner_up_count = sorted_counts[1] if len(sorted_counts) > 1 else 0

    artist_search_id = artist_results[0].get("browseId") if artist_results else None

    query_norm = artist.strip().lower()
    exact_match_ids = [
        r["browseId"] for r in artist_results
        if r.get("browseId") and (r.get("artist") or "").strip().lower() == query_norm
    ]
    exact_match_id = None
    if exact_match_ids:
        # Several results can carry the same exact name (homonym channels); prefer
        # whichever one actually shows up in the song-vote data, since that's the
        # one with a real catalog rather than a near-empty duplicate/ghost channel.
        exact_match_id = max(exact_match_ids, key=lambda aid: counts.get(aid, 0))
    exact_match_votes = counts.get(exact_match_id, 0) if exact_match_id else 0

    is_dominant = song_vote_count >= _DOMINANT_SONG_VOTES and song_vote_count >= runner_up_count * 1.5

    if exact_match_id:
        song_dominates_exact = (
            song_vote_id
            and song_vote_id != exact_match_id
            and song_vote_count >= _DOMINANT_SONG_VOTES
            and song_vote_count >= exact_match_votes * 2
        )
        artist_id = song_vote_id if song_dominates_exact else exact_match_id
    elif song_vote_id and (song_vote_id == artist_search_id or is_dominant):
        artist_id = song_vote_id
    elif artist_search_id:
        artist_id = artist_search_id
    else:
        artist_id = song_vote_id

    if not artist_id:
        return None

    # English-locale search romanizes non-English artist names (e.g. "Cho Tokimeki
    # Sendenbu" instead of "超ときめき♡宣伝部"), so ja-locale is generally the better
    # display name -- except it also transliterates genuinely Western artist names into
    # katakana (e.g. "The Weeknd" -> "ザ・ウィークエンド"), which we don't want. Treat a
    # pure-katakana ja-name paired with a plain-ASCII en-name as that unwanted case and
    # keep the original instead.
    def _get_ja_name():
        try:
            return yt_ja.get_artist(artist_id).get("name")
        except Exception:
            return None

    def _get_en_name():
        try:
            return yt.get_artist(artist_id).get("name")
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        ja_future = pool.submit(_get_ja_name)
        en_future = pool.submit(_get_en_name)
        ja_name = ja_future.result()
        en_name = en_future.result()

    if ja_name and _is_pure_katakana(ja_name) and en_name and _is_ascii(en_name):
        name = en_name
    else:
        name = ja_name or en_name or artist
    return name, artist_id


_ARTIST_TRACKS_CACHE: dict[str, list[dict]] = {}


# Release titles that are essentially never a genuine A-side studio track and are
# frequently missing lyrics data entirely (live recordings, instrumental-only
# "less vocal" mixes, best-of compilations) -- some Japanese-market artists have
# dozens of these, which were bloating the discography with entries that mostly
# just failed the lyrics fetch anyway. Matched against the *release* title, not
# individual track titles, so a legitimately-titled song isn't caught by this.
_EXCLUDED_RELEASE_RE = re.compile(
    r"\blive\b|ライブ|less vocal|instrumental|インスト|\bbest\b|ベスト|"
    r"greatest hits|complete\s*(pack|edition)?|selection",
    re.IGNORECASE,
)


def _collect_album_refs(artist_page: dict, artist_id: str, section_key: str) -> list[dict]:
    """Fetch every entry in an artist page section (albums/singles), following the
    "show more" pagination when present. Falls back to the initially-shown results if
    the paginated fetch errors out or unexpectedly returns fewer entries than that."""
    section = artist_page.get(section_key) or {}
    results = section.get("results", [])
    params = section.get("params")
    if not params:
        return results

    # The "show more" target is the section's own browseId (a discography-specific
    # id), not the artist's regular browseId.
    browse_id = section.get("browseId") or artist_id
    try:
        full = yt.get_artist_albums(browse_id, params)
        if len(full) >= len(results):
            results = full
    except Exception:
        pass

    return [r for r in results if not _EXCLUDED_RELEASE_RE.search(r.get("title") or "")]


def fetch_all_tracks(artist_id: str) -> list[dict]:
    """Walk the artist's full discography (every album/single/EP) instead of relying on
    fuzzy search, so obscure b-sides aren't missed just because they rank low in search."""
    if artist_id in _ARTIST_TRACKS_CACHE:
        return _ARTIST_TRACKS_CACHE[artist_id]

    artist_page = yt.get_artist(artist_id)

    album_entries = _collect_album_refs(artist_page, artist_id, "albums")
    single_entries = _collect_album_refs(artist_page, artist_id, "singles")

    # YT Music returns each section newest-first; reverse so that, once sorted by
    # year below, entries within the same year keep their original release order.
    album_entries = list(reversed(album_entries))
    single_entries = list(reversed(single_entries))

    for entry in single_entries:
        entry["_type_rank"] = 0
    for entry in album_entries:
        entry["_type_rank"] = 1

    def entry_year(entry):
        try:
            return int(entry.get("year"))
        except (TypeError, ValueError):
            return 9999  # unknown year: treat as newest, sort to the end

    # Oldest -> newest overall; singles/EPs before albums within the same year.
    all_entries = sorted(
        single_entries + album_entries,
        key=lambda e: (entry_year(e), e["_type_rank"]),
    )

    tracks = []
    seen_album_ids = set()
    for entry in all_entries:
        album_id = entry.get("browseId")
        if not album_id or album_id in seen_album_ids:
            continue
        seen_album_ids.add(album_id)
        try:
            album = yt.get_album(album_id)
        except Exception:
            continue

        year = int(album["year"]) if album.get("year") else None
        album_type = (album.get("type") or "").lower()
        album_name = album.get("title")
        for t in album.get("tracks", []):
            title, video_id = t.get("title"), t.get("videoId")
            if not title or not video_id:
                continue
            tracks.append({
                "title": title,
                "videoId": video_id,
                "albumId": album_id,
                "albumName": album_name,
                "_year": year,
                "_type": album_type,
            })

    _ARTIST_TRACKS_CACHE[artist_id] = tracks
    return tracks


_ARTIST_POPULAR_CACHE: dict[str, list[dict]] = {}
_POPULAR_TRACK_LIMIT = 50


def fetch_popular_tracks(artist_id: str) -> list[dict]:
    """Fetch (up to) the artist's top 50 most popular tracks, using YT Music's own
    "Top Songs" ranking playlist, for a "well-known songs only" difficulty tier --
    as opposed to fetch_all_tracks's full discography walk."""
    if artist_id in _ARTIST_POPULAR_CACHE:
        return _ARTIST_POPULAR_CACHE[artist_id]

    artist_page = yt.get_artist(artist_id)
    songs_section = artist_page.get("songs") or {}

    raw_tracks = []
    playlist_id = songs_section.get("browseId")
    if playlist_id:
        try:
            playlist = yt.get_playlist(playlist_id, limit=_POPULAR_TRACK_LIMIT)
            raw_tracks = playlist.get("tracks", [])
        except Exception:
            raw_tracks = []
    if not raw_tracks:
        raw_tracks = songs_section.get("results", [])

    tracks = []
    for t in raw_tracks[:_POPULAR_TRACK_LIMIT]:
        title, video_id = t.get("title"), t.get("videoId")
        if not title or not video_id:
            continue
        album = t.get("album") or {}
        tracks.append({
            "title": title,
            "videoId": video_id,
            "albumId": album.get("id"),
            "albumName": album.get("name"),
            # The Top Songs playlist doesn't expose release year/type, so duplicate
            # versions fall back to _pick_winner's title-based (variant-qualifier)
            # tie-break only.
            "_year": None,
            "_type": "",
        })

    _ARTIST_POPULAR_CACHE[artist_id] = tracks
    return tracks


_ARTIST_VIDEO_CACHE: dict[str, list[dict]] = {}
_VIDEO_TITLE_RE = re.compile(r"[「『]([^」』]+)[」』]")
_LIVE_VIDEO_RE = re.compile(r"live|tour|ライブ|ツアー", re.IGNORECASE)


def fetch_video_tracks(artist_id: str) -> list[dict]:
    """Some major-label artists (e.g. Mr.Children) aren't fully synced to YT Music's
    structured catalog -- no albums/singles sections, an empty/near-empty Top Songs
    playlist -- but do have official music videos uploaded, listed in the artist
    page's "videos" section as "<Artist>「<Song>」MUSIC VIDEO"-style titles. Used as a
    last-resort fallback when the normal catalog sources come up empty."""
    if artist_id in _ARTIST_VIDEO_CACHE:
        return _ARTIST_VIDEO_CACHE[artist_id]

    artist_page = yt.get_artist(artist_id)
    videos_section = artist_page.get("videos") or {}
    browse_id = videos_section.get("browseId")

    raw_tracks = videos_section.get("results", [])
    if browse_id:
        try:
            playlist = yt.get_playlist(browse_id, limit=100)
            if playlist.get("tracks"):
                raw_tracks = playlist["tracks"]
        except Exception:
            pass

    tracks = []
    for t in raw_tracks:
        raw_title, video_id = t.get("title"), t.get("videoId")
        if not raw_title or not video_id:
            continue
        if _LIVE_VIDEO_RE.search(raw_title):
            continue
        m = _VIDEO_TITLE_RE.search(raw_title)
        if not m:
            continue
        tracks.append({
            "title": m.group(1).strip(),
            "videoId": video_id,
            "albumId": None,
            "albumName": None,
            "_year": None,
            "_type": "",
        })

    _ARTIST_VIDEO_CACHE[artist_id] = tracks
    return tracks


def _dedupe_tracks(tracks: list[dict]) -> list[dict]:
    entries = []
    for t in tracks:
        if is_instrumental(t["title"]):
            continue
        entries.append({**t, "title": clean_title(t["title"])})

    groups: dict[str, list[dict]] = {}
    for e in entries:
        groups.setdefault(normalize_title(e["title"]), []).append(e)

    songs = []
    for group in groups.values():
        # collapse exact same-videoId dupes before picking a winner
        unique = list({e["videoId"]: e for e in group}.values())
        winner = _pick_winner(unique)
        # Always display the version-stripped title, even when the only available
        # release still has a marker (e.g. a tour-only live cut) -- this keeps
        # titles clean instead of showing "Song (...JapanホールTour2016 ver.)".
        songs.append({"title": strip_variant_for_display(winner["title"]), "videoId": winner["videoId"]})
    return songs


def fetch_songs(artist: str, scope: str = "all") -> list[dict]:
    """scope="top50"/"top25" limits the pool to the artist's most popular tracks
    (by YT Music's own ranking) instead of the full discography."""
    target = find_target_artist(artist)
    if not target:
        return []
    _, artist_id = target

    if scope == "top25":
        tracks = fetch_popular_tracks(artist_id)[:25]
    elif scope == "top50":
        tracks = fetch_popular_tracks(artist_id)
    else:
        tracks = fetch_all_tracks(artist_id)

    songs = _dedupe_tracks(tracks)
    if len(songs) < 4:
        # Catalog sources came up empty/near-empty (see fetch_video_tracks) -- fall
        # back to the artist's official music videos before giving up entirely.
        songs = _dedupe_tracks(fetch_video_tracks(artist_id))
    return songs


def _estimated_wrapped_rows(line: str, chars_per_row: int = 22) -> int:
    """Rough estimate of how many visual rows a lyric line takes once it wraps in the
    quiz card (which is a fixed, narrow width) -- used to bound rendered height."""
    return max(1, -(-len(line) // chars_per_row))  # ceil division


def extract_snippet(
    lyrics_text: str,
    title: str,
    min_lines: int = 2,
    max_lines: int = 4,
    max_visual_rows: int = 4,
) -> str | None:
    """Pick a random run of lyric lines. A single long lyric line can wrap to multiple
    rows on its own, so line count alone doesn't bound the rendered height -- cap by
    estimated wrapped row count instead (still respecting max_lines)."""
    lines = [line.strip() for line in lyrics_text.splitlines()]
    lines = [line for line in lines if line and not re.match(r"^[\[\(].*[\]\)]$", line)]
    if len(lines) < min_lines:
        return None

    title_lower = title.lower()
    attempts = 0
    while attempts < 15:
        attempts += 1
        start = random.randint(0, len(lines) - min_lines)
        snippet_lines = lines[start:start + min_lines]
        rows = sum(_estimated_wrapped_rows(line) for line in snippet_lines)
        for extra_line in lines[start + min_lines:start + max_lines]:
            extra_rows = _estimated_wrapped_rows(extra_line)
            if rows + extra_rows > max_visual_rows:
                break
            snippet_lines.append(extra_line)
            rows += extra_rows

        snippet = "\n".join(snippet_lines)
        if title_lower not in snippet.lower():
            return snippet
    return None


_LYRICS_CACHE: dict[str, str | None] = {}


def fetch_lyrics(video_id: str) -> str | None:
    # "Play again" reshuffles from the same (cached) song pool, so replaying
    # tends to re-hit songs whose lyrics were already fetched last time --
    # cache both hits and confirmed misses so a replay doesn't redo this
    # network round-trip for every song, which was the slow part of a
    # rebuild. Only a confirmed "this song has no lyrics" is cached as a miss
    # though -- a transient network/API error is NOT cached, since that would
    # otherwise permanently blacklist a song that just had a momentary
    # hiccup for the rest of the server's uptime.
    if video_id in _LYRICS_CACHE:
        return _LYRICS_CACHE[video_id]
    try:
        watch_playlist = yt.get_watch_playlist(video_id)
        lyrics_browse_id = watch_playlist.get("lyrics")
        if not lyrics_browse_id:
            _LYRICS_CACHE[video_id] = None
            return None
        lyrics = yt.get_lyrics(lyrics_browse_id)["lyrics"]
    except Exception:
        return None
    _LYRICS_CACHE[video_id] = lyrics
    return lyrics


def build_questions(
    artist: str,
    count: int | None,
    difficulty: str = "normal",
    scope: str = "all",
) -> list[dict]:
    """count=None means "as many as we can find". difficulty="hard" shows a single
    lyric line instead of 2-4, making the source song harder to guess. scope="top50"/
    "top25" limits the song pool to the artist's most popular tracks."""
    min_lines, max_lines = (1, 1) if difficulty == "hard" else (2, 4)

    songs = fetch_songs(artist, scope)
    if len(songs) < 4:
        return []

    random.shuffle(songs)
    all_titles = [s["title"] for s in songs]

    questions = []
    for song in songs:
        if count is not None and len(questions) >= count:
            break
        lyrics = fetch_lyrics(song["videoId"])
        if not lyrics:
            continue

        snippet = extract_snippet(lyrics, song["title"], min_lines=min_lines, max_lines=max_lines)
        if not snippet:
            continue

        distractor_pool = [t for t in all_titles if t != song["title"]]
        if len(distractor_pool) < 3:
            continue
        distractors = random.sample(distractor_pool, 3)
        choices = distractors + [song["title"]]
        random.shuffle(choices)

        questions.append({
            "snippet": snippet,
            "choices": choices,
            # base64'd, not plaintext -- the answer is still technically visible to
            # anyone who inspects the network response, but this at least keeps it
            # from being readable at a glance.
            "a": base64.b64encode(song["title"].encode("utf-8")).decode("ascii"),
        })

    return questions


@app.route("/")
def index():
    return render_template("index.html")


_SUGGESTION_NAME_CACHE: dict[str, str | None] = {}


def resolve_suggestion_name(raw: str) -> str | None:
    """get_search_suggestions sometimes returns a truncated/partial artist name
    (e.g. "ときめき宣伝部" for 超ときめき♡宣伝部) or an "artist songname" completion
    (e.g. "YOASOBI 夜に駆ける") -- resolve it to just the artist's actual full name so
    that's what's shown in the dropdown. Returns None (dropped by the caller) rather
    than the raw, possibly song-name-containing text when it can't be resolved.

    Deliberately lighter-weight than find_target_artist (used for the actual quiz
    build): a single ja-locale "artists" search, instead of also cross-checking a
    song-search majority vote. That cross-check matters for a bare, ambiguous
    single word typed by the user (e.g. "SEKAI"), but raw suggestions from YT
    Music's own autocomplete are already specific phrases (e.g. "sekai no owari
    rpg"), so a direct artist search resolves them reliably without needing it --
    and skipping it keeps suggestions responsive while typing, since this runs
    once per candidate (up to 6) on every keystroke.
    """
    if raw in _SUGGESTION_NAME_CACHE:
        return _SUGGESTION_NAME_CACHE[raw]
    try:
        results = yt_ja.search(raw, filter="artists", limit=1)
    except Exception:
        results = []
    resolved = results[0].get("artist") if results else None
    _SUGGESTION_NAME_CACHE[raw] = resolved
    return resolved


def _direct_artist_matches(query: str) -> list[str]:
    # get_search_suggestions' own autocomplete phrases sometimes miss an artist
    # entirely for a short/generic query (e.g. "超" for "超ときめき♡宣伝部" -- none
    # of YT Music's suggested completions resolve to it), even though a direct
    # artist-name search for that same raw text does surface it. Searching the
    # raw query directly (not just resolving autocomplete phrases) catches these.
    try:
        # ytmusicapi's `limit` only controls how many result pages it fetches, not
        # how many it returns -- a single page is already ~20 items, so slice.
        results = yt_ja.search(query, filter="artists", limit=5)[:5]
    except Exception:
        results = []
    return [r["artist"] for r in results if r.get("artist")]


@app.route("/api/suggest")
def suggest():
    query = (request.args.get("q") or "").strip()
    if len(query) < 1:
        return jsonify([])
    try:
        raw = yt.get_search_suggestions(query)
    except Exception:
        raw = []

    # get_search_suggestions mixes bare artist names in with "artist songname"
    # completions. Some real artist names are themselves multi-word (e.g. "SEKAI
    # NO OWARI"), so a space can't be used to tell those apart -- instead resolve
    # every candidate through find_target_artist below, which collapses both
    # cases down to the actual artist name via majority vote.
    seen = set()
    candidates = []
    for text in raw:
        if text in seen:
            continue
        seen.add(text)
        candidates.append(text)
    candidates = candidates[:6]

    with ThreadPoolExecutor(max_workers=len(candidates) + 1) as pool:
        resolve_futures = [pool.submit(resolve_suggestion_name, c) for c in candidates]
        direct_future = pool.submit(_direct_artist_matches, query)
        resolved = [f.result() for f in resolve_futures]
        direct_matches = direct_future.result()

    seen_resolved = set()
    names = []
    for name in resolved + direct_matches:
        if not name or name in seen_resolved:
            continue
        seen_resolved.add(name)
        names.append(name)
    return jsonify(names)


@app.route("/api/debug/albums")
def debug_albums():
    # TEMPORARY diagnostic route -- lists every album/single entry walked for an
    # artist, to see what's inflating the live track count. Not linked from the UI.
    artist = (request.args.get("artist") or "").strip()
    if not artist:
        return jsonify({"error": "artist is required"}), 400
    target = find_target_artist(artist)
    if not target:
        return jsonify({"error": "artist_not_found"}), 404
    name, artist_id = target

    artist_page = yt.get_artist(artist_id)
    album_entries = _collect_album_refs(artist_page, artist_id, "albums")
    single_entries = _collect_album_refs(artist_page, artist_id, "singles")

    def summarize(entries, kind):
        out = []
        for e in entries:
            out.append({
                "kind": kind,
                "title": e.get("title"),
                "year": e.get("year"),
                "browseId": e.get("browseId"),
            })
        return out

    popular = fetch_popular_tracks(artist_id)
    popular_summary = []
    for t in popular[:25]:
        popular_summary.append({
            "title": t["title"],
            "videoId": t["videoId"],
            "has_lyrics": bool(fetch_lyrics(t["videoId"])),
        })

    # Mirror fetch_songs(name, "top25") exactly (slice to 25 BEFORE dedup, same
    # order as fetch_songs), so this matches what /api/quiz/build actually iterates.
    deduped_top25 = _dedupe_tracks(popular[:25])
    deduped_summary = []
    for t in deduped_top25:
        deduped_summary.append({
            "title": t["title"],
            "videoId": t["videoId"],
            "has_lyrics": bool(fetch_lyrics(t["videoId"])),
        })

    return jsonify({
        "resolved_name": name,
        "album_count": len(album_entries),
        "single_count": len(single_entries),
        "albums": summarize(album_entries, "album"),
        "singles": summarize(single_entries, "single"),
        "popular_tracks_first25": popular_summary,
        "deduped_top25_pool_size": len(deduped_top25),
        "deduped_top25": deduped_summary,
    })


@app.route("/api/quiz/build", methods=["POST"])
def build_quiz():
    data = request.get_json(force=True) or {}
    artist = (data.get("artist") or "").strip()
    count_raw = data.get("count", "all")
    count = None if count_raw in (None, "all") else int(count_raw)
    difficulty = "hard" if data.get("difficulty") == "hard" else "normal"
    scope_raw = data.get("scope")
    scope = scope_raw if scope_raw in ("top50", "top25") else "all"

    if not artist:
        return jsonify({"error": "artist is required"}), 400

    questions = build_questions(artist, count, difficulty, scope)
    if not questions:
        return jsonify({"error": "no_quiz_available"}), 404
    return jsonify({"artist": artist, "questions": questions})


if __name__ == "__main__":
    app.run(debug=True, port=5050, threaded=True)
