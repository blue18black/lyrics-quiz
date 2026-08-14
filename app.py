import base64
import random
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, jsonify, render_template, request
from ytmusicapi import YTMusic

import deezer

app = Flask(__name__)
# English locale is used for all catalog/track fetching -- Japanese-artist track
# titles come through in Japanese either way (with a romanized " - romaji" suffix
# that deezer.clean_title() strips), but Japanese locale makes YT Music surface extra
# katakana-transliterated duplicate tracks for non-Japanese artists (e.g. "Blinding
# Lights" also appearing as "ブラインディング・ライツ"). Japanese locale is only used
# separately (yt_ja) for the artist-name suggestion dropdown, since English-locale
# search results romanize Japanese artist names (e.g. "Cho Tokimeki Sendenbu"
# instead of "超ときめき♡宣伝部").
yt = YTMusic()
yt_ja = YTMusic(language="ja")


# ---- Song discovery: Deezer for the catalog/ranking, YT Music for lyrics ----
#
# Deezer (deezer.py) finds and ranks the artist's songs -- see that module's
# docstring for why (no MV/live-video mixing, a per-track popularity rank
# covering the whole catalog). Neither Deezer's nor iTunes' public API expose
# lyrics text at all, so lyrics still have to come from YT Music: each
# Deezer-ranked track is searched for on YT Music (by title + the single/
# album/EP it's released on, then by title + performing artist name) to find
# the matching video, and fetch_lyrics() is called on that.

_YTMUSIC_AUDIO_ONLY_TYPE = "MUSIC_VIDEO_TYPE_ATV"


def _yt_search_songs(query: str, limit: int = 8) -> list[dict]:
    try:
        return yt.search(query, filter="songs", limit=limit)
    except Exception:
        return []


def _yt_search_albums(query: str, limit: int = 3) -> list[dict]:
    try:
        return yt.search(query, filter="albums", limit=limit)
    except Exception:
        return []


def _yt_get_album_tracks(browse_id: str) -> list[dict]:
    try:
        return yt.get_album(browse_id).get("tracks") or []
    except Exception:
        return []


def _search_ytmusic_video(search_title: str, query_suffix: str) -> tuple[str, str, str] | None:
    """Search "search_title query_suffix" on YT Music and return (videoId, its own
    cleaned title, album name) of the first audio-only (ATV) candidate whose title
    matches. None if nothing matches."""
    for c in _yt_search_songs(f"{search_title} {query_suffix}"):
        if c.get("videoType") != _YTMUSIC_AUDIO_ONLY_TYPE:
            continue
        song_title = deezer.clean_title(c.get("title") or "")
        if deezer._has_unwanted_keyword(song_title):
            continue
        if deezer._search_titles_match(search_title, song_title):
            return c.get("videoId"), song_title, (c.get("album") or {}).get("name")
    return None


def _search_ytmusic_video_by_album_browse(search_title: str, album_title: str | None, artist_name: str | None) -> tuple[str, str] | None:
    """Last resort for a song that never shows up in filter="songs" results at all:
    find the one matching album via filter="albums" and scan its own track list."""
    if not album_title:
        return None
    query = f"{artist_name} {album_title}" if artist_name else album_title
    for album in _yt_search_albums(query):
        browse_id = album.get("browseId")
        if not browse_id:
            continue
        for c in _yt_get_album_tracks(browse_id):
            if c.get("videoType") != _YTMUSIC_AUDIO_ONLY_TYPE:
                continue
            song_title = deezer.clean_title(c.get("title") or "")
            if deezer._has_unwanted_keyword(song_title):
                continue
            if deezer._search_titles_match(search_title, song_title):
                return c.get("videoId"), song_title
    return None


def _query_suffixes_for(album_title: str | None, artist_name: str | None) -> list[tuple[str, str]]:
    bare_artist_name = re.sub(r"[\(\[（【].*?[\)\]）】]", "", artist_name or "").strip()
    artist_suffix = bare_artist_name or artist_name
    suffixes = []
    if album_title:
        if "/" in album_title:
            for part in album_title.split("/"):
                part = part.strip()
                if part:
                    suffixes.append(("album", part))
        suffixes.append(("album", album_title))
    if artist_suffix:
        suffixes.append(("artist", artist_suffix))
    return suffixes


def _fallback_search_titles(title: str) -> list[str]:
    titles = [title]
    base = deezer._NEW_VER_RE.sub("", deezer.strip_trailing_version_suffix(title)).strip()
    if base and base != title:
        titles.append(base)
    return titles


