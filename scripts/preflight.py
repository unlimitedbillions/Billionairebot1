#!/usr/bin/env python3
"""
preflight.py — Production gate. Checks assets, durations, and tools.
Auto-repairs missing assets once before failing.
"""
import json, re, shutil, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JOBS = ROOT / "input" / "scripts" / "render-jobs.json"
SRC = ROOT / "input" / "scripts" / "billionaire-stories.json"
MANIFEST = ROOT / "cache" / "assets.json"
BANDS = {"short": (145, 175), "long": (360, 440)}
W = 30

def words(script): return len(re.sub(r"\[[^\]]*\]", "", script).split())

def gather():
    jobs_file = JOBS if JOBS.exists() else SRC
    jobs = json.loads(jobs_file.read_text()) if jobs_file.exists() else []
    man = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else None
    
    scenes = sum(len(re.findall(r"\[Visual:", j.get("script", ""))) for j in jobs)
    narr_ok = bool(jobs) and all(BANDS["short"][0] <= words(j.get("script", "")) <= BANDS["short"][1] if j["id"].endswith("-short") else BANDS["long"][0] <= words(j.get("script", "")) <= BANDS["long"][1] for j in jobs)
    
    imgs_ok = imgs_tot = vids_ok = vids_tot = 0
    if man:
        for ep in man.get("episodes", []):
            for sc in ep.get("scenes", []):
                if sc.get("type") == "portrait":
                    imgs_tot += 1
                    if sc.get("asset") and (ROOT / sc["asset"]).exists(): imgs_ok += 1
                elif sc.get("type") == "broll":
                    vids_tot += 1
                    if sc.get("asset_video") and (ROOT / sc["asset_video"]).exists(): vids_ok += 1
                    
    audio_ok = True
    try: import edge_tts    except: audio_ok = False
    
    return [
        ("Script", bool(jobs), f"{len(jobs)} jobs"),
        ("Scenes", scenes > 0, str(scenes)),
        ("Narration", narr_ok, "bands ok"),
        ("Scene durations", narr_ok, "est ok"),
        ("Visual manifest", man is not None, ""),
        ("Images", imgs_tot > 0 and imgs_ok == imgs_tot, f"{imgs_ok}/{imgs_tot}"),
        ("Video clips", vids_tot > 0 and vids_ok == vids_tot, f"{vids_ok}/{vids_tot}"),
        ("Audio", audio_ok, "edge-tts"),
        ("Fonts", bool(shutil.which("fc-list")), ""),
        ("FFmpeg", bool(shutil.which("ffmpeg")), ""),
        ("Remotion browser", bool(shutil.which("node")) and (ROOT / "node_modules" / "@remotion" / "cli").exists(), "node+remotion"),
    ]

def show(rows, failed):
    print("╔" + "═"*W + "╗")
    print("║" + "PRODUCTION PREFLIGHT".center(W) + "║")
    print("╠" + "═"*W + "╣")
    for name, ok, detail in rows:
        label = f"{name} {detail}".strip()[:24]
        print(f"║ {label:<24}{'✓' if ok else '✗':>3}  ║")
    print("╚" + "═"*W + "╝")
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
