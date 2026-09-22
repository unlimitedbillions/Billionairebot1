#!/usr/bin/env python3
"""
youtube_oauth.py — FINAL FIX: Manual URL Construction for Termux.
Bypasses google_auth_oauthlib bugs by building the OAuth URL manually.
"""
import argparse
import base64
import json
import os
import sys
import secrets
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode
import requests # Required for manual token exchange

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent if HERE.name == "tools" else HERE
DATA = ROOT / "data"
DATA.mkdir(parents=True, exist_ok=True)

LOG = DATA / "yt_uploads.json"
TOKEN = ROOT / "token.json"
SECRETS_FILE = ROOT / "client_secrets.json"

SCOPES = ["https://www.googleapis.com/auth/youtube"]
OAUTH_PORT = int(os.getenv("YT_OAUTH_PORT", "8642"))
REDIRECT_URI = f"http://localhost:{OAUTH_PORT}/"


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
    """Materialize a base64 secret, tolerating missing padding and URL-safe input.
    GitHub secrets are sometimes stored without trailing '=' padding; Python's
    strict decoder otherwise raises 'Incorrect padding' before upload starts.
    Raw JSON is also accepted as a safe local/CI fallback.
    """
    val = os.getenv(name, "").strip()
    if not val or dest.exists():
        return
    if val.startswith("{"):
        try:
            json.loads(val)
        except json.JSONDecodeError as e:
            raise SystemExit(f"[yt] {name} contains invalid raw JSON: {e}")
        dest.write_text(val, encoding="utf-8")
        return
    compact = "".join(val.split())
    compact += "=" * (-len(compact) % 4)
    try:
        data = base64.b64decode(compact, altchars=b"-_", validate=True)
    except Exception as e:
        raise SystemExit(f"[yt] {name} is not valid base64 (check that the secret was encoded once): {e}")
    dest.write_bytes(data)


def _is_termux():
    pfx = os.environ.get("PREFIX", "")
    return bool(os.environ.get("TERMUX_VERSION")) or "/data/data/com.termux" in pfx


def _load_client_config():
    try:
        return json.loads(SECRETS_FILE.read_text())
    except Exception as e:
        sys.exit(f"[yt] client_secrets.json is invalid JSON: {e}")


def _get_credentials_from_json(cfg):
    """Extract client_id and secret from either 'installed' or 'web' key."""
    for typ in ("installed", "web"):
        if typ in cfg:
            cid = cfg[typ].get("client_id")
            csec = cfg[typ].get("client_secret")
            if cid and csec:
                return cid, csec
    sys.exit("[yt] ERROR: No valid 'installed' or 'web' credentials found in JSON")


def _looks_like_google_error(url):
    try:
        p = urlparse(url)
        qs = parse_qs(p.query)
    except Exception:
        return False
    if "accounts.google.com" in p.netloc and ("authError" in url or "error" in qs):
        return True
    if "error" in qs or "authError" in qs:
        return True
    return False


def _normalize_redirect_input(raw):
    raw = raw.strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = raw.lstrip("?")
        return f"{REDIRECT_URI}?{raw}"
    return raw


