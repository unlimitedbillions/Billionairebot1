#!/usr/bin/env python3
"""
youtube_oauth.py — YouTube link + PRIVATE uploads + 1-week public expiry.

Subcommands:
  link     One-time OAuth in browser -> creates token.json (run on phone/PC)
  upload   Upload rendered videos as PRIVATE (--file jobs.json | --queue q.json | --video x.mp4)
  public   Make a video public (--id ID | --latest)
  private  Make a video private again (--id ID | --all)
  expire   Auto-private anything public for >= N days (cron-safe, exit 0)
  list     Show local upload log

CI usage: set secrets YT_CLIENT_SECRETS_B64 and YT_TOKEN_B64 (base64 of the two JSON files).
"""
import argparse, base64, json, os, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent if HERE.name == "tools" else HERE
DATA = ROOT / "data"; DATA.mkdir(parents=True, exist_ok=True)
LOG = DATA / "yt_uploads.json"
TOKEN = ROOT / "token.json"
SECRETS = ROOT / "client_secrets.json"
SCOPES = ["https://www.googleapis.com/auth/youtube"]
OAUTH_PORT = 8642  # add http://localhost:8642/ to Google Console redirect URIs


def now():
    return datetime.now(timezone.utc).isoformat()


def parse_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_log():
    if LOG.exists():
        try:
            return json.loads(LOG.read_text())
        except Exception:
            return []
    return []


def save_log(entries):
    LOG.write_text(json.dumps(entries, indent=2))


def _b64_env(name, dest: Path):
    val = os.getenv(name, "").strip()
    if val and not dest.exists():
        dest.write_bytes(base64.b64decode(val))


def get_service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    _b64_env("YT_CLIENT_SECRETS_B64", SECRETS)
    _b64_env("YT_TOKEN_B64", TOKEN)

    creds = None
    if TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN.write_text(creds.to_json())
        else:
            if not SECRETS.exists():
                sys.exit("MISSING client_secrets.json (or YT_CLIENT_SECRETS_B64). Download from Google Cloud Console.")
            flow = InstalledAppFlow.from_client_secrets_file(str(SECRETS), SCOPES)
            creds = flow.run_local_server(port=OAUTH_PORT, prompt="consent", access_type="offline")
            TOKEN.write_text(creds.to_json())
            print("[yt] Linked! token.json created. Keep it secret.")
    return build("youtube", "v3", credentials=creds)


def upload_one(yt, video_path, title, description, tags, thumb=None, category="22"):
    from googleapiclient.http import MediaFileUpload
    body = {
        "snippet": {"title": title[:100], "description": description, "tags": tags[:30], "categoryId": category},
        "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False},
    }
    request = yt.videos().insert(
        part="snippet,status", body=body,
        media_body=MediaFileUpload(str(video_path), chunksize=8 * 1024 * 1024, resumable=True),
    )
    resp = None
    while resp is None:
        status, resp = request.next_chunk()
        if status:
            print(f"[yt]   uploading {int(status.progress() * 100)}%")
    vid = resp["id"]
    if thumb and Path(thumb).exists():
        try:
            yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(str(thumb))).execute()
            print("[yt]   thumbnail set")
        except Exception as e:
            print(f"[yt]   thumbnail skipped ({e})")
    print(f"[yt]   PRIVATE upload done: {vid} | {title}")
    return vid


def find_job_video(job_id):
    d = ROOT / "output" / job_id
    if not d.exists():
        return None, None
    mp4 = next((f for f in sorted(d.glob("*.mp4"))), None)
    thumb = d / "thumb_16x9.jpg"
    return mp4, (thumb if thumb.exists() else None)


def cmd_link(args):
    get_service()