def _find_ytmusic_video_for_track(track: dict) -> tuple[str, str] | None:
    """Find the (videoId, title) matching a Deezer-ranked track on YT Music, by
    title + the single/album/EP it was released on (most reliable -- that's the
    same release Deezer's ranking refers to), then title + performing artist name,
    then (if the track has no native-script title) the romanized/English title,
    then finally by browsing the one matching album's own track list directly.
    The returned title is YT Music's own (typically native-script Japanese), used
    for display instead of Deezer's -- Deezer's title_short is sometimes only the
    romanized/English form for an otherwise Japanese-titled song."""
    title = track.get("title")
    if not title:
        return None
    album_title = track.get("album")
    artist_name = track.get("artist")
    plain_album_title = track.get("plain_album_title")
    plain_artist_name = track.get("plain_artist")

    default_suffixes = _query_suffixes_for(album_title, artist_name)
    plain_suffixes = _query_suffixes_for(plain_album_title, plain_artist_name) or default_suffixes
    for i, search_title in enumerate(_fallback_search_titles(title)):
        query_suffixes = default_suffixes if i == 0 else plain_suffixes
        for _, query_suffix in query_suffixes:
            result = _search_ytmusic_video(search_title, query_suffix)
            if result:
                return result[0], result[1]

    english_title = track.get("english_title")
    if english_title:
        for _, query_suffix in default_suffixes:
            result = _search_ytmusic_video(english_title, query_suffix)
            if result:
                return result[0], result[1]

    for candidate_album in (album_title, plain_album_title):
        matched = _search_ytmusic_video_by_album_browse(title, candidate_album, artist_name)
        if matched:
            return matched
    return None


def fetch_songs(artist: str, scope: str = "all") -> tuple[str, list[dict]] | None:
    """Resolve an artist via Deezer and return (canonical_name, tracks), tracks
    staying in Deezer-rank order (most popular first). scope="top25"/"top50" is
    applied by the caller (build_questions), which also backfills past the nominal
    cutoff if too many of the top tier don't have a usable YT Music match."""
    result = deezer.get_ranked_tracks(artist)
    if not result or len(result["tracks"]) < 4:
        return None
    return result["artistName"], result["tracks"]


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

    # A burst of concurrent requests to YT Music's (unofficial) API appears to
    # get some fraction of them transiently rejected -- retrying once after a
    # short pause recovers a lot of those.
    for attempt in range(2):
        try:
            watch_playlist = yt.get_watch_playlist(video_id)
            lyrics_browse_id = watch_playlist.get("lyrics")
            if not lyrics_browse_id:
                _LYRICS_CACHE[video_id] = None
                return None
            lyrics = yt.get_lyrics(lyrics_browse_id)["lyrics"]
            _LYRICS_CACHE[video_id] = lyrics
            return lyrics
        except Exception:
            if attempt == 0:
                time.sleep(0.6)
    return None


_SCOPE_NOMINAL_SIZE = {"top25": 25, "top50": 50}


