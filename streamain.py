"""
Streamain client: log in (through Cloudflare), scrape the user's video list,
and resolve each video's direct CDN mp4 URL.

Design notes:
  - Uses cloudscraper to transparently solve Cloudflare's basic JS challenge.
  - Credentials come from environment variables, never hard-coded:
        STREAMAIN_EMAIL, STREAMAIN_PASSWORD
  - Optional fallback if login is blocked by a managed/Turnstile challenge:
        STREAMAIN_COOKIE   -> raw "Cookie:" header string copied from a browser
                              (e.g. "cf_clearance=...; playbob_user_session=...; XSRF-TOKEN=...")
  - Results are cached in memory with a TTL so we don't log in on every request.

This module makes NO assumptions that it runs locally — it is meant to run on a
cloud host (Render) with unrestricted outbound internet access.
"""

from __future__ import annotations

import os
import re
import time
import base64
import threading
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Optional
from urllib.parse import urljoin, quote

import cloudscraper
from bs4 import BeautifulSoup

BASE_URL = "https://streamain.com"
LOGIN_URL = f"{BASE_URL}/en/login"
VIDEOS_URL = f"{BASE_URL}/en/user/videos"
EMBED_URL = f"{BASE_URL}/embed/{{video_id}}"

# Stremio's own free metadata addon — resolves a title to its IMDB id.
CINEMETA_BASE = "https://v3-cinemeta.strem.io"
# Minimum fuzzy-match confidence before we let a video hijack a real title.
MATCH_THRESHOLD = float(os.environ.get("STREAMAIN_MATCH_THRESHOLD", "0.6"))

# How long (seconds) a scraped library stays fresh before we re-scrape.
CACHE_TTL = int(os.environ.get("STREAMAIN_CACHE_TTL", "1800"))  # 30 min

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)

# A video ID looks like "GyJWNvc59kpxc8g" in the sample HTML.
_ID_RE = re.compile(r"^[A-Za-z0-9]{8,}$")

# Match a direct CDN mp4 URL anywhere in an embed page (HTML or inline JS).
_MP4_RE = re.compile(
    r"https?://cdn\.streamain\.com/[^\s\"'<>\\]+?\.mp4[^\s\"'<>\\]*",
    re.IGNORECASE,
)

# --- Release-name parsing (title -> movie/series metadata) ---------------
# Season/episode patterns, tried in order: s01e04 / s1 e4 / s01.e04, 1x04, season 1 episode 4
_SERIES_RES = [
    re.compile(r"(?i)\bs(\d{1,2})\s*[.\-_ ]?\s*e(\d{1,3})\b"),
    re.compile(r"(?i)\b(\d{1,2})\s*x\s*(\d{1,3})\b"),
    re.compile(r"(?i)\bseason\s*(\d{1,2})\s*episode\s*(\d{1,3})\b"),
]
_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
# Common release/quality junk to strip from a parsed title before matching.
_JUNK_RE = re.compile(
    r"(?i)\b(?:"
    r"1080p|720p|2160p|480p|4k|x264|x265|h264|h265|hevc|xvid|"
    r"bluray|blu-ray|brrip|bdrip|webrip|web-dl|webdl|web|hdrip|dvdrip|hdtv|"
    r"aac|ac3|dts|dd5\.1|10bit|hdr|remux|proper|repack|extended|uncut|"
    r"internal|amzn|nf|hmax|dsnp|multi|dual|subbed|dubbed|complete"
    r")\b"
)


