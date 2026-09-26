#!/usr/bin/env python3
"""
asset_manager.py — PLAN + PREPARE phase (runs BEFORE rendering). 100% FREE / KEYLESS.

Rules:
  * Every b-roll scene resolves to a validated MP4; real footage is preferred,
    but a relevant still can be converted into a subtle motion clip when video
    providers cannot supply suitable footage.
  * Zero network at render: everything downloaded/generated beforehand.
  * Reuse cap: one file serves at most 2 scenes (MAX_REUSE).
  * Chain (all free):
        cache/reuse -> Pexels (7s timeout, 1 retry; skipped if no key)
                    -> Wikimedia -> Openverse -> Internet Archive (clips)
                    -> POLLINATIONS AI IMAGE (free, no key, no signup;
                       scene imagery only, NEVER people/faces)
                    -> LoremFlickr keyword photos (free, no key)
                    -> bundled image fallback / ffmpeg gradient image
  * Semantic asset reuse is a LAST-RESORT fallback only, after real video and
    relevant image->motion fallbacks fail; unrelated assets are never reused.
  * Face safety: queries that look like a real person NEVER go to AI or random
    photo services; they resolve from Wikimedia only, else bundled silhouette,
    else gradient card.
  * Manual Qwen hero shots: drop a file in input/visuals/ and reference it as
    [Visual: filename.jpg] in the script — bound locally, zero network.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote, unquote

import requests

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "input" / "scripts" / "billionaire-stories.json"
OUT_JOBS = ROOT / "input" / "scripts" / "render-jobs.json"
VIS = ROOT / "input" / "visuals"
CACHE = ROOT / "cache"
MANIFEST = CACHE / "assets.json"
FALLBACK = ROOT / "assets" / "fallback"

PEXELS_KEY = os.getenv("PEXELS_API_KEY", "").strip()
API_T = 7            # spec: ~6-8s
RETRY = 1            # max 1 retry per source
MAX_REUSE = 2        # one file serves at most 2 scenes
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".mkv", ".m4v", ".avi", ".mpeg", ".mpg", ".m2ts", ".mts", ".ts", ".ogv", ".3gp", ".flv", ".wmv", ".asf"}
DL_T = 20            # keep asset prep bounded; local/generated fallbacks handle misses
AI_T = 40            # key-less AI image timeout
WPS = 3.0
HTTP_HEADERS = {"User-Agent": "BillionaireBot/1.0 (https://github.com/unlimitedbillions/Billionairebot1)"}

VISUAL_RE = re.compile(r"\[Visual:\s*(?P<val>[^\]]+?)\s*\]")
usage = {}
PREEXISTING = set()

FACE_KEYWORDS = ["portrait", "face", "head", "musk", "bezos", "gates", "jobs",
                 "buffett", "arnault", "page", "brin", "person", "man", "woman",
                 "boy", "child", "ceo", "founder"]


def is_face_query(query):
    q = query.lower()
    return any(k in q for k in FACE_KEYWORDS)


STOPWORDS = {"the", "and", "for", "with", "from", "over", "into", "this", "that", "young", "old", "night", "day"}


def query_terms(query):
    return {w for w in re.findall(r"[a-z0-9]+", provider_query(query).lower()) if len(w) >= 4 and w not in STOPWORDS}


def candidate_is_relevant(candidate, query):
    """Reject provider results whose descriptive title is clearly unrelated.
    Providers without descriptive titles remain eligible; media validation still
    requires a real playable video.
    """
    if not isinstance(candidate, dict):
        return True
    text = str(candidate.get("text") or "").lower()
    terms = query_terms(query)
    if not text or not terms:
        return True
    tokens = set(re.findall(r"[a-z0-9]+", unquote(text)))
    return bool(tokens & terms)


def candidate_url(candidate):
    return candidate.get("url") if isinstance(candidate, dict) else candidate


def log(m):
    print(f"[assets] {m}", flush=True)


def h(s):
    return hashlib.sha1(s.encode()).hexdigest()[:10]


def provider_query(query):
    q = query.strip()
    while True:
        cleaned = re.sub(r"^(?:pexels|wikimedia|archive|openverse)\s*:\s*", "", q, flags=re.I)
        if cleaned == q:
            return q
        q = cleaned


def get(url, params=None, headers=None):
    req_headers = dict(HTTP_HEADERS)
    if headers:
        req_headers.update(headers)
    r = requests.get(url, params=params, headers=req_headers, timeout=API_T)
    r.raise_for_status()
    return r.json()


def with_retry(fn, *a):
    for i in range(RETRY + 1):
        try:
            v = fn(*a)
            if v:
                return v
        except Exception as e:
            log(f"  {fn.__name__} attempt {i + 1} failed: {e}")
    return []


# ---------------- KEYED (free) PROVIDER --------------------------------------
def pexels_videos(query, orient):
    if not PEXELS_KEY:
        return []
    query = provider_query(query)
    d = get("https://api.pexels.com/videos/search",
            {"query": query, "orientation": orient, "per_page": 10},
            {"Authorization": PEXELS_KEY})
    out = []
    for v in d.get("videos", []):
        files = [f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4"]
        pick = next((f for f in sorted(files, key=lambda x: x.get("width", 0))
                     if 0 < f.get("width", 99999) <= 1280), None) or (files[0] if files else None)
        if pick:
            out.append(pick["link"])
    return out


def pexels_images(query, orient):
    if not PEXELS_KEY:
        return []
    query = provider_query(query)
    d = get("https://api.pexels.com/v1/search",
            {"query": query, "orientation": orient, "per_page": 3},
            {"Authorization": PEXELS_KEY})
    return [p["src"]["large"] for p in d.get("photos", [])]


# ---------------- KEY-LESS PROVIDERS -----------------------------------------
def wikimedia_images(query):
    query = provider_query(query)
    d = get("https://commons.wikimedia.org/w/api.php", {
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": f"filetype:bitmap {query}", "gsrnamespace": "6",
        "gsrlimit": "3", "prop": "imageinfo", "iiprop": "url|mime"})
    pages = (d.get("query") or {}).get("pages") or {}
    out = []
    for p in sorted(pages.values(), key=lambda x: x.get("index", 99)):
        ii = (p.get("imageinfo") or [{}])[0]
        if ii.get("mime") in ("image/jpeg", "image/png") and ii.get("url"):
            out.append(ii["url"])
    return out


def wikimedia_videos(query):
    query = provider_query(query)
    d = get("https://commons.wikimedia.org/w/api.php", {
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": f"filetype:video {query}", "gsrnamespace": "6",
        "gsrlimit": "8", "prop": "imageinfo", "iiprop": "url|mime|size"})
    pages = (d.get("query") or {}).get("pages") or {}
    out = []
    for p in sorted(pages.values(), key=lambda x: x.get("index", 99)):
        ii = (p.get("imageinfo") or [{}])[0]
        mime = ii.get("mime", "")
        size = int(ii.get("size") or 0)
        url = ii.get("url")
        if url and mime.startswith("video/") and (size == 0 or size <= 60_000_000):
            out.append({"url": url, "text": p.get("title") or p.get("pageid", "")})
    return out


def openverse_images(query):
    d = get("https://api.openverse.org/v1/images/", {"q": query, "page_size": 3})
    return [r["url"] for r in d.get("results", []) if r.get("url")]


def archive_videos(query):
    query = provider_query(query)
    d = get("https://archive.org/advancedsearch.php", {
        "q": f"({query}) AND mediatype:(movies)", "fl[]": "identifier",
        "rows": "5", "output": "json"})
    out = []
    for doc in ((d.get("response") or {}).get("docs") or []):
        ident = doc.get("identifier")
        if not ident:
            continue
        try:
            m = get(f"https://archive.org/metadata/{ident}")
            candidates = [
                f for f in m.get("files", [])
                if Path(f.get("name", "")).suffix.lower() in VIDEO_EXTENSIONS
                and int(f.get("size", 0) or 0) < 60_000_000
            ]
            # Keep several candidates from each item. Archive can expose one
            # forbidden/broken derivative while another derivative is usable.
            title = str(doc.get("title") or ident)
            for f in candidates[:4]:
                out.append({
                    "url": f"https://archive.org/download/{ident}/{quote(f['name'], safe='')}",
                    "text": f"{title} {f.get('name', '')}",
                })
        except Exception:
            continue
    return out


def pollinations_image(query, slot):
    """FREE key-less AI image. Scene imagery only (prompt forbids people)."""
    prompt = (f"{query}, cinematic documentary still, dramatic lighting, "
              f"teal and orange grade, no text, no watermark, no people")
    url = ("https://image.pollinations.ai/prompt/" + quote(prompt) +
           f"?width=1280&height=720&nologo=true&seed={100 + slot}")
    return [url]


def loremflickr_image(query, slot):
    """FREE key-less keyword photos (real CC images)."""
    kw = ",".join(re.findall(r"[a-z]+", query.lower())[:3]) or "business"
    return [f"https://loremflickr.com/1280/720/{kw}?lock={slot + 1}"]


# ---------------- LOCAL GENERATORS (never fail) ------------------------------
def bundled_image(query):
    if not FALLBACK.exists():
        return None
    q = query.lower()
    pref = ["fallback-portrait.jpg", "fallback-city.jpg", "fallback-money.jpg"]
    if any(w in q for w in ("money", "coin", "stock", "market", "wealth")):
        pref = ["fallback-money.jpg", "fallback-city.jpg", "fallback-portrait.jpg"]
    elif any(w in q for w in ("city", "building", "office", "skyline")):
        pref = ["fallback-city.jpg", "fallback-money.jpg", "fallback-portrait.jpg"]
    for name in pref:
        f = FALLBACK / name
        if f.exists():
            return f
    return next(iter(sorted(FALLBACK.glob("*.jpg")) + sorted(FALLBACK.glob("*.png"))), None)


def gradient_image(dest, variant=0):
    palettes = [("0x0a0c12", "0x23304a"), ("0x120a0a", "0x4a2323"), ("0x0a120c", "0x234a30")]
    c0, c1 = palettes[variant % len(palettes)]
    cmd = ["ffmpeg", "-y", "-f", "lavfi",
           "-i", f"gradients=s=1280x720:d=1:c0={c0}:c1={c1}:speed=0.01",
           "-frames:v", "1", "-q:v", "2", str(dest)]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return dest.exists()
    except Exception as e:
        log(f"gradient gen failed: {e}")
        return False



"""Convert a relevant still into a deterministic, render-safe MP4.

