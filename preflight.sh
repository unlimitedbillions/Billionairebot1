#!/usr/bin/env bash
# preflight.sh — validates every custom file before pushing
cd "$(dirname "$0")" || exit 1
FAIL=0

echo "== JSON =="
for f in input/scripts/billionaire-stories.json input/thumbnails.json; do
  if [ -f "$f" ]; then
    python -m json.tool "$f" > /dev/null 2>&1 && echo "OK   $f" || { echo "FAIL $f"; FAIL=1; }
  else echo "MISS $f"; FAIL=1; fi
done

echo "== NODE (ESM syntax) =="
for f in scripts/verify-durations.mjs scripts/make-thumbnails.mjs scripts/publish-safe.mjs; do
  if [ -f "$f" ]; then
    node --check "$f" 2>/dev/null && echo "OK   $f" || { echo "FAIL $f"; FAIL=1; }
  else echo "MISS $f"; FAIL=1; fi
done

echo "== PYTHON =="
if [ -f tools/youtube_oauth.py ]; then
  python -m py_compile tools/youtube_oauth.py 2>/dev/null && echo "OK   tools/youtube_oauth.py" || { echo "FAIL tools/youtube_oauth.py"; FAIL=1; }
else echo "MISS tools/youtube_oauth.py"; FAIL=1; fi

echo "== YAML =="
python - <<'EOF'
import sys
try:
    import yaml
except ImportError:
    print("SKIP yaml checks (pip install pyyaml to enable)"); sys.exit(0)
for f in [".github/workflows/billionaire-stories.yml", ".github/workflows/youtube-privacy.yml"]:
    try:
        yaml.safe_load(open(f)); print("OK  ", f)
    except Exception as e:
        print("FAIL", f, "-", e); sys.exit(1)
EOF
[ $? -eq 0 ] || FAIL=1

echo "== DURATION LAW (word counts) =="
python - <<'EOF'
import json, re
try:
    jobs = json.load(open("input/scripts/billionaire-stories.json"))
except Exception:
    print("skip (json invalid)"); raise SystemExit(0)
for j in jobs:
    text = re.sub(r"\[[^\]]*\]", "", j.get("script", ""))
    words = len(text.split())
    kind = "short" if j["id"].endswith("-short") else "long"
    lo, hi = (150, 170) if kind == "short" else (380, 430)
    print(("OK  " if lo <= words <= hi else "WARN"), j["id"], f"{words} words (target {lo}-{hi})")
EOF

echo ""
[ $FAIL -eq 0 ] && echo "ALL CHECKS PASSED — safe to push ✅" || echo "FIX THE FAILURES BEFORE PUSH ❌"
exit $FAIL