def _clean_name(text: str) -> str:
    """Strip junk tags, brackets and separators from a parsed title."""
    text = _JUNK_RE.sub(" ", text)
    text = re.sub(r"[\[\]\(\)\{\}]", " ", text)
    text = re.sub(r"[-_.]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_release_title(raw: str) -> dict:
    """Parse an arbitrary upload title into movie/series metadata.

    Handles formats like:
        Inception (2010) | Inception.2010.1080p.x264 | the dark knight 2008
        Gachiakuta s01e04 | breaking.bad.S05E14 | Show 1x04 | Show season 1 episode 4
    Returns a dict with kind = "movie" | "series" | "none".
    """
    if not raw:
        return {"kind": "none"}
    norm = re.sub(r"[._]+", " ", raw.strip())
    norm = re.sub(r"\s+", " ", norm).strip()

    # Series takes priority — an SxxEyy marker is a strong signal.
    for rx in _SERIES_RES:
        m = rx.search(norm)
        if m:
            name = _clean_name(norm[: m.start()])
            if name:
                return {
                    "kind": "series",
                    "name": name,
                    "year": None,
                    "season": int(m.group(1)),
                    "episode": int(m.group(2)),
                }

    # Movie — use the last 4-digit year as the title/year boundary if present.
    years = list(_YEAR_RE.finditer(norm))
    if years:
        m = years[-1]
        name = _clean_name(norm[: m.start()])
        if name:
            return {
                "kind": "movie",
                "name": name,
                "year": int(m.group(1)),
                "season": None,
                "episode": None,
            }

    # Fallback: treat the whole (cleaned) string as a movie title with no year.
    name = _clean_name(norm)
    if name:
        return {"kind": "movie", "name": name, "year": None, "season": None, "episode": None}
    return {"kind": "none"}


class CinemetaResolver:
    """Resolve a parsed title to an IMDB id via Cinemeta's search catalog.

    Results are cached in memory (they never change) so repeated library
    refreshes don't re-hit Cinemeta.
    """

    def __init__(self) -> None:
        self._scraper = cloudscraper.create_scraper()
        self._cache: dict[str, Optional[str]] = {}
        self._lock = threading.Lock()

    def resolve(self, kind: str, name: str, year: Optional[int]) -> Optional[str]:
        ctype = "series" if kind == "series" else "movie"
        key = f"{ctype}|{name.lower()}|{year or ''}"
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        imdb = self._search(ctype, name, year)
        with self._lock:
            self._cache[key] = imdb
        return imdb

    def _search(self, ctype: str, name: str, year: Optional[int]) -> Optional[str]:
        try:
            url = f"{CINEMETA_BASE}/catalog/{ctype}/top/search={quote(name)}.json"
            resp = self._scraper.get(url, timeout=20)
            if resp.status_code != 200:
                return None
            metas = resp.json().get("metas", [])
        except Exception:
            return None

        target = name.lower()
        best_id: Optional[str] = None
        best_score = 0.0
        for meta in metas:
            mid = meta.get("id", "")
            if not mid.startswith("tt"):
                continue
            score = SequenceMatcher(None, target, (meta.get("name") or "").lower()).ratio()
            if year and str(year) in str(meta.get("releaseInfo") or ""):
                score += 0.15
            if score > best_score:
                best_score = score
                best_id = mid
        return best_id if best_id and best_score >= MATCH_THRESHOLD else None


class StreamainError(RuntimeError):
    """Raised when scraping fails in a way the caller should surface."""


class CloudflareChallenge(StreamainError):
    """Raised when Cloudflare blocks automated access and a cookie fallback is needed."""


@dataclass
class Video:
    id: str
    title: str
    stream_url: str = ""
    poster: str = ""
    imdb_id: str = ""
    season: Optional[int] = None
    episode: Optional[int] = None

    @property
    def encoded_id(self) -> str:
        """Stremio-safe id, prefixed so Nuvio routes it to this addon."""
        raw = base64.urlsafe_b64encode(self.id.encode("utf-8")).decode("ascii")
        return f"streamain:{raw}"

    @staticmethod
    def decode_id(encoded: str) -> str:
        raw = encoded[len("streamain:"):] if encoded.startswith("streamain:") else encoded
        # base64 may be URL-decoded already; pad before decoding.
        padded = raw + "=" * (-len(raw) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")


@dataclass
class _Cache:
    videos: dict[str, Video] = field(default_factory=dict)
    # Auto-built prank maps: real IMDB id -> the personal video that hijacks it.
    movie_map: dict[str, Video] = field(default_factory=dict)
    series_map: dict[str, Video] = field(default_factory=dict)  # key: "tt123:S:E"
    fetched_at: float = 0.0

    def fresh(self) -> bool:
        return bool(self.videos) and (time.time() - self.fetched_at) < CACHE_TTL


class StreamainClient:
    def __init__(
        self,
        email: Optional[str] = None,
        password: Optional[str] = None,
        cookie: Optional[str] = None,
    ) -> None:
        self.email = email or os.environ.get("STREAMAIN_EMAIL", "")
        self.password = password or os.environ.get("STREAMAIN_PASSWORD", "")
        self.cookie = cookie or os.environ.get("STREAMAIN_COOKIE", "")
        self._cache = _Cache()
        self._lock = threading.Lock()
        self._scraper = None
        self._resolver = CinemetaResolver()

    # -- session ----------------------------------------------------------
    def _new_scraper(self):
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "darwin", "mobile": False}
        )
        scraper.headers.update({"User-Agent": USER_AGENT})
        if self.cookie:
            scraper.headers.update({"Cookie": self.cookie})
        return scraper

    def _ensure_session(self):
        """Return a logged-in scraper session, logging in if necessary."""
        if self._scraper is not None:
            return self._scraper

        scraper = self._new_scraper()

        # If a raw cookie was supplied we assume it already carries a valid
        # session and skip the credential login entirely.
        if self.cookie:
            self._scraper = scraper
            return scraper

        if not (self.email and self.password):
            raise StreamainError(
                "No credentials: set STREAMAIN_EMAIL and STREAMAIN_PASSWORD "
                "(or STREAMAIN_COOKIE) in the environment."
            )

        # 1) GET the login page to obtain the CSRF token + cookies.
        try:
            resp = scraper.get(LOGIN_URL, timeout=30)
        except Exception as exc:  # network / cloudflare hard failure
            raise CloudflareChallenge(f"Could not load login page: {exc}") from exc

        if resp.status_code in (403, 503) and "cf" in resp.text.lower():
            raise CloudflareChallenge(
                "Cloudflare blocked the login page. Provide STREAMAIN_COOKIE as a fallback."
            )

        token = self._extract_csrf(resp.text)

        # 2) POST credentials. Field names follow the Laravel/Vironeer login form.
        payload = {
            "_token": token or "",
            "email": self.email,
            "password": self.password,
            "remember": "on",
        }
        headers = {"Referer": LOGIN_URL, "Origin": BASE_URL}
        try:
            login_resp = scraper.post(
                LOGIN_URL, data=payload, headers=headers, timeout=30, allow_redirects=True
            )
        except Exception as exc:
            raise CloudflareChallenge(f"Login request failed: {exc}") from exc

        # A successful login usually redirects to the dashboard; the login form
        # disappears from the resulting HTML.
        if 'name="password"' in login_resp.text and "/user" not in login_resp.url:
            raise StreamainError(
                "Login failed — check STREAMAIN_EMAIL / STREAMAIN_PASSWORD "
                "(or Cloudflare may require STREAMAIN_COOKIE)."
            )

        self._scraper = scraper
        return scraper

    @staticmethod
    def _extract_csrf(html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        meta = soup.find("meta", attrs={"name": "csrf-token"})
        if meta and meta.get("content"):
            return meta["content"]
        hidden = soup.find("input", attrs={"name": "_token"})
        if hidden and hidden.get("value"):
            return hidden["value"]
        return ""

    # -- scraping ---------------------------------------------------------
    def _scrape_video_list(self, scraper) -> list[Video]:
        resp = scraper.get(VIDEOS_URL, timeout=30)
        if resp.status_code != 200:
            raise StreamainError(f"My Videos page returned HTTP {resp.status_code}")

        soup = BeautifulSoup(resp.text, "html.parser")
        videos: dict[str, Video] = {}

        # Preferred: the Share dropdown carries a JSON blob with filename + links.
        for tag in soup.select("[data-share]"):
            share = tag.get("data-share", "")
            vid = self._id_from_share(share)
            title = self._title_from_share(share)
            if vid and vid not in videos:
                videos[vid] = Video(id=vid, title=title or vid)

        # Fallback: derive ids + titles from the edit/watch links.
        if not videos:
            for a in soup.select("a[href]"):
                href = a["href"]
                m = re.search(r"/user/videos/([A-Za-z0-9]+)/edit", href)
                if not m:
                    m = re.search(r"/en/([A-Za-z0-9]+)/watch", href)
                if m:
                    vid = m.group(1)
                    if _ID_RE.match(vid) and vid not in videos:
                        title = a.get_text(strip=True) or vid
                        videos[vid] = Video(id=vid, title=title)

        return list(videos.values())

    @staticmethod
    def _id_from_share(share: str) -> str:
        m = re.search(r"/embed/([A-Za-z0-9]+)", share)
        if not m:
            m = re.search(r"/en/([A-Za-z0-9]+)/watch", share)
        return m.group(1) if m else ""

    @staticmethod
    def _title_from_share(share: str) -> str:
        m = re.search(r'"filename"\s*:\s*"([^"]+)"', share)
        return m.group(1) if m else ""

    def _resolve_stream(self, scraper, video: Video) -> None:
        """Fill video.stream_url by scraping its embed page.

        The embed player stores the direct CDN mp4 in the video element's
        data-link attribute, e.g.:
            <video id="playbob-video"
                   data-link="https://cdn.streamain.com/users/.../file.mp4"></video>
        """
        url = EMBED_URL.format(video_id=video.id)
        resp = scraper.get(url, headers={"Referer": BASE_URL + "/"}, timeout=30)
        if resp.status_code != 200:
            return

        soup = BeautifulSoup(resp.text, "html.parser")

        # 1) Preferred: the player's data-link attribute.
        el = soup.find(id="playbob-video") or soup.find("video")
        if el and el.get("data-link"):
            video.stream_url = urljoin(url, el["data-link"])
            return

        # 2) Fallback: any cdn.streamain.com mp4 anywhere in the page/JS.
        match = _MP4_RE.search(resp.text)
        if match:
            video.stream_url = match.group(0)
            return

        # 3) Last resort: a classic <source> tag.
        source = soup.find("source")
        if source and source.get("src"):
            video.stream_url = urljoin(url, source["src"])

    # -- public API -------------------------------------------------------
    def get_library(self, force: bool = False) -> dict[str, Video]:
        """Return {encoded_id: Video}, using the in-memory cache when fresh."""
        with self._lock:
            if not force and self._cache.fresh():
                return self._cache.videos

            scraper = self._ensure_session()
            videos = self._scrape_video_list(scraper)
            for v in videos:
                if not v.stream_url:
                    try:
                        self._resolve_stream(scraper, v)
                    except Exception:
                        # Leave stream_url empty; the addon will skip it gracefully.
                        pass

            library = {v.encoded_id: v for v in videos}
            movie_map, series_map = self._build_prank_maps(videos)
            self._cache = _Cache(
                videos=library,
                movie_map=movie_map,
                series_map=series_map,
                fetched_at=time.time(),
            )
            return library

    def _build_prank_maps(self, videos: list[Video]) -> tuple[dict, dict]:
        """Parse each video title and auto-map it onto a real movie/episode."""
        movie_map: dict[str, Video] = {}
        series_map: dict[str, Video] = {}
        for v in videos:
            if not v.stream_url:
                continue
            parsed = parse_release_title(v.title)
            kind = parsed.get("kind")
            if kind not in ("movie", "series"):
                continue
            imdb = self._resolver.resolve(kind, parsed["name"], parsed.get("year"))
            if not imdb:
                continue
            v.imdb_id = imdb
            if kind == "movie":
                movie_map[imdb] = v
            else:
                v.season = parsed["season"]
                v.episode = parsed["episode"]
                series_map[f"{imdb}:{parsed['season']}:{parsed['episode']}"] = v
        return movie_map, series_map

    def get_video(self, encoded_id: str) -> Optional[Video]:
        return self.get_library().get(encoded_id)

    def get_prank_movie(self, imdb_id: str) -> Optional[Video]:
        self.get_library()  # ensure cache/maps are populated
        return self._cache.movie_map.get(imdb_id)

    def get_prank_series(self, imdb_id: str, season: int, episode: int) -> Optional[Video]:
        self.get_library()
        return self._cache.series_map.get(f"{imdb_id}:{season}:{episode}")

    def get_mappings(self) -> dict:
        """Return a JSON-friendly summary of every auto-built prank mapping."""
        self.get_library()  # ensure cache/maps are populated
        movies = [
            {"imdb_id": imdb, "video_title": v.title, "video_id": v.id,
             "resolved": bool(v.stream_url)}
            for imdb, v in self._cache.movie_map.items()
        ]
        series = [
            {"id": key, "imdb_id": v.imdb_id, "season": v.season, "episode": v.episode,
             "video_title": v.title, "video_id": v.id, "resolved": bool(v.stream_url)}
            for key, v in self._cache.series_map.items()
        ]
        mapped_ids = {v.id for v in self._cache.movie_map.values()} | {
            v.id for v in self._cache.series_map.values()
        }
        unmapped = [
            {"video_title": v.title, "video_id": v.id, "resolved": bool(v.stream_url)}
            for v in self._cache.videos.values()
            if v.id not in mapped_ids
        ]
        return {"movies": movies, "series": series, "unmapped": unmapped}
