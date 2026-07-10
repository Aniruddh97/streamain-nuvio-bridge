"""
Streamain -> Nuvio/Stremio bridge (Flask addon).

Exposes the Stremio addon protocol so Nuvio can list and play your personal
Streamain videos:

    GET /manifest.json
    GET /catalog/movie/streamain.json
    GET /meta/movie/<id>.json
    GET /stream/movie/<id>.json

Playback strategy: the /stream response hands the player the direct
cdn.streamain.com mp4 URL plus a Referer proxy-header hint, so Nuvio streams
the bytes straight from Cloudflare (no proxying through this app).

Run locally:
    STREAMAIN_EMAIL=... STREAMAIN_PASSWORD=... flask --app app run
Run in production (Render):
    gunicorn app:app
"""

from __future__ import annotations

from flask import Flask, jsonify, Response

from streamain import StreamainClient, Video, StreamainError

app = Flask(__name__)
client = StreamainClient()

MANIFEST = {
    "id": "org.streamain.bridge",
    "version": "1.1.0",
    "name": "Streamain",
    "description": "Your personal Streamain videos, in Nuvio/Stremio.",
    "logo": "https://streamain.com/images/favicon.png",
    # catalog/meta only serve our own ids; stream also answers real IMDB ids
    # so a mapped movie/episode plays a personal video instead.
    "resources": [
        {"name": "catalog", "types": ["movie"], "idPrefixes": ["streamain:"]},
        {"name": "meta", "types": ["movie"], "idPrefixes": ["streamain:"]},
        {"name": "stream", "types": ["movie", "series"], "idPrefixes": ["tt", "streamain:"]},
    ],
    "types": ["movie", "series"],
    "idPrefixes": ["tt", "streamain:"],
    "catalogs": [{"type": "movie", "id": "streamain", "name": "My Streamain Videos"}],
}


def _cors(resp: Response) -> Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


def _meta(video: Video) -> dict:
    return {
        "id": video.encoded_id,
        "type": "movie",
        "name": video.title,
        "poster": video.poster or None,
        "posterShape": "poster",
        "background": video.poster or None,
    }


def _stream_response(video: Video | None) -> Response:
    """Build a Stremio stream reply, or an empty list if nothing maps."""
    if not video or not video.stream_url:
        return jsonify({"streams": []})
    return jsonify(
        {
            "streams": [
                {
                    "url": video.stream_url,
                    "name": "Streamain",
                    "title": video.title,
                    "behaviorHints": {
                        "notWebReady": False,
                        "proxyHeaders": {
                            "request": {"Referer": "https://streamain.com/"}
                        },
                    },
                }
            ]
        }
    )


@app.after_request
def add_cors(resp: Response) -> Response:
    return _cors(resp)


@app.route("/")
def index() -> Response:
    return _cors(jsonify({"status": "ok", "manifest": "/manifest.json"}))


@app.route("/manifest.json")
def manifest() -> Response:
    return jsonify(MANIFEST)


@app.route("/debug/mappings")
def debug_mappings() -> Response:
    """Show which personal videos auto-mapped onto which real movies/episodes."""
    try:
        return _cors(jsonify(client.get_mappings()))
    except StreamainError as exc:
        return _cors(jsonify({"error": str(exc)}))


@app.route("/catalog/movie/streamain.json")
def catalog() -> Response:
    try:
        library = client.get_library()
    except StreamainError as exc:
        return _cors(jsonify({"metas": [], "error": str(exc)}))
    metas = [_meta(v) for v in sorted(library.values(), key=lambda v: v.title.lower())]
    return jsonify({"metas": metas})


@app.route("/meta/movie/<path:raw_id>.json")
def meta(raw_id: str) -> Response:
    encoded = raw_id if raw_id.startswith("streamain:") else f"streamain:{raw_id}"
    video = client.get_video(encoded)
    if not video:
        return jsonify({"meta": None})
    return jsonify({"meta": _meta(video)})


@app.route("/stream/movie/<path:raw_id>.json")
def stream_movie(raw_id: str) -> Response:
    # Real movie hijack: Nuvio asks for an IMDB id we auto-mapped a video onto.
    if raw_id.startswith("tt"):
        return _stream_response(client.get_prank_movie(raw_id))
    # Personal video played from our own catalog.
    encoded = raw_id if raw_id.startswith("streamain:") else f"streamain:{raw_id}"
    return _stream_response(client.get_video(encoded))


@app.route("/stream/series/<path:raw_id>.json")
def stream_series(raw_id: str) -> Response:
    # Series ids arrive as "tt1234567:<season>:<episode>".
    parts = raw_id.split(":")
    if len(parts) != 3 or not parts[0].startswith("tt"):
        return jsonify({"streams": []})
    try:
        video = client.get_prank_series(parts[0], int(parts[1]), int(parts[2]))
    except ValueError:
        return jsonify({"streams": []})
    return _stream_response(video)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=7000)