def _manual_auth_manual_url(client_id, client_secret):
    """
    MANUAL CONSTRUCTION: Builds the OAuth URL directly to guarantee redirect_uri exists.
    Then exchanges the code for tokens using direct HTTP requests.
    """
    state = secrets.token_hex(16)
    
    # --- CRITICAL FIX: Manually build the params dict ---
    params = {
        "response_type": "code",
        "client_id": client_id,
        "scope": " ".join(SCOPES),
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
        "redirect_uri": REDIRECT_URI,  # <--- FORCED PRESENCE
        # Optional PKCE fields if your app requires them (usually not needed for Desktop apps via localhost)
        # "code_challenge": ..., 
        # "code_challenge_method": "S256",
    }
    
    auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
    
    print("\n[yt] === MANUAL AUTHORIZATION (Direct URL Build) ===")
    print(f"[yt] Client ID: {client_id[:20]}...")
    print(f"[yt] Redirect URI: {REDIRECT_URI}")
    print("[yt]")
    print("[yt] 1) Open this EXACT URL in your browser:")
    print("")
    print(auth_url)
    print("")
    print("[yt] 2) Sign in + Approve.")
    print("[yt] 3) Browser redirects to localhost. Page may say 'Site Can't Be Reached'. EXPECTED.")
    print("[yt] 4) COPY THE ENTIRE ADDRESS BAR URL (contains ?code=...&state=...)")
    print("[yt]    DO NOT paste accounts.google.com/error URLs.")
    print("[yt]")

    attempts = 0
    max_attempts = 3

    while attempts < max_attempts:
        attempts += 1
        raw = input("[yt] Redirect URL> ").strip()
        redirect = _normalize_redirect_input(raw)

        if not redirect:
            print("[yt] Empty input. Try again.")
            continue

        if _looks_like_google_error(redirect):
            print("[yt] ERROR: Pasted Google Error page.")
            print("[yt] This means the redirect_uri was STILL rejected by Google.")
            print("[yt] Check Google Cloud Console -> Credentials -> Your Client ID.")
            print("[yt] Ensure 'Authorized redirect URIs' contains EXACTLY:")
            print(f"[yt]   {REDIRECT_URI}")
            sys.exit(3)

        try:
            parsed = urlparse(redirect)
            qs = parse_qs(parsed.query)
        except Exception as e:
            print(f"[yt] Parse error: {e}")
            continue

        if "code" not in qs:
            print("[yt] No 'code' in URL. Paste full address bar URL.")
            continue
            
        returned_state = qs.get("state", [""])[0]
        if returned_state != state:
            print("[yt] ERROR: State mismatch. Re-run link command fresh.")
            sys.exit(3)

        code = qs["code"][0]
        
        # Exchange code for tokens manually using requests
        print("[yt] Exchanging code for tokens...")
        try:
            resp = requests.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": REDIRECT_URI,
                    "grant_type": "authorization_code",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30
            )
            resp.raise_for_status()
            token_data = resp.json()
            
            # Create a simple credentials object compatible with google-auth
            from google.oauth2.credentials import Credentials
            creds = Credentials(
                token=token_data["access_token"],
                refresh_token=token_data.get("refresh_token"),
                token_uri="https://oauth2.googleapis.com/token",
                client_id=client_id,
                client_secret=client_secret,
                scopes=SCOPES,
            )
            return creds
            
        except Exception as e:
            print(f"[yt] Token exchange failed: {e}")
            print(f"[yt] Response: {resp.text[:200]}")
            sys.exit(4)

    print("[yt] Too many attempts. Re-run link.")
    sys.exit(3)


def get_service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    _b64_env("YT_CLIENT_SECRETS_B64", SECRETS_FILE)
    _b64_env("YT_TOKEN_B64", TOKEN)

    creds = None

    if TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN.write_text(creds.to_json())
        else:
            if not SECRETS_FILE.exists():
                sys.exit("MISSING client_secrets.json")

            cfg = _load_client_config()
            client_id, client_secret = _get_credentials_from_json(cfg)
            
            if not client_id or not client_secret:
                sys.exit("[yt] ERROR: Missing client_id or client_secret in JSON")

            if _is_termux():
                creds = _manual_auth_manual_url(client_id, client_secret)
            else:
                # Fallback for PC: Use standard library flow
                from google_auth_oauthlib.flow import InstalledAppFlow
                flow = InstalledAppFlow.from_client_config(
                    {"installed": {"client_id": client_id, "client_secret": client_secret, "redirect_uris": [REDIRECT_URI]}},
                    SCOPES
                )
                try:
                    creds = flow.run_local_server(port=OAUTH_PORT, prompt="consent", access_type="offline")
                except Exception as e:
                    print(f"\n[yt] Auto-flow failed ({e}). Falling back to manual...")
                    creds = _manual_auth_manual_url(client_id, client_secret)

            TOKEN.write_text(creds.to_json())
            print("[yt] Linked! token.json created.")

            tok = json.loads(TOKEN.read_text())
            if "refresh_token" not in tok:
                print("[yt] *** WARNING: NO refresh_token. CI will fail after 1 hour. ***")
                print("[yt] Fix: Set Consent Screen to 'In Production', rm token.json, re-link.")
                sys.exit(2)

            print("[yt] OK: refresh_token present. Headless CI ready.")

    return build("youtube", "v3", credentials=creds)


# --- STANDARD FUNCTIONS BELOW (UNCHANGED FROM PREVIOUS WORKING VERSION) ---

