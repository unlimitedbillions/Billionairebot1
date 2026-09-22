#!/usr/bin/env python3
"""
asset_manager.py — PLAN + PREPARE phase (runs BEFORE rendering). 100% FREE / KEYLESS.

Rules:
  * NEVER images alone: b-roll scenes resolve to VIDEO clips; portraits to images.
  * Zero network at render: everything downloaded/generated beforehand.
  * Reuse cap: one file serves at most 2 scenes (MAX_REUSE).
  * Chain (all free):
        cache/reuse -> Pexels (7s timeout, 1 retry; skipped if no key)
                    -> Wikimedia -> Openverse -> Internet Archive (clips)
                    -> POLLINATIONS AI IMAGE (free, no key, no signup;
                       scene imagery only, NEVER people/faces)
                    -> LoremFlickr keyword photos (free, no key)
                    -> bundled fallback -> ffmpeg gradient / motion clip
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
from urllib.parse import quote

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
DL_T = 20            # keep asset prep bounded; local/generated fallbacks handle misses
AI_T = 40            # key-less AI image timeout
WPS = 3.0

VISUAL_RE = re.compile(r"\[Visual:\s*(?P<val>[^\]]+?)\s*\]")
usage = {}
PREEXISTING = set()

FACE_KEYWORDS = ["portrait", "face", "head", "musk", "bezos", "gates", "jobs",
                 "buffett", "arnault", "page", "brin", "person", "man", "woman",
                 "boy", "child", "ceo", "founder"]


def is_face_query(query):
    q = query.lower()
    return any(k in q for k in FACE_KEYWORDS)


def log(m):
    print(f"[assets] {m}", flush=True)


def h(s):
    return hashlib.sha1(s.encode()).hexdigest()[:10]


def get(url, params=None, headers=None):
    r = requests.get(url, params=params, headers=headers, timeout=API_T)
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
    d = get("https://api.pexels.com/videos/search",
            {"query": query, "orientation": orient, "per_page": 3},
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
    d = get("https://api.pexels.com/v1/search",
            {"query": query, "orientation": orient, "per_page": 3},
            {"Authorization": PEXELS_KEY})
    return [p["src"]["large"] for p in d.get("photos", [])]


# ---------------- KEY-LESS PROVIDERS -----------------------------------------
def wikimedia_images(query):
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


def openverse_images(query):
    d = get("https://api.openverse.org/v1/images/", {"q": query, "page_size": 3})
    return [r["url"] for r in d.get("results", []) if r.get("url")]


def archive_videos(query):
    d = get("https://archive.org/advancedsearch.php", {
        "q": f"({query}) AND mediatype:(movies)", "fl[]": "identifier",
        "rows": "2", "output": "json"})
    out = []
    for doc in ((d.get("response") or {}).get("docs") or []):
        ident = doc.get("identifier")
        if not ident:
            continue
        try:
            m = get(f"https://archive.org/metadata/{ident}")
            for f in m.get("files", []):
                if f.get("name", "").endswith(".mp4") and int(f.get("size", 0)) < 60_000_000:
                    out.append(f"https://archive.org/download/{ident}/{f['name']}")
                    break
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


def generated_clip(image_path, dest, seconds=5):
    vf = ("scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720,"
          "zoompan=z='min(zoom+0.0008,1.25)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
          f":d={int(seconds * 24)}:s=1280x720:fps=24")
    cmd = ["ffmpeg", "-y", "-loop", "1", "-i", str(image_path), "-vf", vf,
           "-t", str(seconds), "-an", "-c:v", "libx264", "-preset", "ultrafast",
           "-pix_fmt", "yuv420p", str(dest)]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return dest.exists()
    except Exception as e:
        log(f"clip gen failed: {e}")
        return False


# ---------------- DOWNLOAD + VALIDATE ----------------------------------------
def _is_jpeg(p):
    with open(p, "rb") as f:
        return f.read(3) == b"\xff\xd8\xff"


def download(url, dest):
    try:
        with requests.get(url, stream=True, timeout=DL_T if "pollinations" not in url else AI_T) as r:
            r.raise_for_status()
            tmp = dest.with_suffix(dest.suffix + ".part")
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
            if dest.suffix in (".jpg", ".jpeg") and not _is_jpeg(tmp):
                tmp.unlink(missing_ok=True)
                return False
            tmp.replace(dest)
        return dest.exists() and dest.stat().st_size > 5000
    except Exception as e:
        log(f"download failed {url}: {e}")
        return False


def _save(url, prefix, kind, query, orient, slot, source):
    ext = ".mp4" if prefix == "vv" else (".png" if url.lower().split("?")[0].endswith(".png") else ".jpg")
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


def fetch_video(query, orient, slot):
    for src, fn in (("pexels", lambda: pexels_videos(query, orient)),
                    ("archive", lambda: archive_videos(query))):
        url = _pick(with_retry(fn), slot)
        if url:
            r = _save(url, "vv", "vid", query, orient, slot, src)
            if r[0]:
                return r

    # never a bare image for b-roll: build a motion clip from whatever image won
    img_name, _ = fetch_image(query, orient, slot)
    if img_name:
        dest = VIS / f"vv-{h('vid|' + query + '|' + orient + '|' + str(slot))}.mp4"
        if generated_clip(VIS / img_name, dest):
            return dest.name, "generated-clip"
    return None, "missing"


def assign(tag_val, orient):
    tag_val = tag_val.strip()
    if tag_val.startswith("wikimedia:"):
        kind, query = "image", tag_val.split(":", 1)[1].strip()
    elif tag_val.startswith("pexels:"):
        kind, query = "video", tag_val.split(":", 1)[1].strip()
    else:
        kind, query = "video", tag_val

    base = ("va-" if kind == "image" else "vv-") + h(f"{kind}|{query}|{orient}")

    # Reuse restored/local assets before any network call.  This makes the
    # asset-prep phase genuinely cache-first across GitHub Actions runs.
    for fname in sorted(PREEXISTING):
        if fname.startswith(base) and (VIS / fname).exists():
            used = usage.get(fname, 0)
            if used < MAX_REUSE:
                usage[fname] = used + 1
                return fname, "cache", kind

    for fname, cnt in usage.items():
        if fname.startswith(base) and cnt < MAX_REUSE and (VIS / fname).exists():
            usage[fname] += 1
            return fname, "reuse", kind

    slot = len([f for f in usage if f.startswith(base)])
    if kind == "image":
        fname, src = fetch_image(query, orient, slot)
    else:
        fname, src = fetch_video(query, orient, slot)
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
                fname, src, kind = assign(val, orient)
                scenes.append({"id": f"scene-{n:03d}",
                               "duration": round(scene_words(line) / WPS, 1),
                               "type": "portrait" if kind == "image" else "broll",
                               "asset": f"input/visuals/{fname}" if (fname and kind == "image") else None,
                               "asset_video": f"input/visuals/{fname}" if (fname and kind == "video") else None,
                               "source": src, "query": val})
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
                   "chain": ["cache/reuse", "pexels", "wikimedia", "openverse",
                             "archive", "ai-free(pollinations)", "loremflickr",
                             "bundled-fallback", "generated"]},
        "episodes": episodes}, indent=2))
    OUT_JOBS.write_text(json.dumps(rewritten, indent=2))

    scenes = [s for e in episodes for s in e["scenes"]]
    imgs = [s for s in scenes if s["type"] == "portrait"]
    vids = [s for s in scenes if s["type"] == "broll"]
    ok_i = sum(1 for s in imgs if s["asset"] and (ROOT / s["asset"]).exists())
    ok_v = sum(1 for s in vids if s["asset_video"] and (ROOT / s["asset_video"]).exists())
    uniq = len({s.get("asset") or s.get("asset_video") for s in scenes})
    log(f"images {ok_i}/{len(imgs)} | clips {ok_v}/{len(vids)} | unique files {uniq} | reuse<= {MAX_REUSE}/file")
    log(f"manifest -> {MANIFEST.relative_to(ROOT)} | renderjobs -> {OUT_JOBS.relative_to(ROOT)}")
    if ok_i < len(imgs) or ok_v < len(vids):
        sys.exit(1)


if __name__ == "__main__":
    main()
