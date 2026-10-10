"""YouTube Music browsing for bulb_voice (search, genres & moods, song radio,
personal home/library) on top of ytmusicapi.

ytmusicapi's typed parsers break when YouTube mixes item types on a page
(2026-10-10: get_mood_playlists("Blues") crashed on a video among the
playlists), so pages are parsed here by walking the raw response and keeping
only items we understand. Everything returned is plain dicts:

    item  = {"kind": "song"|"video"|"playlist"|"album"|"artist",
             "id": videoId | playlistId | browseId,
             "title", "subtitle", "thumbnail", "duration"(s, songs/videos)}
    track = {"videoId", "title", "artist", "duration"(s)}

Signed in when a browser-headers file exists (created by the owner with
`ytmusicapi browser --file <AUTH_FILE>`); anonymous otherwise. All calls are
blocking (requests) — call them from a thread.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from ytmusicapi import YTMusic

log = logging.getLogger("bulb_voice")

MOODS_TTL = 24 * 3600


def _text(runs_obj) -> str:
    if not isinstance(runs_obj, dict):
        return ""
    return "".join(r.get("text", "") for r in runs_obj.get("runs", []))


def _thumb(renderer: dict) -> str:
    def walk(o):
        if isinstance(o, dict):
            if "thumbnails" in o and isinstance(o["thumbnails"], list) and o["thumbnails"]:
                return o["thumbnails"][-1].get("url", "")
            for v in o.values():
                u = walk(v)
                if u:
                    return u
        elif isinstance(o, list):
            for v in o:
                u = walk(v)
                if u:
                    return u
        return ""
    return walk(renderer.get("thumbnailRenderer") or renderer.get("thumbnail") or {})


def _secs(d) -> int:
    if isinstance(d, (int, float)):
        return int(d)
    try:
        parts = [int(p) for p in str(d).split(":")]
    except ValueError:
        return 0
    total = 0
    for p in parts:
        total = total * 60 + p
    return total


def _walk(o, key):
    if isinstance(o, dict):
        if key in o:
            yield o[key]
        for v in o.values():
            yield from _walk(v, key)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v, key)


def _item_from_two_row(r: dict) -> dict | None:
    """musicTwoRowItemRenderer (shelf cards) -> item, or None if unknown."""
    title = _text(r.get("title"))
    nav = r.get("navigationEndpoint", {})
    bid = nav.get("browseEndpoint", {}).get("browseId", "")
    vid = nav.get("watchEndpoint", {}).get("videoId", "")
    if bid.startswith("VL"):
        kind, iid = "playlist", bid[2:]
    elif bid.startswith("MPRE"):
        kind, iid = "album", bid
    elif bid.startswith("UC"):
        kind, iid = "artist", bid
    elif vid:
        kind, iid = "video", vid
    else:
        return None
    return {"kind": kind, "id": iid, "title": title, "subtitle": _text(r.get("subtitle")),
            "thumbnail": _thumb(r), "duration": 0}


def _item_from_list_row(r: dict) -> dict | None:
    """musicResponsiveListItemRenderer (song rows on shelves) -> song item."""
    vid = (r.get("playlistItemData") or {}).get("videoId", "")
    if not vid:
        return None
    cols = [_text(c.get("musicResponsiveListItemFlexColumnRenderer", {}).get("text"))
            for c in r.get("flexColumns", [])]
    return {"kind": "song", "id": vid, "title": cols[0] if cols else "",
            "subtitle": " · ".join(c for c in cols[1:] if c), "thumbnail": _thumb(r), "duration": 0}


def _shelf_items(shelf: dict) -> list[dict]:
    out = []
    for r in _walk(shelf, "musicTwoRowItemRenderer"):
        it = _item_from_two_row(r)
        if it:
            out.append(it)
    for r in _walk(shelf, "musicResponsiveListItemRenderer"):
        it = _item_from_list_row(r)
        if it:
            out.append(it)
    return out


def _artists(entry: dict) -> str:
    return ", ".join(a.get("name", "") for a in entry.get("artists") or [] if a.get("name"))


def _track(entry: dict) -> dict | None:
    vid = entry.get("videoId")
    if not vid:
        return None
    return {"videoId": vid, "title": entry.get("title") or "Unknown",
            "artist": _artists(entry), "duration": _secs(entry.get("duration_seconds")
                                                          or entry.get("duration") or entry.get("length") or 0)}


class YTM:
    def __init__(self, auth_file: Path):
        self.auth_file = Path(auth_file)
        self._client: YTMusic | None = None
        self._client_signed_in = False
        self._moods: tuple[float, dict] | None = None

    # -- client ---------------------------------------------------------------
    def client(self) -> YTMusic:
        want_auth = self.auth_file.exists()
        if self._client is None or want_auth != self._client_signed_in:
            if want_auth:
                try:
                    self._client = YTMusic(str(self.auth_file))
                    self._client_signed_in = True
                    log.info("YouTube Music: signed in (%s)", self.auth_file.name)
                    return self._client
                except Exception as e:  # malformed/expired headers file
                    log.warning("YouTube Music auth file unusable (%s); anonymous", e)
            self._client = YTMusic()
            self._client_signed_in = False
        return self._client

    @property
    def signed_in(self) -> bool:
        self.client()
        return self._client_signed_in

    # -- browsing -------------------------------------------------------------
    def search(self, query: str, kind: str = "songs", limit: int = 20) -> list[dict]:
        filt = kind if kind in ("songs", "videos", "albums", "playlists", "artists") else "songs"
        out = []
        for r in self.client().search(query, filter=filt, limit=limit):
            rt = r.get("resultType", filt.rstrip("s"))
            if rt in ("song", "video") and r.get("videoId"):
                out.append({"kind": rt, "id": r["videoId"], "title": r.get("title", ""),
                            "subtitle": _artists(r) + (f" · {r['album']['name']}" if r.get("album") else ""),
                            "thumbnail": (r.get("thumbnails") or [{}])[-1].get("url", ""),
                            "duration": _secs(r.get("duration_seconds") or r.get("duration") or 0)})
            elif rt == "album" and r.get("browseId"):
                out.append({"kind": "album", "id": r["browseId"], "title": r.get("title", ""),
                            "subtitle": _artists(r) + (f" · {r['year']}" if r.get("year") else ""),
                            "thumbnail": (r.get("thumbnails") or [{}])[-1].get("url", ""), "duration": 0})
            elif rt == "playlist" and (r.get("browseId") or r.get("playlistId")):
                pid = r.get("playlistId") or r["browseId"].removeprefix("VL")
                out.append({"kind": "playlist", "id": pid, "title": r.get("title", ""),
                            "subtitle": r.get("author", "") or r.get("itemCount", ""),
                            "thumbnail": (r.get("thumbnails") or [{}])[-1].get("url", ""), "duration": 0})
            elif rt == "artist" and r.get("browseId"):
                out.append({"kind": "artist", "id": r["browseId"], "title": r.get("artist", "") or r.get("title", ""),
                            "subtitle": "Artist", "thumbnail": (r.get("thumbnails") or [{}])[-1].get("url", ""),
                            "duration": 0})
        return out

    def moods(self) -> dict[str, list[dict]]:
        """{"Moods & moments": [{"title", "params"}], "Genres": [...]} (cached a day)."""
        if self._moods and time.time() - self._moods[0] < MOODS_TTL:
            return self._moods[1]
        cats = {sec: [{"title": c["title"], "params": c["params"]} for c in items]
                for sec, items in self.client().get_mood_categories().items()}
        self._moods = (time.time(), cats)
        return cats

    def mood_items(self, params: str) -> list[dict]:
        """Playlists/albums/videos on a genre or mood page (tolerant parser)."""
        raw = self.client()._send_request(
            "browse", {"browseId": "FEmusic_moods_and_genres_category", "params": params})
        items = [it for it in (_item_from_two_row(r) for r in _walk(raw, "musicTwoRowItemRenderer")) if it]
        order = {"playlist": 0, "album": 1, "video": 2, "artist": 3}
        return sorted(items, key=lambda i: order.get(i["kind"], 9))

    def home(self, limit: int = 8) -> list[dict]:
        """Personal shelves when signed in (Quick picks, mixes…), generic otherwise."""
        raw = self.client()._send_request("browse", {"browseId": "FEmusic_home"})
        shelves = []
        for sh in _walk(raw, "musicCarouselShelfRenderer"):
            title = _text(((sh.get("header") or {}).get("musicCarouselShelfBasicHeaderRenderer") or {}).get("title"))
            items = _shelf_items(sh.get("contents", []))
            if title and items:
                shelves.append({"title": title, "items": items[:20]})
            if len(shelves) >= limit:
                break
        return shelves

    def library(self) -> list[dict]:
        if not self.signed_in:
            return []
        out = []
        for p in self.client().get_library_playlists(limit=50):
            if p.get("playlistId"):
                out.append({"kind": "playlist", "id": p["playlistId"], "title": p.get("title", ""),
                            "subtitle": str(p.get("count", "")),
                            "thumbnail": (p.get("thumbnails") or [{}])[-1].get("url", ""), "duration": 0})
        return out

    # -- tracks to play ---------------------------------------------------------
    def playlist_tracks(self, playlist_id: str, limit: int = 50) -> list[dict]:
        pl = self.client().get_playlist(playlist_id, limit=limit)
        return [t for t in (_track(e) for e in pl.get("tracks", [])) if t][:limit]

    def album_tracks(self, browse_id: str) -> list[dict]:
        al = self.client().get_album(browse_id)
        artist = _artists(al)
        tracks = [t for t in (_track(e) for e in al.get("tracks", [])) if t]
        for t in tracks:
            t["artist"] = t["artist"] or artist
        return tracks

    def radio(self, video_id: str, limit: int = 25) -> list[dict]:
        """'More like this': YouTube Music's radio for a song (first entry = the song)."""
        wp = self.client().get_watch_playlist(videoId=video_id, radio=True, limit=limit)
        return [t for t in (_track(e) for e in wp.get("tracks", [])) if t][:limit]