The clip is intentionally longer than a typical scene so the renderer can trim
it to the exact measured TTS duration. The subtle zoom/pan keeps still fallbacks
from looking like frozen frames.
"""
MOTION_SECONDS = 8
MOTION_FPS = 30


def image_to_motion(image_path, query, orient, slot):
    out = VIS / f"rv-motion-{h('motion|' + query + '|' + orient + '|' + str(slot))}.mp4"
    tmp = out.with_suffix(".tmp.mp4")
    vf = (
        "scale=1280:720:force_original_aspect_ratio=increase,"
        "crop=1280:720,"
        "zoompan=z='min(zoom+0.0007,1.10)':"
        "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d={MOTION_SECONDS * MOTION_FPS}:s=1280x720:fps={MOTION_FPS},"
        "format=yuv420p"
    )
    cmd = [
        "ffmpeg", "-y", "-loop", "1", "-i", str(image_path),
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-map", "0:v:0", "-map", "1:a:0",
        "-vf", vf, "-t", str(MOTION_SECONDS),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k", "-shortest",
        "-movflags", "+faststart", str(tmp),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=90)
        if _is_video(tmp) and tmp.stat().st_size > 5000:
            tmp.replace(out)
            return out.name
    except (OSError, subprocess.SubprocessError) as e:
        log(f"image->motion failed for {query!r}: {e}")
    finally:
        tmp.unlink(missing_ok=True)
    return None


def semantic_asset_reuse(query, orient, blocked):
    """Last-resort reuse of an existing video whose scene query is semantically close."""
    terms = query_terms(query)
    if not terms:
        return None

    best = None
    manifest = {}
    try:
        if MANIFEST.exists():
            manifest = json.loads(MANIFEST.read_text())
    except (OSError, ValueError):
        manifest = {}

    for episode in manifest.get("episodes", []):
        for scene in episode.get("scenes", []):
            asset = scene.get("asset_video")
            prior_query = str(scene.get("query") or "")
            if not asset or not prior_query:
                continue
            path = ROOT / asset
            if not path.exists() or path.name in blocked:
                continue
            used = usage.get(path.name, 0)
            if used >= MAX_REUSE:
                continue
            prior_terms = query_terms(prior_query)
            if not prior_terms:
                continue
            overlap = len(terms & prior_terms)
            score = overlap / max(1, len(terms | prior_terms))
            if overlap and (best is None or score > best[0]):
                best = (score, path.name, prior_query)

    if best and best[0] >= 0.25:
        usage[best[1]] = usage.get(best[1], 0) + 1
        log(f"  semantic reuse: {best[1]} ({best[0]:.2f} match) for {query!r}")
        return best[1]
    return None


# ---------------- DOWNLOAD + VALIDATE ----------------------------------------
def _is_jpeg(p):
    with open(p, "rb") as f:
        return f.read(3) == b"\xff\xd8\xff"


def _is_video(p):
    try:
        # WebM/Matroska files commonly omit duration at the stream level while
        # ffprobe still exposes a valid container duration. Validate both the
        # video stream and the container duration so genuine Wikimedia clips
        # are not rejected as "invalid video".
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_type:format=duration",
             "-of", "json", str(p)],
            capture_output=True, text=True, timeout=10, check=False)
        if probe.returncode != 0:
            return False
        data = json.loads(probe.stdout or "{}")
        stream = (data.get("streams") or [{}])[0]
        duration = float((data.get("format") or {}).get("duration") or 0)
        return stream.get("codec_type") == "video" and duration > 0
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return False


def media_duration(path):
    """Return the measured media duration in seconds, or 0 when unavailable."""
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=10, check=False,
        )
        value = float((probe.stdout or "").strip())
        return value if value > 0 else 0.0
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return 0.0


def download(url, dest):
    try:
        with requests.get(url, stream=True, headers=HTTP_HEADERS, timeout=DL_T if "pollinations" not in url else AI_T) as r:
            r.raise_for_status()
            tmp = dest.with_suffix(dest.suffix + ".part")
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
            if dest.suffix in (".jpg", ".jpeg") and not _is_jpeg(tmp):
                tmp.unlink(missing_ok=True)
                return False
            if dest.suffix in VIDEO_EXTENSIONS and not _is_video(tmp):
                tmp.unlink(missing_ok=True)
                log(f"invalid video rejected: {url}")
                return False
            tmp.replace(dest)
        return dest.exists() and dest.stat().st_size > 5000
    except Exception as e:
        log(f"download failed {url}: {e}")
        return False


def _save(url, prefix, kind, query, orient, slot, source):
    path_suffix = Path(url.split("?", 1)[0]).suffix.lower()
    if prefix in ("vv", "rv"):
        ext = path_suffix if path_suffix in VIDEO_EXTENSIONS else ".mp4"
    else:
        ext = ".png" if path_suffix == ".png" else ".jpg"
    dest = VIS / f"{prefix}-{h(kind + '|' + query + '|' + orient + '|' + str(slot))}{ext}"
    if download(url, dest):
        return dest.name, source
    return None, "missing"


def _pick(cands, slot):
    if not cands:
        return None
    return cands[slot] if len(cands) > slot else (cands[0] if slot == 0 else None)


# ---------------- RESOLUTION CHAINS ------------------------------------------
def fetch_image(query, orient, slot):
    face = is_face_query(query)

    for src, fn in (("pexels", lambda: pexels_images(query, orient)),
                    ("wikimedia", lambda: wikimedia_images(query)),
                    ("openverse", lambda: openverse_images(query))):
        url = _pick(with_retry(fn), slot)
        if url:
            r = _save(url, "va", "img", query, orient, slot, src)
            if r[0]:
                return r

    if not face:  # AI + random-photo layers are FORBIDDEN for real-person queries
        url = _pick(with_retry(lambda: pollinations_image(query, slot)), slot)
        if url:
            r = _save(url, "va", "img", query, orient, slot, "ai-free")
            if r[0]:
                return r
        url = _pick(with_retry(lambda: loremflickr_image(query, slot)), slot)
        if url:
            r = _save(url, "va", "img", query, orient, slot, "loremflickr")
            if r[0]:
                return r

    b = bundled_image(query)
    if b and slot == 0:
        dest = VIS / f"va-{h('img|' + query + '|' + orient + '|0')}{b.suffix}"
        shutil.copy2(b, dest)
        return dest.name, "bundled-fallback"

    dest = VIS / f"va-{h('img|' + query + '|' + orient + '|' + str(slot))}.jpg"
    if gradient_image(dest, slot):
        return dest.name, "gradient-card"
    return None, "missing"


def fetch_video(query, orient, slot, blocked=None):
    blocked = blocked or set()

    # Tier 1: real footage from trusted/free providers.
    for src, fn in (("pexels", lambda: pexels_videos(query, orient)),
                    ("wikimedia", lambda: wikimedia_videos(query)),
                    ("archive", lambda: archive_videos(query))):
        candidates = with_retry(fn)
        if not candidates:
            continue
        relevant = [c for c in candidates if candidate_is_relevant(c, query)]
        if not relevant:
            log(f"  {src}: no semantically relevant candidates for {query!r}")
            continue
        ordered = relevant[slot:] + relevant[:slot]
        for candidate in ordered:
            url = candidate_url(candidate)
            if not url:
                continue
            r = _save(url, "rv", "vid", query, orient, slot, src)
            if r[0]:
                return r

    # Tier 2: semantic visual fallback. A relevant still is preferable to a
    # missing scene, and is converted into a real MP4 with subtle motion.
    image_name, image_src = fetch_image(query, orient, slot)
    if image_name:
        motion = image_to_motion(VIS / image_name, query, orient, slot)
        if motion:
            return motion, f"{image_src}->motion"

    # Tier 3: semantic reuse is deliberately last. It may reuse only a prior
    # asset with meaningful query overlap and within the normal reuse cap.
    reused = semantic_asset_reuse(query, orient, blocked)
    if reused:
        return reused, "semantic-reuse"

    return None, "missing"


def assign(tag_val, orient, blocked=None):
    """Resolve one scene asset without reusing a source already used in this episode.

    blocked is episode-scoped: the same cached asset may still be reused by a
    different episode, but never twice inside one episode.
    """
    blocked = blocked or set()
    tag_val = tag_val.strip()
    if tag_val.startswith("wikimedia:"):
        kind, query = "image", tag_val.split(":", 1)[1].strip()
    elif tag_val.startswith("pexels:"):
        kind, query = "video", tag_val.split(":", 1)[1].strip()
    else:
        kind, query = "video", tag_val

    base = ("va-" if kind == "image" else "rv-") + h(f"{kind}|{query}|{orient}")

    # Reuse restored/local assets before any network call. The saved filename
    # includes the slot in its hash, so compute the exact cache filename here.
    ext_candidates = sorted(VIDEO_EXTENSIONS) if kind == "video" else [".jpg", ".png"]
    for slot in range(MAX_REUSE):
        stem = ("rv-" if kind == "video" else "va-") + h(
            f"{kind}|{query}|{orient}|{slot}"
        )
        for ext in ext_candidates:
            candidate = stem + ext
            if (VIS / candidate).exists() and (kind == "image" or candidate in PREEXISTING) and (kind == "image" or candidate not in blocked):
                used = usage.get(candidate, 0)
                if used < MAX_REUSE:
                    usage[candidate] = used + 1
                    return candidate, "cache", kind

    # IMPORTANT: never reuse an unrelated asset merely because it has
    # the same media type. The previous implementation did that and could
    # assign an arbitrary earlier clip/image to the next scene, creating many
    # visually duplicated scenes. Reuse is now restricted to the exact
    # query+orientation asset slots above. If no exact reusable asset exists,
    # fetch/generate a new one.
    used_stems = {Path(f).stem for f in usage}
    slot = 0
    while True:
        probe = ("rv-" if kind == "video" else "va-") + h(
            f"{kind}|{query}|{orient}|{slot}"
        )
        if probe not in used_stems:
            break
        slot += 1
    if kind == "image":
        fname, src = fetch_image(query, orient, slot)
    else:
        fname, src = fetch_video(query, orient, slot, blocked)
    if fname:
        usage[fname] = usage.get(fname, 0) + 1
    return fname, src, kind


def scene_words(line):
    return len(re.sub(r"\[[^\]]*\]", "", line).split())


def process_jobs(jobs):
    VIS.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    PREEXISTING.update(f.name for f in VIS.iterdir() if f.is_file())

    episodes, rewritten = [], []
    for job in jobs:
        jid = job.get("id", "job")
        orient = job.get("orientation", "portrait")
        lines = [l for l in job.get("script", "").split("\n") if l.strip()]
        scenes, new_lines = [], []
        episode_used_assets = set()

        for line in lines:
            def repl(m):
                val = m.group("val")
                n = len(scenes) + 1
                if not val.startswith(("wikimedia:", "pexels:")) and "." in val and " " not in val:
                    scenes.append({"id": f"scene-{n:03d}",
                                   "duration": round(scene_words(line) / WPS, 1),
                                   "type": "local", "asset": f"input/visuals/{val}",
                                   "asset_video": None, "source": "local", "query": val})
                    return m.group(0)
                fname, src, kind = assign(val, orient, episode_used_assets)
                if fname:
                    episode_used_assets.add(fname)
                scene_record = {"id": f"scene-{n:03d}",
                                "duration": round(scene_words(line) / WPS, 1),
                                "type": "portrait" if kind == "image" else "broll",
                                "asset": f"input/visuals/{fname}" if (fname and kind == "image") else None,
                                "asset_video": f"input/visuals/{fname}" if (fname and kind == "video") else None,
                                "source": src, "query": val}
                # Canonical render-ready segment metadata. The scene duration
                # remains narration/TTS-controlled; this is the source media's
                # own measured duration for validation/diagnostics only.
                if fname and kind == "video":
                    media_path = VIS / fname
                    scene_record["visual_segment"] = {
                        "asset_video": f"input/visuals/{fname}",
                        "duration": media_duration(media_path),
                        "scene_id": f"scene-{n:03d}",
                        "source": src,
                    }
                scenes.append(scene_record)
                return f"[Visual: {fname}]" if fname else m.group(0)

            new_lines.append(VISUAL_RE.sub(repl, line))

        newjob = dict(job)
        newjob["script"] = "\n".join(new_lines)
        rewritten.append(newjob)
        ok = all((s["asset"] if s["type"] == "portrait" else s["asset_video"])
                 or s["type"] == "local" for s in scenes)
        episodes.append({"episode": jid, "status": "ready" if ok else "degraded", "scenes": scenes})

    return episodes, rewritten


def main():
    if not SRC.exists():
        sys.exit("[assets] source json missing: input/scripts/billionaire-stories.json")
    jobs = json.loads(SRC.read_text())
    log(f"planning {len(jobs)} episode(s); prefetching images + clips BEFORE render...")

    episodes, rewritten = process_jobs(jobs)

    MANIFEST.write_text(json.dumps({
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "policy": {"pexels_timeout_s": API_T, "pexels_max_retries": RETRY,
                   "max_reuse_per_file": MAX_REUSE,
                   "semantic_reuse": "last_resort",
                   "motion_fallback_seconds": MOTION_SECONDS,
                   "visual_segment_schema": "{asset_video,duration,scene_id,source}",
                   "chain": ["exact-cache/reuse", "pexels", "wikimedia", "archive",
                             "relevant-image->motion", "semantic-video-reuse"]},
        "episodes": episodes}, indent=2))
    OUT_JOBS.write_text(json.dumps(rewritten, indent=2))

    scenes = [s for e in episodes for s in e["scenes"]]
    imgs = [s for s in scenes if s["type"] == "portrait"]
    vids = [s for s in scenes if s["type"] == "broll"]
    ok_i = sum(1 for s in imgs if s["asset"] and (ROOT / s["asset"]).exists())
    ok_v = sum(1 for s in vids if s["asset_video"] and (ROOT / s["asset_video"]).exists())
    uniq = len({s.get("asset") or s.get("asset_video") for s in scenes})
    reuse_counts = [
        count
        for e in episodes
        for count in [{
            a: sum(1 for s in e["scenes"] if (s.get("asset") or s.get("asset_video")) == a)
            for a in {s.get("asset") or s.get("asset_video") for s in e["scenes"] if (s.get("asset") or s.get("asset_video"))}
        }][0].values()
    ]
    duplicate_assets = sum(1 for count in reuse_counts if 1 < count <= MAX_REUSE)
    reuse_violations = sum(1 for count in reuse_counts if count > MAX_REUSE)
    source_counts = {}
    for scene in scenes:
        src = scene.get("source") or "unknown"
        source_counts[src] = source_counts.get(src, 0) + 1
    log(
        f"images {ok_i}/{len(imgs)} | clips {ok_v}/{len(vids)} | unique files {uniq} | "
        f"reused files {duplicate_assets} | reuse<= {MAX_REUSE}/file"
    )
    log("sources: " + ", ".join(f"{k}={v}" for k, v in sorted(source_counts.items())))
    unresolved = [s for s in scenes if (s["type"] == "broll" and not s.get("asset_video")) or
                  (s["type"] == "portrait" and not s.get("asset"))]
    if unresolved:
        log(f"unresolved scenes: {len(unresolved)}")
        for s in unresolved[:20]:
            log(f"  {s.get('id')} [{s.get('type')}] {s.get('query')}")
    if reuse_violations:
        sys.exit(f"[assets] reuse policy violated: {reuse_violations} file(s) used more than {MAX_REUSE} times")
    log(f"manifest -> {MANIFEST.relative_to(ROOT)} | renderjobs -> {OUT_JOBS.relative_to(ROOT)}")
    if ok_i < len(imgs) or ok_v < len(vids):
        sys.exit(1)


if __name__ == "__main__":
    main()
