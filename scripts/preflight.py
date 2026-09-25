#!/usr/bin/env python3
"""
preflight.py — Production gate. Checks source narration, generated scene/assets,
and required tools before rendering.

Narration is authoritative from billionaire-stories.json. asset_manager.py may
rewrite only visual-tag payloads; it must never change spoken narration.
Actual TTS duration remains the final duration authority.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JOBS = ROOT / "input" / "scripts" / "render-jobs.json"
SRC = ROOT / "input" / "scripts" / "billionaire-stories.json"
MANIFEST = ROOT / "cache" / "assets.json"
SHORT_WORD_BAND = (100, 175)
LONG_MIN_WORDS = 540
W = 34
VISUAL_MARKER_RE = re.compile(r"\[Visual:\s*[^\]]+?\s*\]")


def words(script):
    return len(re.sub(r"\[[^\]]*\]", "", script).split())


def narration_text(script):
    """Return spoken text with visual/control tags removed and whitespace normalized."""
    return re.sub(r"\s+", " ", re.sub(r"\[[^\]]*\]", "", script)).strip()


def visual_count(script):
    return len(VISUAL_MARKER_RE.findall(script))


def load_jobs():
    source = json.loads(SRC.read_text()) if SRC.exists() else []
    rendered = json.loads(JOBS.read_text()) if JOBS.exists() else []
    return source, rendered


def check_narration(source, rendered):
    """Validate source bands and prove asset preparation preserved narration."""
    if not source or not rendered:
        return False, "missing source/render jobs"

    src_by_id = {str(j.get("id")): j for j in source}
    out_by_id = {str(j.get("id")): j for j in rendered}
    problems = []

    if set(src_by_id) != set(out_by_id):
        missing = sorted(set(src_by_id) - set(out_by_id))
        extra = sorted(set(out_by_id) - set(src_by_id))
        if missing:
            problems.append("missing jobs: " + ", ".join(missing))
        if extra:
            problems.append("unexpected jobs: " + ", ".join(extra))

    for jid, src_job in src_by_id.items():
        if jid not in out_by_id:
            continue
        script = str(src_job.get("script", ""))
        out_script = str(out_by_id[jid].get("script", ""))
        count = words(script)
        short = jid.endswith("-short")
        band_ok = SHORT_WORD_BAND[0] <= count <= SHORT_WORD_BAND[1] if short else count >= LONG_MIN_WORDS
        if not band_ok:
            expected = "100-175" if short else f">={LONG_MIN_WORDS}"
            problems.append(f"{jid}: {count} words (expected {expected})")
        if narration_text(script) != narration_text(out_script):
            problems.append(f"{jid}: narration changed during asset preparation")
        if visual_count(script) != visual_count(out_script):
            problems.append(
                f"{jid}: visual-tag count changed {visual_count(script)} -> {visual_count(out_script)}"
            )

    if problems:
        return False, "; ".join(problems[:6])
    return True, f"{len(source)} jobs; source narration preserved"


def check_scene_structure(rendered):
    """Validate scene timing inputs without imposing a fake long-form ceiling."""
    if not rendered:
        return False, "no render jobs"
    bad = []
    for job in rendered:
        script = str(job.get("script", ""))
        scene_lines = [line for line in script.split("\n") if line.strip() and visual_count(line)]
        for idx, line in enumerate(scene_lines, 1):
            if words(line) <= 0:
                bad.append(f"{job.get('id', 'job')} scene {idx}: no narration")
    if bad:
        return False, "; ".join(bad[:6])
    return True, "scene narration inputs valid"


def probe_video(path):
    """Return (ok, detail) only when ffprobe sees a real video stream with duration."""
    if not path.exists():
        return False, "missing"
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return False, "ffprobe unavailable"
    try:
        p = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_type:format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=15, check=True,
        )
        data = json.loads(p.stdout or "{}")
        stream = (data.get("streams") or [None])[0]
        duration = float((data.get("format") or {}).get("duration") or 0)
        return bool(stream and stream.get("codec_type") == "video" and duration > 0), \
            f"{stream.get('codec_type', 'none') if stream else 'none'}:{duration:.2f}s"
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return False, "unreadable media"


def gather():
    source, rendered = load_jobs()
    man = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else None
    scenes = sum(visual_count(str(j.get("script", ""))) for j in rendered)
    narr_ok, narr_detail = check_narration(source, rendered)
    scene_ok, scene_detail = check_scene_structure(rendered)

    imgs_ok = imgs_tot = vids_ok = vids_tot = 0
    if man:
        for ep in man.get("episodes", []):
            for sc in ep.get("scenes", []):
                if sc.get("type") == "portrait":
                    imgs_tot += 1
                    if sc.get("asset") and (ROOT / sc["asset"]).exists():
                        imgs_ok += 1
                elif sc.get("type") == "broll":
                    vids_tot += 1
                    if sc.get("asset_video"):
                        valid, _detail = probe_video(ROOT / sc["asset_video"])
                        if valid:
                            vids_ok += 1

    audio_ok = True
    try:
        import importlib
        importlib.import_module("edge_tts")
    except Exception:
        audio_ok = False

    return [
        ("Script", bool(rendered), f"{len(rendered)} jobs"),
        ("Scenes", scenes > 0, str(scenes)),
        ("Narration", narr_ok, narr_detail),
        ("Scene structure", scene_ok, scene_detail),
        ("Visual manifest", man is not None, ""),
        ("Images", imgs_tot > 0 and imgs_ok == imgs_tot, f"{imgs_ok}/{imgs_tot}"),
        ("Video clips", vids_tot > 0 and vids_ok == vids_tot, f"{vids_ok}/{vids_tot}"),
        ("Audio", audio_ok, "edge-tts"),
        ("Fonts", bool(shutil.which("fc-list")), ""),
        ("FFmpeg", bool(shutil.which("ffmpeg")), ""),
        ("FFprobe", bool(shutil.which("ffprobe")), ""),
        ("Remotion browser", bool(shutil.which("node")) and (ROOT / "node_modules" / "@remotion" / "cli").exists(), "node+remotion"),
    ]


def show(rows, failed):
    print("╔" + "═" * W + "╗")
    print("║" + "PRODUCTION PREFLIGHT".center(W) + "║")
    print("╠" + "═" * W + "╣")
    for name, ok, detail in rows:
        label = f"{name} {detail}".strip()[:28]
        print(f"║ {label:<28}{'✓' if ok else '✗':>3} ║")
    print("╚" + "═" * W + "╝")
    print("PREFLIGHT FAIL: " + ", ".join(failed) if failed else "READY TO RENDER")


def main():
    rows = gather()
    failed = [n for n, ok, _ in rows if not ok]

    if failed and any(("Image" in f or "clip" in f) for f in failed):
        print("[preflight] repairing missing assets...")
        subprocess.run([sys.executable, str(ROOT / "scripts" / "asset_manager.py")], cwd=ROOT)
        rows = gather()
        failed = [n for n, ok, _ in rows if not ok]

    show(rows, failed)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
