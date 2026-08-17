"""
Blacksite — import an existing Chrome profile into a persona's browser dir.

Use case: boss has a Chrome profile already logged into the persona's Google
account (and possibly target sites like Bigo). Copy that profile into the
persona's isolated browser dir so engine Playwright (channel='chrome') can
launch it as a persistent context with all cookies / localStorage / IndexedDB
intact.

Usage:
  py scripts/import_chrome_profile.py --persona P03 --source "Profile 3"
  py scripts/import_chrome_profile.py --persona P03 --source "Profile 3" --dest-name chrome

Pre-conditions:
  - Chrome MUST be closed completely (lockfile / sqlite WAL conflicts otherwise)
  - PERSONA_<id>_GMAIL in .env should match the source profile's account
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
USER_DATA = Path(os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data"))


def chrome_running() -> bool:
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
            text=True, errors="replace",
        )
        return "chrome.exe" in out.lower()
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--persona", required=True)
    ap.add_argument("--source", required=True,
                    help='Source profile dir name e.g. "Profile 3" or "Default"')
    ap.add_argument("--dest-name", default="chrome",
                    help="subdir under personas/<id>/browser/ (default: chrome)")
    ap.add_argument("--force", action="store_true",
                    help="overwrite existing dest dir")
    args = ap.parse_args()

    src = USER_DATA / args.source
    if not src.is_dir():
        print(f"ERROR: source not found: {src}")
        sys.exit(1)

    if chrome_running():
        print("ERROR: Chrome is running. Close ALL Chrome windows first.")
        print("       (including any browsers connected via Claude in Chrome MCP)")
        sys.exit(2)

    dest_root = ROOT / "personas" / args.persona / "browser" / args.dest_name
    dest_default = dest_root / "Default"
    if dest_root.exists():
        if not args.force:
            print(f"ERROR: dest exists: {dest_root}")
            print("       pass --force to overwrite, or delete it first")
            sys.exit(3)
        shutil.rmtree(dest_root)

    dest_root.mkdir(parents=True, exist_ok=True)

    print(f"copying profile data:")
    print(f"  src  = {src}")
    print(f"  dest = {dest_default}")
    print(f"  size approx = ", end="", flush=True)
    try:
        total = sum(f.stat().st_size for f in src.rglob("*") if f.is_file())
        print(f"{total / 1024 / 1024:.1f} MB")
    except Exception:
        print("?")

    skip_dirs = {"Service Worker", "Cache", "Code Cache", "GPUCache",
                 "GrShaderCache", "DawnCache", "DawnGraphiteCache",
                 "GraphiteDawnCache", "ShaderCache", "Crash Reports",
                 "blob_storage", "VideoDecodeStats"}

    def ignore(dir_path, names):
        return [n for n in names if n in skip_dirs]

    shutil.copytree(src, dest_default, ignore=ignore, dirs_exist_ok=True)

    src_local_state = USER_DATA / "Local State"
    if src_local_state.exists():
        try:
            data = json.loads(src_local_state.read_text(encoding="utf-8"))
            minimal = {
                "profile": {
                    "info_cache": {
                        "Default": data.get("profile", {}).get("info_cache", {}).get(
                            args.source, {"name": args.persona, "user_name": ""}
                        )
                    },
                    "last_used": "Default",
                    "profiles_created": 1,
                },
                "user_experience_metrics": data.get("user_experience_metrics", {}),
                "os_crypt": data.get("os_crypt", {}),
            }
            (dest_root / "Local State").write_text(json.dumps(minimal, indent=2), encoding="utf-8")
            print(f"  wrote {dest_root / 'Local State'}")
        except Exception as e:
            print(f"  WARN: Local State rewrite failed: {e}")

    for stale in ["lockfile", "Default/lockfile", "SingletonLock",
                  "SingletonCookie", "SingletonSocket"]:
        p = dest_root / stale
        if p.exists():
            try: p.unlink()
            except Exception: pass

    cookies_db = dest_default / "Network" / "Cookies"
    print(f"\nverify:")
    print(f"  Cookies sqlite: {cookies_db.exists()} ({cookies_db.stat().st_size if cookies_db.exists() else 0} bytes)")
    indexeddb = dest_default / "IndexedDB"
    print(f"  IndexedDB dir:  {indexeddb.exists()}")
    local_storage = dest_default / "Local Storage"
    print(f"  Local Storage:  {local_storage.exists()}")

    print(f"\n✅ profile imported. next:")
    print(f"  py agents/bigo/login.py --persona {args.persona} --use-chrome --probe-only")


if __name__ == "__main__":
    main()