def cmd_upload(args):
    yt = get_service()
    entries = load_log()
    known_jobs = {e["job_id"] for e in entries}
    known_videos = {e.get("video_id") for e in entries}

    jobs = []
    if args.video:
        jobs = [{"id": Path(args.video).stem, "title": args.title or Path(args.video).stem,
                 "description": args.description or "", "tags": (args.tags or "").split(",") if args.tags else [],
                 "_video": args.video, "_thumb": args.thumb}]
    else:
        queue_file = args.queue or args.file or "input/scripts/billionaire-stories.json"
        raw = json.loads((ROOT / queue_file).read_text())
        banned = []
        spec = ROOT / "output" / "out-of-spec.json"
        if spec.exists():
            try: banned = json.loads(spec.read_text())
            except Exception: pass
        jobs = [j for j in raw if j["id"] not in known_jobs and j["id"] not in banned]

    if not jobs:
        print("[yt] Nothing new to upload."); return

    for job in jobs:
        if "_video" in job:
            mp4, thumb = Path(job["_video"]), job.get("_thumb")
        else:
            mp4, thumb = find_job_video(job["id"])
        if not mp4 or not Path(mp4).exists():
            print(f"[yt] {job['id']}: no rendered mp4 — skipped"); continue
        vid = upload_one(
            yt, mp4,
            job.get("title", job["id"]),
            job.get("description", ""),
            job.get("tags", []),
            thumb=thumb,
        )
        entries.append({"job_id": job["id"], "video_id": vid, "title": job.get("title", job["id"]),
                        "uploaded_at": now(), "observed_public_at": None, "expired_at": None,
                        "status": "private"})
        save_log(entries)
    print("[yt] Upload pass complete. All videos PRIVATE until you publish them.")


def _set_privacy(yt, vid, status):
    yt.videos().update(part="status", body={"id": vid, "status": {"privacyStatus": status}}).execute()


def cmd_public(args):
    yt = get_service(); entries = load_log()
    target = entries[-1] if args.latest else next((e for e in entries if e["video_id"] == args.id), None)
    if not target:
        sys.exit("[yt] video not found in log")
    _set_privacy(yt, target["video_id"], "public")
    target["status"] = "public"; target["observed_public_at"] = now()
    save_log(entries)
    print(f"[yt] PUBLIC: {target['video_id']} — will auto-private in {args.days} days (expire cron).")


def cmd_private(args):
    yt = get_service(); entries = load_log()
    targets = entries if args.all else [e for e in entries if e["video_id"] == args.id]
    for e in targets:
        _set_privacy(yt, e["video_id"], "private")
        e["status"] = "private"
    save_log(entries)
    print(f"[yt] {len(targets)} video(s) set to private.")


def cmd_expire(args):
    """Cron-safe: private anything that has been public >= args.days (default 7)."""
    yt = get_service(); entries = load_log(); changed = 0
    for e in entries:
        try:
            live = yt.videos().list(part="status", id=e["video_id"]).execute()
        except Exception as ex:
            print(f"[yt] {e['video_id']}: status check failed ({ex})"); continue
        items = live.get("items", [])
        if not items:
            e["status"] = "gone"; continue
        privacy = items[0]["status"]["privacyStatus"]
        if privacy == "public":
            if not e.get("observed_public_at"):
                e["observed_public_at"] = now()  # first time seen public (even if set manually in Studio)
                print(f"[yt] {e['video_id']}: observed public, 7-day clock started.")
            elif parse_dt(e["observed_public_at"]) <= datetime.now(timezone.utc) - timedelta(days=args.days):
                _set_privacy(yt, e["video_id"], "private")
                e["status"] = "private"; e["expired_at"] = now(); e["observed_public_at"] = None
                changed += 1
                print(f"[yt] {e['video_id']}: public for >= {args.days} days -> back to PRIVATE.")
        else:
            e["status"] = privacy
    save_log(entries)
    print(f"[yt] expire pass done. {changed} video(s) re-privated.")


def cmd_list(args):
    for e in load_log():
        print(f"{e['status']:8} | {e['video_id']} | {e['title'][:50]} | uploaded {e['uploaded_at'][:10]}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("link")
    u = sub.add_parser("upload"); u.add_argument("--file"); u.add_argument("--queue"); u.add_argument("--video"); u.add_argument("--thumb"); u.add_argument("--title"); u.add_argument("--description"); u.add_argument("--tags")
    g = sub.add_parser("public"); g.add_argument("--id"); g.add_argument("--latest", action="store_true"); g.add_argument("--days", type=int, default=7)
    v = sub.add_parser("private"); v.add_argument("--id"); v.add_argument("--all", action="store_true")
    e = sub.add_parser("expire"); e.add_argument("--days", type=int, default=7)
    sub.add_parser("list")
    a = p.parse_args()
    {"link": cmd_link, "upload": cmd_upload, "public": cmd_public, "private": cmd_private,
     "expire": cmd_expire, "list": cmd_list}[a.cmd](a)
