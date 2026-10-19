#!/usr/bin/env python3
"""Build the US instrument procedure pack (/radar/procs.bin on the card) from FAA CIFP.

The P4 board draws the approaches, departures (SIDs) and arrivals (STARs) of
an airport on the airport card from this pack, over the route map's base map.

  python3 scripts/make_procedures.py <FAACIFP18> <OurAirports airports.csv> <out file>

FAACIFP18 is the ARINC 424 file in the FAA's Coded Instrument Flight
Procedures download (https://www.faa.gov/air_traffic/flight_info/aeronav/
digital_products/cifp/download/; US government data, free to use). It is
updated every 28 days (AIRAC cycle). Not for navigation.

Each leg's path is worked out from its ARINC path terminator: fix-to-fix
legs are straight, RF / AF legs are arcs, holds are racetracks, and legs that
end at an altitude, a radial or radar vectors get a short line along their
course (their real length depends on the aircraft).

File (little-endian):
  header  "PRC1", u32 version, u32 airport count, u32 AIRAC cycle (e.g. 2610)
  index   count × (char ident[12], u32 offset, u32 size), sorted by ident
          (OurAirports idents, as in the board's airport database)
  blobs   per airport: u16 procedure count, then per procedure:
            u8 kind (1 approach, 2 SID, 3 STAR), u8 name length, u8 runways
            length, u8 0, u16 segment count, name ("ILS RWY 35R"), runways
            ("17L 17R" or "" = all), then per segment:
              u8 flags (1 = approach transition / en-route transition,
              2 = final approach / common route, 4 = runway transition),
              u8 name length, u16 point count, name (transition: "CRUSE"),
              then per point: i32 lat×1e7, i32 lon×1e7, u8 flags, u8 fix
              length, u8 altitude length, fix ident, altitude ("3000+")
                point flags: 1 = named fix, 2 = missed approach, 4 = IAF,
                8 = FAF, 16 = MAP, 32 = pen up before this point (vectors /
                open end), 64 = hold / course reversal
"""

import csv
import math
import struct
import sys
from collections import defaultdict

VERSION = 1

APPROACH, SID, STAR = 1, 2, 3
P_FIX, P_MISSED, P_IAF, P_FAF, P_MAP, P_PENUP, P_HOLD = 1, 2, 4, 8, 16, 32, 64

APPROACH_TYPES = {
    "B": "LOC/BC", "D": "VOR/DME", "F": "FMS", "G": "IGS", "H": "RNAV (RNP)",
    "I": "ILS", "J": "GLS", "L": "LOC", "N": "NDB", "P": "GPS", "Q": "NDB/DME",
    "R": "RNAV (GPS)", "S": "VOR", "T": "TACAN", "U": "SDF", "V": "VOR", "X": "LDA",
}

NM_M = 1852.0
EARTH_M = 6371000.0


def col(line, a, b):
    """1-based inclusive columns, as in the ARINC 424 spec."""
    return line[a - 1:b]


def coord(s):
    """'N28254582' / 'W081183237' -> degrees."""
    s = s.strip()
    if len(s) < 9:
        return None
    hemi, digits = s[0], s[1:]
    deg_len = 2 if hemi in "NS" else 3
    try:
        d = int(digits[:deg_len])
        m = int(digits[deg_len:deg_len + 2])
        sec = int(digits[deg_len + 2:]) / 100.0
    except ValueError:
        return None
    v = d + m / 60.0 + sec / 3600.0
    return -v if hemi in "SW" else v


def latlon(line, a=33):
    lat = coord(col(line, a, a + 8))
    lon = coord(col(line, a + 9, a + 18))
    if lat is None or lon is None:
        return None
    return (lat, lon)


def number(s, scale=1.0):
    s = s.strip()
    try:
        return int(s) * scale if scale != 1.0 else int(s)
    except ValueError:
        return None


# --- spherical helpers ---