def build_questions(
    artist: str,
    count: int | None,
    difficulty: str = "normal",
    scope: str = "all",
    on_progress=None,
) -> list[dict]:
    """count=None means "as many as we can find". difficulty="hard" shows a single
    lyric line instead of 2-4, making the source song harder to guess. scope="top50"/
    "top25" limits the song pool to the artist's most popular tracks (by Deezer's
    per-track rank). on_progress(current, total), if given, is called after each
    song's YT Music match + lyrics lookup finishes (progress reporting)."""
    min_lines, max_lines = (1, 1) if difficulty == "hard" else (2, 4)

    resolved = fetch_songs(artist, scope)
    if not resolved:
        return []
    _, songs = resolved

    # Deezer's title_short is sometimes only the romanized/English form for an
    # otherwise Japanese-titled song -- once a track's matching YT Music video is
    # found, its own (typically native-script) title is used for display instead.
    # Keyed by the track dict's id() so distractor titles pick up the same
    # resolution as the correct answer wherever the same song is both.
    resolved_titles: dict[int, str] = {}

    def display_title(song):
        return resolved_titles.get(id(song), song["title"])

    progress_lock = threading.Lock()
    progress_state = {"current": 0, "total": len(songs)}

    def build_one(song):
        match = _find_ytmusic_video_for_track(song)
        result = None
        if match:
            video_id, yt_title = match
            lyrics = fetch_lyrics(video_id)
            if lyrics:
                resolved_titles[id(song)] = yt_title
                snippet = extract_snippet(lyrics, yt_title, min_lines=min_lines, max_lines=max_lines)
                if snippet:
                    result = (song, snippet)
        if on_progress:
            with progress_lock:
                progress_state["current"] += 1
                on_progress(progress_state["current"], progress_state["total"])
        return result

    # Matching each song on YT Music + fetching its lyrics is the slow part (up to
    # a few network calls per song), and doing it one song at a time was slow
    # enough that a large catalog (scope="all" can mean 100+ candidate songs) hit
    # the request timeout before finishing. Fetch concurrently instead -- these
    # are independent lookups.
    nominal = _SCOPE_NOMINAL_SIZE.get(scope)
    if nominal:
        # "全問" (count=None) within a ranked scope means "the full top N" --
        # i.e. Top25 + all questions should aim for exactly 25, not whatever
        # the top 25 alone happens to yield.
        if count is None:
            count = nominal
        # songs stays in Deezer-rank order. Try the nominal top N first (that's
        # what "top 25/50" should mean); only reach past rank N if too few of
        # them have a usable YT Music match to satisfy the requested question
        # count, rather than just falling short of it.
        primary, backfill = songs[:nominal], songs[nominal:]
    else:
        primary, backfill = songs, []
    progress_state["total"] = len(primary)

    with ThreadPoolExecutor(max_workers=8) as pool:
        found = [r for r in pool.map(build_one, primary) if r]

    if count is not None and len(found) < count and backfill:
        progress_state["current"] = 0
        progress_state["total"] = len(backfill)
        with ThreadPoolExecutor(max_workers=8) as pool:
            found += [r for r in pool.map(build_one, backfill) if r]

    random.shuffle(found)  # presentation order, independent of rank

    all_titles = [display_title(s) for s in songs]

    questions = []
    for song, snippet in found:
        if count is not None and len(questions) >= count:
            break

        title = display_title(song)
        distractor_pool = [t for t in all_titles if t != title]
        if len(distractor_pool) < 3:
            continue
        distractors = random.sample(distractor_pool, 3)
        choices = distractors + [title]
        random.shuffle(choices)

        questions.append({
            "snippet": snippet,
            "choices": choices,
            # base64'd, not plaintext -- the answer is still technically visible to
            # anyone who inspects the network response, but this at least keeps it
            # from being readable at a glance.
            "a": base64.b64encode(title.encode("utf-8")).decode("ascii"),
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
    # every candidate through resolve_suggestion_name below, which collapses both
    # cases down to the actual artist name.
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


@app.route("/api/debug/album")
def debug_album():
    # TEMPORARY diagnostic route -- fetches one known album directly to check
    # whether Accept-Language actually affects Deezer's localization from this
    # server's network origin. Not linked from the UI.
    data = deezer._get(deezer._DEEZER_API_BASE, "/album/59626242/tracks", {"limit": 5})
    return jsonify(data)


@app.route("/api/debug/list")
def debug_list():
    # TEMPORARY diagnostic route -- lists every track Deezer/iTunes discovery
    # returned for an artist, to check for duplicates/junk. Not linked from the UI.
    artist = (request.args.get("artist") or "").strip()
    if not artist:
        return jsonify({"error": "artist is required"}), 400
    result = deezer.get_ranked_tracks(artist)
    if not result:
        return jsonify({"error": "artist_not_found"}), 404
    return jsonify({
        "artistName": result["artistName"],
        "count": len(result["tracks"]),
        "tracks": [
            {
                "rank": t["rank"], "title": t["title"], "artist": t["artist"], "album": t["album"],
                "isrc": t.get("isrc"), "src_id": t.get("src_id"),
            }
            for t in result["tracks"]
        ],
    })


# Building a quiz can take anywhere from a few seconds to well over a minute
# (matching each candidate song against YT Music + fetching its lyrics), so
# /api/quiz/build kicks the work off in a background thread and returns
# immediately with a job id; the frontend polls /api/quiz/progress/<job_id> to
# show a live "X/Y songs checked" progress bar and pick up the result once done.
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_MAX_JOBS = 50


def _run_build_job(job_id: str, artist: str, count: int | None, difficulty: str, scope: str) -> None:
    def on_progress(current, total):
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job:
                job["current"] = current
                job["total"] = total

    try:
        questions = build_questions(artist, count, difficulty, scope, on_progress=on_progress)
    except Exception:
        questions = None

    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return  # evicted (see _MAX_JOBS) before finishing
        if questions:
            job["status"] = "done"
            job["artist"] = artist
            job["questions"] = questions
        else:
            job["status"] = "error"


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

    job_id = uuid.uuid4().hex
    with _JOBS_LOCK:
        _JOBS[job_id] = {"status": "running", "current": 0, "total": 0}
        if len(_JOBS) > _MAX_JOBS:
            del _JOBS[next(iter(_JOBS))]  # dicts preserve insertion order -- evict oldest

    threading.Thread(
        target=_run_build_job, args=(job_id, artist, count, difficulty, scope), daemon=True
    ).start()
    return jsonify({"job_id": job_id})


@app.route("/api/quiz/progress/<job_id>")
def quiz_progress(job_id):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        return jsonify({"error": "job_not_found"}), 404
    response = {"status": job["status"], "current": job["current"], "total": job["total"]}
    if job["status"] == "done":
        response["artist"] = job["artist"]
        response["questions"] = job["questions"]
    return jsonify(response)


if __name__ == "__main__":
    app.run(debug=True, port=5050, threaded=True)
