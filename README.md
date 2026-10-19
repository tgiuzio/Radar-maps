# Radar maps

Data packs for the microSD card of the SigmaRadar (Waveshare ESP32-P4 3.4" round board).
The files are attached to the [releases](../../releases); the board reads them from the card
under `/radar/`.

| Card path | Release files | What it is | Source and licence |
|---|---|---|---|
| `/radar/map/` | `l0.bin` `l1.bin` `l2.bin` `labels.bin` `manifest.txt` `pack.ok` | Detailed route map: coastlines, ocean depth, land cover, lakes, rivers, roads, borders, 14,000 place names | [Natural Earth](https://www.naturalearthdata.com/) 1:10m, public domain |
| `/radar/procs.bin` | `procs.bin` | US instrument procedures: approaches, SIDs, STARs (3,070 airports) | [FAA CIFP](https://www.faa.gov/air_traffic/flight_info/aeronav/digital_products/cifp/), US government data. Updated every 28 days (AIRAC): the [Procedures pack](.github/workflows/procedures.yml) workflow rebuilds it when a new cycle takes effect; the release notes and `CIFP_CYCLE` give the cycle. **Not for navigation.** |
| `/radar/apmaps.bin` | `apmaps.bin` | Airport diagrams: runways, taxiways, aprons, terminals, gates, hold-short lines | © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors, [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/). This file is a derived database under the same licence. |

## Installing on a card

The card must be formatted FAT32.

1. Download the files of the latest release.
2. Put the six map files in a folder `radar/map/` on the card, and `procs.bin` and
   `apmaps.bin` in `radar/`.
3. Put the card in the board and restart it.

The airport database (`/radar/db`) is not here: the board builds it from
[OurAirports](https://ourairports.com/data/) (public domain) on first boot.

## Scripts

`scripts/make_procedures.py` builds `procs.bin` from the FAA CIFP; `scripts/update_procedures.py`
checks daily (GitHub Actions) for a new cycle and replaces `procs.bin` in the latest release.
`python3 scripts/update_procedures.py --dry-run` builds it without uploading.