def upload_one(yt, video_path, title, description, tags, thumb=None, category="22"):
    from googleapiclient.http import MediaFileUpload
    body = {
        "snippet": {"title": title[:100], "description": description, "tags": tags[:30], "categoryId": category},
        "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False},
    }
    request = yt.videos().insert(part="snippet,status", body=body, media_body=MediaFileUpload(str(video_path), chunksize=8*1024*1024, resumable=True))
    resp = None
    while resp is None:
        status, resp = request.next_chunk()
        if status: print(f"[yt]   uploading {int(status.progress()*100)}%")
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
    if not d.exists(): return None, None
    mp4 = next((f for f in sorted(d.glob("*.mp4"))), None)
    thumb = d / "thumb_16x9.jpg"
    return mp4, (thumb if thumb.exists() else None)

def cmd_link(args):
    get_service()

def cmd_upload(args):
    yt = get_service()
    entries = load_log()
    known_jobs = {e["job_id"] for e in entries}
    jobs = []
    if args.video:
        jobs = [{"id": Path(args.video).stem, "title": args.title or Path(args.video).stem, "description": args.description or "", "tags": (args.tags or "").split(",") if args.tags else [], "_video": args.video, "_thumb": args.thumb}]
    else:
        queue_file = args.queue or args.file or "input/scripts/billionaire-stories.json"
        raw = json.loads((ROOT / queue_file).read_text())
        banned = []
        spec = ROOT / "output" / "out-of-spec.json"
        if spec.exists():
            try: banned = json.loads(spec.read_text())
            except: pass
        jobs = [j for j in raw if j["id"] not in known_jobs and j["id"] not in banned]
    if not jobs:
        print("[yt] Nothing new to upload."); return
    for job in jobs:
        if "_video" in job:
            mp4 = Path(job["_video"]); thumb = job.get("_thumb")
        else:
            mp4, thumb = find_job_video(job["id"])
        if not mp4 or not Path(mp4).exists():
            print(f"[yt] {job['id']}: no rendered mp4 — skipped"); continue
        vid = upload_one(yt, mp4, job.get("title", job["id"]), job.get("description", ""), job.get("tags", []), thumb=thumb)
        entries.append({"job_id": job["id"], "video_id": vid, "title": job.get("title", job["id"]), "uploaded_at": now(), "observed_public_at": None, "expired_at": None, "status": "private"})
        save_log(entries)
    print("[yt] Upload pass complete. All videos PRIVATE until you publish them.")

def _set_privacy(yt, vid, status):
    yt.videos().update(part="status", body={"id": vid, "status": {"privacyStatus": status}}).execute()

def cmd_public(args):
    yt = get_service(); entries = load_log()
    target = entries[-1] if args.latest else next((e for e in entries if e["video_id"] == args.id), None)
    if not target: sys.exit("[yt] video not found in log")
    _set_privacy(yt, target["video_id"], "public")
    target["status"] = "public"; target["observed_public_at"] = now()
    save_log(entries)
    print(f"[yt] PUBLIC: {target['video_id']} — will auto-private in {args.days} days (expire cron).")

def cmd_private(args):
    yt = get_service(); entries = load_log()
    targets = entries if args.all else [e for e in entries if e["video_id"] == args.id]
    for e in targets:
        _set_privacy(yt, e["video_id"], "private"); e["status"] = "private"
    save_log(entries)
    print(f"[yt] {len(targets)} video(s) set to private.")

def cmd_expire(args):
    yt = get_service(); entries = load_log(); changed = 0
    for e in entries:
        try:
            live = yt.videos().list(part="status", id=e["video_id"]).execute()
        except Exception as ex:
            print(f"[yt] {e['video_id']}: status check failed ({ex})"); continue
        items = live.get("items", [])
        if not items: e["status"] = "gone"; continue
        privacy = items[0]["status"]["privacyStatus"]
        if privacy == "public":
            if not e.get("observed_public_at"):
                e["observed_public_at"] = now()
                print(f"[yt] {e['video_id']}: observed public, {args.days}-day clock started.")
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
    {"link": cmd_link, "upload": cmd_upload, "public": cmd_public, "private": cmd_private, "expire": cmd_expire, "list": cmd_list}[a.cmd](a)
