# dji-ppk

Docker Compose service that post-processes DJI RTK drone flights (tested with a Matrice 4E) against
ESTPOS base station data and produces camera positions for photogrammetry (WebODM `geo.txt`).

Engine: **RTKLIB-EX** from [rtklibexplorer/RTKLIB](https://github.com/rtklibexplorer/RTKLIB), tag `v2.5.1`.
This is the continuation of the well known `demo5` branch, which was retired on 2025-07-26 (last tag `b34L`);
all development by the same author now happens on `main`. The tag is a build argument (`RTKLIB_REF`), so
`demo5`/`b34L` can still be built for comparison.

## Reference result

A 20 min, 5 Hz flight with 1230 photos and an ESTPOS Virtual RINEX base 130 m from the site, compared with
Emlid Studio 1.10 (1229/1230 photos fixed). The comparison report is in `examples/reference-result/`.

| configuration | photos fixed | trajectory fixed | median 3D vs Emlid |
|---|---|---|---|
| `dji_m4e.conf` (L1+L2+L5, base PCO+PCV) **default** | 1230/1230 | 99.6 % | 5.8 mm (1.5 mm horizontal, 5.7 mm vertical) |
| default + `--set pos1-posopt2=off` (PCO only) | 1230/1230 | 99.5 % | 2.1 mm |
| `dji_m4e_l1l2.conf` (L1+L2) | 1230/1230 | 100 % | 14.1 mm (vertical) |
| RTKLIB-EX `main` @ `06e86442` (2026-08-31), same conf | 1230/1230 | 99.0 % | 5.8 mm (photo positions equal to v2.5.1 within 0.5 mm) |
| `--set pos1-navsys=63` (GLONASS + BeiDou on) | 0 | 0 % | no solution |
| `--set misc-timeinterp=on` | 0 | – | RTKLIB writes no event solutions |

Processing time is about 5 s per flight.

The 76 commits on `main` after v2.5.1 (checked 2026-09-21) only touch the solver for urban-canyon use:
reference-satellite choice by variance, satellite-count semantics for `pos2-min*sats` (see
`config/dji_m4e_main.conf`), and a doppler slip detector that is off by default. On this flight they
produce the same photo positions (within 0.5 mm) and a few more float epochs during the initial
convergence before the first photo (trajectory fix ratio 99.0 % vs 99.6 %), so `v2.5.1` stays the default.
Build any other version with `RTKLIB_REF=<tag|branch> docker compose --profile cli build ppk`.

## Layout

```
Dockerfile           multi-stage build: RTKLIB-EX (rnx2rtkp, convbin, pos2kml), crx2rnx, IGS ANTEX, python package
compose.yaml         services `ppk` (one-shot CLI, profile "cli") and `ppk-watch` (folder watcher)
.env.example         template for host directories (FLIGHTS_DIR, BASE_DIR, OUT_DIR), poll interval, RTKLIB_REF
config/dji_m4e.conf  RTKLIB options (three frequencies, GPS+SBAS+Galileo+QZSS, fix-and-hold, combined filter)
config/dji_m4e_l1l2.conf  two-frequency variant
config/dji_m4e_main.conf  variant for RTKLIB-EX `main` (satellite-count semantics differ from v2.5.1)
ppk/                 python package `ppk` (stdlib only) with the CLI, parsers and tests
examples/            reference comparison report for the flight above
```

Mounted paths inside the containers: `/data/flights` (read-only), `/data/base` (read-only), `/data/out`.

## Usage

```sh
cp .env.example .env            # adjust FLIGHTS_DIR / BASE_DIR / OUT_DIR
docker compose --profile cli build ppk

# 1. What to order from ESTPOS for a flight folder
docker compose --profile cli run --rm ppk estpos-window /data/flights/<flight>

# 2. Order the Virtual RINEX at https://gnss-rtk.maaamet.ee/sbc
#    (Post Processing -> RINEX Data -> tick "Virtual RINEX", enter the printed lat/lon/height,
#    date, quarter-hour start and length), download it into BASE_DIR, then validate it:
docker compose --profile cli run --rm ppk check-base /data/base/<file> --flight /data/flights/<flight>

# 3. Process
docker compose --profile cli run --rm ppk process /data/flights/<flight> --base /data/base/<file>

# 4. Compare against an Emlid Studio *_events.pos (also done automatically when one is in the flight folder)
docker compose --profile cli run --rm ppk compare /data/out/<flight> "/data/flights/<flight>/<name>_events.pos"
docker compose --profile cli run --rm ppk compare --antenna ...   # raw antenna positions, no lever arm
```

Options for `process`: `--conf <file>`, `--set key=value` (repeatable RTKLIB override), `--geo-accuracy`
(adds horizontal/vertical accuracy columns to `geo.txt`), `--fixed-only`, `--keep-work`, `--name`.
Without `--base` the first RINEX file inside the flight folder or under `/data/base` that covers the flight is used.

### Watcher

```sh
docker compose up -d ppk-watch
docker compose logs -f ppk-watch
```

Every `PPK_POLL_SECONDS` the watcher scans `/data/flights` for folders containing an `.OBS`/`.NAV`/`.MRK`
triplet. A flight is processed once its files are stable and a base file covering it exists (in the flight
folder or in `/data/base`). Results go to `/data/out/<flight>/`; a `DONE` marker prevents reprocessing and a
`FAILED.log` records errors (retried when the input files change).

## Outputs (`/data/out/<flight>/`)

| file | content |
|---|---|
| `geo.txt` | WebODM/ODM geo file: `EPSG:4326`, then `<image> <lon> <lat> <ellipsoidal height>` per photo (camera position) |
| `events.csv` | per photo: image, GPST, camera lat/lon/h, Q, satellites, std, ratio, antenna lat/lon/h, MRK offsets, DJI RTK position |
| `<obs>_trajectory.pos` | full 5 Hz RTKLIB trajectory (antenna) |
| `<obs>_trajectory_events.pos` | RTKLIB solutions at the exposure times (antenna) |
| `summary.json` | counts, fix ratios, inputs, versions, comparison statistics |
| `compare_report.txt` | comparison with the best reference `*_events*.pos` found in the flight folder |
| `rtklib.log`, `rtklib_used.conf` | exact command, messages and options used |

Camera positions apply the DJI `.MRK` lever arm: camera = antenna + N, + E (mm), height − V. This matches
Emlid Studio (verified to ~1 mm). Image names are the real `DJI_..._NNNN_V.JPG` files matched by the photo index.

## How it works

1. Parse the `.MRK` (photo index, GPS week/seconds with microseconds, lever arm, DJI RTK position).
2. Copy the rover `.OBS` into the work directory inserting one RINEX event record (epoch flag 5) per
   exposure. RTKLIB interpolates its solution to those times and writes `*_events.pos`, exactly as Emlid does.
3. Run `rnx2rtkp -k <conf> -o <trajectory.pos> <rover_events.obs> <base.obs> <rover.nav> [<base.nav>]`.
   The base position comes from the RINEX header and the base antenna (`LEIAR25.R4 LEIT` in ESTPOS
   Virtual RINEX files) is corrected with `igs20.atx`, which is downloaded at image build time.
4. Match the event solutions back to the MRK rows (±1.5 ms), apply the lever arm, write the outputs.

## Notes and pitfalls

- GPS L5, Galileo E5a/E5b and Galileo E1 use different tracking codes on the DJI (I, B) and Leica (Q, C)
  receivers. RTKLIB-EX resolves ambiguities fine anyway; `misc-rnxopt1/2` (e.g. `-EL5Q=-0.25`) exists
  for per-receiver code selection/phase shifts if another base ever needs it.
- The `.trace` files that Emlid Studio leaves in flight folders can be gigabytes; the flight folder is
  mounted read-only and nothing in it is copied except the `.OBS` (with events) into the work directory.
- Base files may be `.??o`, RINEX 3 long names (`.rnx`), Hatanaka (`.crx`, `.??d`), `.gz`, `.Z` or `.zip`.
- ESTPOS has no API; the portal is Leica Spider Business Center. Files are available for 90 days.
- GPST vs UTC: the ESTPOS order form is in UTC (GPST − 18 s); RTKLIB output and `events.csv` are GPST.

## Development

```sh
cd ppk && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests      # host
docker compose --profile cli run --rm --entrypoint pytest ppk /app/ppk/tests   # in the image
docker compose --profile cli build --build-arg RTKLIB_REF=b34L ppk       # alternative engine version
```

The test fixtures are excerpts of real DJI and ESTPOS files. Their coordinates are shifted by a few hundred metres
and the raw observations in `sample.obs` are perturbed, so they exercise the parsers but cannot be used for positioning.

## License

MIT, see `LICENSE`. The Docker image builds and bundles third-party software under its own terms:

- [RTKLIB-EX](https://github.com/rtklibexplorer/RTKLIB) (`rnx2rtkp`, `convbin`, `pos2kml`), BSD-2-Clause, Copyright T. Takasu and rtklibexplorer
- [RNXCMP](https://terras.gsi.go.jp/ja/crx2rnx.html) (`crx2rnx`), Geospatial Information Authority of Japan, redistributable with attribution
- [IGS ANTEX](https://files.igs.org/pub/station/general/) `igs20.atx`, antenna calibration data from the International GNSS Service
