# Streamain → Nuvio/Stremio bridge

A tiny Flask [Stremio addon](https://stremio.github.io/stremio-addon-guide/) that
lists **your personal Streamain videos** and lets you play them in
**Nuvio / Stremio**. It also has an optional **auto-mapping "prank" mode**: name a
Streamain upload after a real movie/episode and opening that title in Nuvio plays
**your** video instead. Designed to run for free on **Render** with unrestricted
outbound internet (PythonAnywhere's free tier can't reach `streamain.com`).

## How it works

```
Nuvio ──manifest/catalog/meta/stream──▶ Flask addon (Render)
                                          │  logs in with cloudscraper
                                          │  scrapes /en/user/videos
                                          │  resolves /embed/<id> → cdn mp4
                                          │  parses titles → Cinemeta → IMDB id
Nuvio ──plays mp4 + Referer header────▶ cdn.streamain.com
```

- The addon **logs in with your Streamain email/password** (via `cloudscraper`
  to pass Cloudflare), scrapes your **My Videos** list, and resolves each
  video's direct `cdn.streamain.com/*.mp4` URL from its embed page.
- **Auto-mapping:** each video title is parsed (robust regex) into a movie or
  episode, resolved to an IMDB id via [Cinemeta](https://v3-cinemeta.strem.io),
  and mapped so real-title stream requests return your video. A fuzzy-match
  threshold stops genuine personal clips from hijacking a real title.
- Results are cached in memory for `STREAMAIN_CACHE_TTL` seconds (default 30 min).
- `/stream` returns the **direct CDN URL** plus a `Referer` proxy-header hint, so
  Nuvio streams straight from Cloudflare — no bytes proxied through this app.

## Deploy on Render (free)

1. **Push this folder to a Git repo** (GitHub/GitLab).
2. In Render: **New → Blueprint**, point it at the repo (it reads `render.yaml`).
   Or **New → Web Service** with:
   - Build: `pip install -r requirements.txt`
   - Start: `gunicorn app:app --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT`
   - Plan: **Free**
3. Set **Environment Variables** (never commit these):

   | Key | Value | Required |
   |---|---|---|
   | `STREAMAIN_EMAIL` | your Streamain login email | yes* |
   | `STREAMAIN_PASSWORD` | your Streamain password | yes* |
   | `STREAMAIN_COOKIE` | raw browser `Cookie:` header (`cf_clearance=...; playbob_user_session=...; XSRF-TOKEN=...`) | only if Cloudflare blocks login |
   | `STREAMAIN_CACHE_TTL` | library cache seconds (default `1800`) | no |
   | `STREAMAIN_MATCH_THRESHOLD` | fuzzy-match confidence `0`–`1` (default `0.6`) | no |

   \*If Cloudflare challenges the login from Render's datacenter IP, skip
   email/password and use `STREAMAIN_COOKIE` instead (log into streamain.com in a
   browser → DevTools → Network → copy the full `Cookie` header).
4. Deploy. Your addon URL is `https://<your-app>.onrender.com/manifest.json`.

## Verify the deployment

Open these in a browser (first hit after idle takes ~30–50s — cold start):

- `/` → `{"status":"ok"}`
- `/manifest.json` → the addon manifest
- `/catalog/movie/streamain.json` → your personal videos
- `/debug/mappings` → which videos mapped onto which movies/episodes (`movies`,
  `series`, and `unmapped` lists, each with a `resolved` flag)

## Add to Nuvio / Stremio

Paste the manifest URL into the addon search/install box:

```
https://<your-app>.onrender.com/manifest.json
```

Your videos appear under the **My Streamain Videos** catalog, and real-title
interception becomes active.

## Prank mode (auto-mapping)

Name the Streamain upload after the target — the parser is format-tolerant:

| To hijack… | Name the Streamain upload |
|---|---|
| A movie | `Inception (2010)`, `Inception.2010`, `inception 2010` |
| A TV episode | `Gachiakuta S01E04`, `breaking.bad.s05e14`, `Show 1x04` |
| Nothing (personal only) | anything not a real title, e.g. `MyBirthday` |

Within one cache cycle the server resolves the title to its IMDB id and maps it.
A guest opening that **exact** movie/episode in Nuvio gets your video; everything
else plays real streams. Check `/debug/mappings` to confirm.

## Run locally

```powershell
pip install -r requirements.txt
$env:STREAMAIN_EMAIL="you@example.com"
$env:STREAMAIN_PASSWORD="secret"
python app.py
# http://localhost:7000/manifest.json
```

## Notes & limitations

- **Cold starts:** Render's free web service sleeps after ~15 min idle; the first
  request wakes it (~30–50s).
- **Cloudflare:** login runs from a datacenter IP, which Cloudflare challenges more
  aggressively. If plain login fails, the app logs a clear message — set
  `STREAMAIN_COOKIE` to bypass.
- **Embed extraction:** the mp4 extractor is deliberately tolerant (regex for
  `cdn.streamain.com/*.mp4` plus a `<source>` fallback). If Streamain changes its
  embed markup, update `_MP4_RE` / `_resolve_stream` in `streamain.py`.
- **Mapping accuracy:** a video didn't map? Check `/debug/mappings` → `unmapped`,
  rename it closer to the real title, or lower `STREAMAIN_MATCH_THRESHOLD`. Wrong
  title hijacked? Raise the threshold (e.g. `0.75`) and add the year to the name.
