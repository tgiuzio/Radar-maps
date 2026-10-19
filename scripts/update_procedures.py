#!/usr/bin/env python3
"""Keep procs.bin in the Radar-maps release on the FAA CIFP cycle in effect.

  python3 scripts/update_procedures.py [--force] [--dry-run] [--date YYYY-MM-DD]

The FAA publishes each 28-day CIFP cycle a few weeks before it takes effect
(https://www.faa.gov/air_traffic/flight_info/aeronav/digital_products/cifp/download/).
This picks the newest CIFP_YYMMDD.zip whose date has come, and when its AIRAC
cycle differs from the procs.bin listed in the release's files.txt, builds a
new procs.bin (scripts/make_procedures.py), uploads it and updates its line in
files.txt ("procs.bin <size> procs.bin cifp-<cycle>") and the cycle in the
release notes. Boards then fetch just that file with their "Install / update
maps" button. It also writes the cycle to CIFP_CYCLE (the workflow commits it,
which keeps GitHub from pausing the daily schedule of an idle repository).

Needs the gh CLI logged in with write access to the maps repo (the workflow's
own token in GitHub Actions). --dry-run builds but uploads nothing; --date
pretends today is another day (to test an upcoming cycle).
"""

import argparse
import datetime as dt
import io
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

MAPS_REPO = os.environ.get("MAPS_REPO", "tgiuzio/Radar-maps")
FAA_PAGE = "https://www.faa.gov/air_traffic/flight_info/aeronav/digital_products/cifp/download/"
OURAIRPORTS = "https://davidmegginson.github.io/ourairports-data/airports.csv"
USER_AGENT = "SigmaRadar/1.0 (+https://github.com/tgiuzio/Radar)"
# AIRAC cycles are 28 days apart; 2026-01-22 began cycle 2601.
AIRAC_EPOCH = dt.date(2026, 1, 22)


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def airac_cycle(effective):
    """'2610' for a cycle that takes effect on 2026-10-01 (cycles count from 01 each year)."""
    step = dt.timedelta(days=28)
    start = AIRAC_EPOCH + step * ((effective - AIRAC_EPOCH).days // 28)
    first = start
    while (first - step).year == start.year:
        first -= step
    return f"{start.year % 100:02d}{(start - first).days // 28 + 1:02d}"


def gh(*args, capture=True):
    return subprocess.run(["gh", *args], check=True, text=True,
                          capture_output=capture).stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="rebuild even if the cycle is current")
    ap.add_argument("--dry-run", action="store_true", help="build, but upload nothing")
    ap.add_argument("--date", help="pretend today is YYYY-MM-DD")
    args = ap.parse_args()
    today = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(dt.timezone.utc).date()

    page = get(FAA_PAGE).decode("utf-8", "replace")
    zips = {}
    for m in re.finditer(r'(https?://[^"\']*CIFP_(\d{6})\.zip)', page):
        d = m.group(2)
        zips[dt.date(2000 + int(d[:2]), int(d[2:4]), int(d[4:]))] = m.group(1)
    in_effect = sorted(d for d in zips if d <= today)
    if not in_effect:
        sys.exit(f"no CIFP in effect on {today} among {sorted(zips)}")
    effective = in_effect[-1]
    url = zips[effective]
    cycle = airac_cycle(effective)
    upcoming = [str(d) for d in sorted(zips) if d > today]
    print(f"CIFP in effect on {today}: cycle {cycle} from {effective} ({url})"
          + (f"; next: {', '.join(upcoming)}" if upcoming else ""))

    tag = gh("release", "view", "--repo", MAPS_REPO, "--json", "tagName", "-q", ".tagName").strip()
    with tempfile.TemporaryDirectory() as tmp:
        gh("release", "download", tag, "--repo", MAPS_REPO, "-p", "files.txt", "-D", tmp)
        files_txt = open(os.path.join(tmp, "files.txt")).read()
        m = re.search(r"^procs\.bin \d+ \S+ cifp-(\d+)$", files_txt, re.M)
        published = m.group(1) if m else None
        print(f"Release {tag}: procs.bin is cifp-{published}")
        if published == cycle and not args.force:
            print("Up to date.")
            return

        print("Downloading CIFP and OurAirports...", flush=True)
        with zipfile.ZipFile(io.BytesIO(get(url))) as z:
            name = next(n for n in z.namelist() if n.upper().endswith("FAACIFP18"))
            cifp_path = os.path.join(tmp, "FAACIFP18")
            with open(cifp_path, "wb") as f:
                f.write(z.read(name))
        oa_path = os.path.join(tmp, "airports.csv")
        with open(oa_path, "wb") as f:
            f.write(get(OURAIRPORTS))

        out = os.path.join(tmp, "procs.bin")
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "make_procedures.py")
        subprocess.run([sys.executable, script, cifp_path, oa_path, out], check=True)
        with open(out, "rb") as f:
            head = f.read(16)
        built = int.from_bytes(head[12:16], "little")
        if head[:4] != b"PRC1" or f"{built:04d}" != cycle:
            sys.exit(f"built pack says cycle {built}, expected {cycle}: not publishing")
        size = os.path.getsize(out)
        line = f"procs.bin {size} procs.bin cifp-{cycle}"
        if m:
            new_files = files_txt[:m.start()] + line + files_txt[m.end():]
        else:
            new_files = files_txt.rstrip("\n") + "\n" + line + "\n"
        print(f"Built procs.bin: {size} bytes, cycle {cycle}")
        if args.dry_run:
            print("Dry run: not uploading. files.txt would become:\n" + new_files)
            return
        with open(os.path.join(tmp, "files.txt"), "w") as f:
            f.write(new_files)
        # The pack first, then the list that points at it.
        gh("release", "upload", tag, out, "--repo", MAPS_REPO, "--clobber", capture=False)
        gh("release", "upload", tag, os.path.join(tmp, "files.txt"), "--repo", MAPS_REPO,
           "--clobber", capture=False)
        notes = gh("release", "view", tag, "--repo", MAPS_REPO, "--json", "body", "-q", ".body")
        new_notes = re.sub(r"CIFP cycle \d{4} \(effective [^)]*\)",
                           f"CIFP cycle {cycle} (effective {effective.day} "
                           f"{effective:%b} {effective.year})", notes)
        if new_notes != notes:
            gh("release", "edit", tag, "--repo", MAPS_REPO, "--notes", new_notes.strip())
        with open("CIFP_CYCLE", "w") as f:
            f.write(f"{cycle} {effective}\n")
        print(f"Published cifp-{cycle} to {MAPS_REPO} {tag}.")


if __name__ == "__main__":
    main()