def dest(p, bearing_deg, dist_m):
    lat1, lon1 = math.radians(p[0]), math.radians(p[1])
    b = math.radians(bearing_deg)
    d = dist_m / EARTH_M
    lat2 = math.asin(math.sin(lat1) * math.cos(d) + math.cos(lat1) * math.sin(d) * math.cos(b))
    lon2 = lon1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(lat1),
                             math.cos(d) - math.sin(lat1) * math.sin(lat2))
    return (math.degrees(lat2), (math.degrees(lon2) + 540) % 360 - 180)


def bearing(a, b):
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1])
    y = math.sin(dl) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def distance(a, b):
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlat = lat2 - lat1
    dl = math.radians(b[1] - a[1])
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_M * math.asin(min(1.0, math.sqrt(h)))


def arc(start, end, center, turn):
    """Points from start to end around center, turning L or R."""
    b0, b1 = bearing(center, start), bearing(center, end)
    r = (distance(center, start) + distance(center, end)) / 2
    sweep = (b1 - b0) % 360 if turn == "R" else -((b0 - b1) % 360)
    n = max(2, int(abs(sweep) / 6))
    return [dest(center, b0 + sweep * i / n, r) for i in range(1, n + 1)]


def racetrack(fix, inbound_true, turn, leg_m):
    """Hold at fix: the inbound leg ends at the fix on inbound_true; turns are
    to the turn side (right unless L), standard rate at ~210 kt."""
    r = 1.5 * NM_M
    kx = 1.0 / (111320.0 * math.cos(math.radians(fix[0])))
    ky = 1.0 / 110540.0

    def at(x, y):  # metres east / north of the fix
        return (fix[0] + y * ky, fix[1] + x * kx)

    c = math.radians(inbound_true)
    ux, uy = math.sin(c), math.cos(c)                # inbound direction
    sx, sy = (uy, -ux) if turn != "L" else (-uy, ux)  # towards the hold side
    pts = []
    # Turn from the fix onto the outbound leg, around a centre r to the side.
    cx, cy = sx * r, sy * r
    for i in range(1, 7):
        a = math.pi * i / 6
        # From the fix (-side from the centre), sweeping ahead (+inbound) to +side.
        x = cx - sx * r * math.cos(a) + ux * r * math.sin(a)
        y = cy - sy * r * math.cos(a) + uy * r * math.sin(a)
        pts.append(at(x, y))
    # Outbound leg, then the turn back to the inbound leg.
    ox, oy = 2 * sx * r - ux * leg_m, 2 * sy * r - uy * leg_m
    pts.append(at(ox, oy))
    cx, cy = sx * r - ux * leg_m, sy * r - uy * leg_m
    for i in range(1, 7):
        a = math.pi * i / 6
        x = cx + sx * r * math.cos(a) - ux * r * math.sin(a)
        y = cy + sy * r * math.cos(a) - uy * r * math.sin(a)
        pts.append(at(x, y))
    pts.append(fix)
    return pts


# --- reading the CIFP file ---

class Cifp:
    def __init__(self, path):
        self.airports = {}            # ident -> (lat, lon, variation (+E), elev ft)
        self.terminal = {}            # (airport, ident) -> (lat, lon)  waypoints, NDBs
        self.runways = {}             # (airport, "RW17L") -> (lat, lon)
        self.localizers = {}          # (airport, ident) -> (lat, lon)
        self.enroute = {}             # (ident, icao) -> (lat, lon)
        self.navaids = {}             # (ident, icao) -> (lat, lon)
        self.legs = defaultdict(list)  # (airport, kind, ident, route type, transition) -> legs
        self.cycle = 0
        with open(path, encoding="ascii", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                if line.startswith("HDR01"):
                    try:
                        self.cycle = int(line[35:39])
                    except ValueError:
                        pass
                if len(line) < 120 or line[0] != "S":
                    continue
                self.read(line)

    def read(self, line):
        sec, sub = col(line, 5, 5), col(line, 6, 6)
        if sec == "P":
            airport = col(line, 7, 10).strip()
            sub = col(line, 13, 13)
            cont = col(line, 22, 22)
            if sub == "A":
                p = latlon(line)
                var = col(line, 52, 56)
                v = number(var[1:], 0.1) if var[:1] in "EW" else 0.0
                if p:
                    self.airports[airport] = (p[0], p[1], (v or 0.0) * (-1 if var[:1] == "W" else 1),
                                              number(col(line, 57, 61)) or 0)
            elif sub in "CN" and cont in "01":
                p = latlon(line)
                if p:
                    self.terminal[(airport, col(line, 14, 18).strip())] = p
            elif sub == "G" and cont in "01":
                p = latlon(line)
                if p:
                    self.runways[(airport, col(line, 14, 18).strip())] = p
            elif sub == "I" and cont in "01":
                p = latlon(line)
                if p:
                    self.localizers[(airport, col(line, 14, 17).strip())] = p
            elif sub in "DEF" and col(line, 39, 39) in "01":
                kind = {"D": SID, "E": STAR, "F": APPROACH}[sub]
                key = (airport, kind, col(line, 14, 19).strip(), col(line, 20, 20),
                       col(line, 21, 25).strip())
                self.legs[key].append(line)
        elif sec == "E" and sub == "A" and col(line, 22, 22) in "01":
            p = latlon(line)
            if p:
                self.enroute[(col(line, 14, 18).strip(), col(line, 20, 21))] = p
        elif sec == "D" and col(line, 22, 22) in "01":
            p = latlon(line) or latlon(line, 56)  # VOR, else the DME
            if p:
                self.navaids[(col(line, 14, 17).strip(), col(line, 20, 21))] = p

    def fix(self, airport, ident, icao, section):
        if not ident:
            return None
        if section in ("PC", "PN"):
            return self.terminal.get((airport, ident)) or self.enroute.get((ident, icao))
        if section == "PG":
            return self.runways.get((airport, ident))
        if section == "PI":
            return self.localizers.get((airport, ident))
        if section == "PA":
            a = self.airports.get(ident)
            return (a[0], a[1]) if a else None
        if section == "EA":
            return self.enroute.get((ident, icao)) or self.terminal.get((airport, ident))
        if section[0] == "D":
            return self.navaids.get((ident, icao))
        return (self.terminal.get((airport, ident)) or self.enroute.get((ident, icao)) or
                self.navaids.get((ident, icao)))


def altitude_text(line):
    desc = col(line, 83, 83)
    a1 = col(line, 85, 89).strip()
    a2 = col(line, 90, 94).strip()

    def fmt(a):
        if a.startswith("FL"):
            return a
        n = number(a)
        return str(n) if n is not None else ""

    a1, a2 = fmt(a1), fmt(a2)
    if not a1:
        return ""
    if desc == "+":
        return a1 + "+"
    if desc == "-":
        return a1 + "-"
    if desc == "B" and a2:
        return f"{a2}-{a1}"
    return a1


def approach_name(ident):
    t = APPROACH_TYPES.get(ident[0], ident[0])
    rest = ident[1:]
    if rest[:1].isdigit():
        rwy = rest[:2]
        rest = rest[2:]
        if rest and rest[0] in "LRC":
            rwy += rest[0]
            rest = rest[1:]
        letter = rest.strip("-")
        return f"{t}{' ' + letter if letter else ''} RWY {rwy}", rwy
    return f"{t}-{rest.strip('-')}" if rest else t, ""


def runways_of(transition):
    """'RW17L' -> ['17L'], 'RW17B' -> ['17L', '17R', '17C'], 'ALL' -> []."""
    if not transition.startswith("RW"):
        return []
    r = transition[2:]
    if r.endswith("B"):
        return [r[:-1] + s for s in "LRC"]
    return [r]


def departure_start(cifp, airport, transition):
    """Where a SID's runway transition starts: the far end of the runway (lift-off)."""
    rws = runways_of(transition)
    for rw in rws:
        try:
            n = int(rw[:2])
        except ValueError:
            continue
        side = {"L": "R", "R": "L", "C": "C"}.get(rw[2:], "")
        far = f"RW{(n + 17) % 36 + 1:02d}{side}"
        p = cifp.runways.get((airport, far)) or cifp.runways.get((airport, "RW" + rw))
        if p:
            return p
    return None


def build_path(cifp, airport, legs, var, elev_ft, is_approach_final, start=None):
    """Points (lat, lon, flags, fix, alt) of one transition / route; start =
    where it begins when its first leg has no fix (SIDs)."""
    pts = []
    cur = None
    first = sorted(legs, key=lambda l: col(l, 27, 29))[0]
    if start is not None and col(first, 48, 49) not in ("IF", "FA", "FC", "FD", "FM"):
        pts.append((start[0], start[1], 0, "", ""))
        cur = start
    missed = False
    last_course = None
    for line in sorted(legs, key=lambda l: col(l, 27, 29)):
        pt = col(line, 48, 49)
        ident = col(line, 30, 34).strip()
        fix = cifp.fix(airport, ident, col(line, 35, 36), col(line, 37, 38).strip())
        desc = col(line, 40, 43)
        turn = col(line, 44, 44)
        course = number(col(line, 71, 74), 0.1)
        true_course = (course + var) % 360 if course is not None else last_course
        dist_field = col(line, 75, 78)
        alt = altitude_text(line)
        flags = P_MISSED if missed else 0
        if desc[3] == "A" or desc[3] == "C" or desc[3] == "D":
            flags |= P_IAF
        if desc[3] == "F":
            flags |= P_FAF
        if desc[3] == "M" and is_approach_final:
            flags |= P_MAP

        def add(p, f=0, name="", a=""):
            pts.append((p[0], p[1], flags | f, name, a))

        if pt in ("IF", "TF", "CF", "DF", "AF", "RF") and fix:
            if pt == "RF" and cur:
                center = cifp.fix(airport, col(line, 107, 111).strip(), col(line, 113, 114),
                                  col(line, 115, 116).strip())
                if center:
                    for p in arc(cur, fix, center, turn)[:-1]:
                        add(p)
            elif pt == "AF" and cur:
                center = cifp.fix(airport, col(line, 51, 54).strip(), col(line, 55, 56),
                                  col(line, 79, 80).strip())
                if center:
                    for p in arc(cur, fix, center, turn)[:-1]:
                        add(p)
            add(fix, P_FIX, ident, alt)
            if cur and pt != "IF":
                last_course = bearing(cur, fix)
            cur = fix
        elif pt in ("HA", "HF", "HM") and fix:
            if cur is None or distance(cur, fix) > 50:
                add(fix, P_FIX, ident, alt)
            inbound = true_course if true_course is not None else (last_course or 0)
            if dist_field.startswith("T"):
                leg_m = (number(dist_field[1:], 0.1) or 1.0) * 3.5 * NM_M  # ~210 kt
            else:
                leg_m = (number(dist_field, 0.1) or 4.0) * NM_M
            for p in racetrack(fix, inbound, turn, leg_m):
                add(p, P_HOLD)
            cur = fix
        elif pt == "PI" and fix:
            # Procedure turn: outbound, a 45° turn, and back.
            if cur is None or distance(cur, fix) > 50:
                add(fix, P_FIX, ident, alt)
            out = (true_course if true_course is not None else (last_course or 0))
            a = dest(fix, out, 4 * NM_M)
            side = -45 if turn == "L" else 45
            b = dest(a, out + side, 1.5 * NM_M)
            add(a, P_HOLD)
            add(b, P_HOLD)
            cur = fix
        elif pt in ("FA", "FC", "FD", "FM") and fix:
            if cur is None or distance(cur, fix) > 50:
                add(fix, P_FIX, ident, alt)
            d = number(dist_field, 0.1) if pt in ("FC", "FD") else None
            end = dest(fix, true_course or 0, (d or 3.0) * NM_M)
            add(end, P_PENUP if pt == "FM" else 0, "", "" if pt == "FM" else alt)
            cur = end
            last_course = true_course
        elif pt in ("CA", "VA", "CD", "VD", "CI", "VI", "CR", "VR", "VM") and cur:
            # Open end (altitude, DME, intercept, radial, vectors): a short
            # line along the course.
            if pt in ("CA", "VA"):
                target = number(col(line, 85, 89)) or 0
                d = min(5.0, max(1.0, (target - elev_ft) / 300.0)) if target else 2.0
            else:
                d = 3.0
            if true_course is not None:
                end = dest(cur, true_course, d * NM_M)
                add(end, P_PENUP if pt == "VM" else 0, "", alt if pt in ("CA", "VA") else "")
                cur = end
                last_course = true_course
        if flags & P_MAP:
            missed = True
    return pts


def main():
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    cifp_path, oa_path, out_path = sys.argv[1:]
    cifp = Cifp(cifp_path)
    print(f"CIFP cycle {cifp.cycle}: {len(cifp.airports)} airports, "
          f"{len(cifp.legs)} procedure routes", flush=True)

    # CIFP ident -> OurAirports ident (the board looks diagrams and
    # procedures up by the OurAirports ident).
    oa = {}
    with open(oa_path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["iso_country"] not in ("US", "PR", "VI", "GU", "AS", "MP"):
                continue
            for code in (r["ident"], r["gps_code"], r["icao_code"], r["local_code"]):
                if code and code not in oa:
                    oa[code] = r["ident"]
            oa[r["ident"]] = r["ident"]

    # Group routes into procedures.
    procs = defaultdict(lambda: defaultdict(list))  # airport -> (kind, ident) -> [(rt, trans, legs)]
    for (airport, kind, ident, rt, trans), legs in cifp.legs.items():
        procs[airport][(kind, ident)].append((rt, trans, legs))

    blobs = {}
    total = 0
    for airport, by_proc in procs.items():
        if airport not in cifp.airports:
            continue
        lat0, lon0, var, elev = cifp.airports[airport]
        out = []
        for (kind, ident), routes in sorted(by_proc.items()):
            if kind == APPROACH:
                name, rwy = approach_name(ident)
                runways = [rwy] if rwy else []
            else:
                name = ident
                runways = sorted({r for _, t, _ in routes for r in runways_of(t)})
            segs = []
            for rt, trans, legs in sorted(routes, key=lambda r: (r[0], r[1])):
                if kind == APPROACH:
                    final = rt != "A"
                    flag = 2 if final else 1
                elif kind == SID:
                    flag = {"1": 4, "4": 4, "F": 4, "T": 4}.get(rt, 2 if rt in "25M" else 1)
                    final = False
                else:
                    flag = {"3": 4, "6": 4, "9": 4, "S": 4}.get(rt, 2 if rt in "25M8" else 1)
                    final = False
                start = None
                if kind == SID:
                    start = departure_start(cifp, airport, trans)
                    if start is None and flag == 2:  # common route without runway transitions
                        start = (lat0, lon0)
                pts = build_path(cifp, airport, legs, var, elev, kind == APPROACH and final,
                                 start)
                if len(pts) >= 1:
                    segs.append((flag, trans if trans != "ALL" else "", pts))
            if segs:
                out.append((kind, name, " ".join(runways), segs))
        if not out:
            continue
        b = bytearray(struct.pack("<H", len(out)))
        for kind, name, runways, segs in out:
            nb, rb = name.encode()[:40], runways.encode()[:40]
            b += struct.pack("<BBBBH", kind, len(nb), len(rb), 0, len(segs)) + nb + rb
            for flag, trans, pts in segs:
                tb = trans.encode()[:8]
                b += struct.pack("<BBH", flag, len(tb), len(pts)) + tb
                for lat, lon, f, fix, alt in pts:
                    fb, ab = fix.encode()[:8], alt.encode()[:12]
                    b += struct.pack("<iiBBB", round(lat * 1e7), round(lon * 1e7), f, len(fb),
                                     len(ab)) + fb + ab
        ident = oa.get(airport, airport)
        if len(ident) > 11:
            continue
        blobs[ident] = bytes(b)
        total += len(out)

    idents = sorted(blobs)
    header = struct.pack("<4sIII", b"PRC1", VERSION, len(idents), cifp.cycle)
    offset = len(header) + 20 * len(idents)
    index = bytearray()
    for ident in idents:
        index += struct.pack("<12sII", ident.encode(), offset, len(blobs[ident]))
        offset += len(blobs[ident])
    tmp = out_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(header)
        f.write(index)
        for ident in idents:
            f.write(blobs[ident])
    import os
    os.replace(tmp, out_path)
    print(f"{out_path}: {len(idents)} airports, {total} procedures, {offset / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
