# dji-ppk

Docker Compose service that post-processes DJI RTK drone flights (tested with a Matrice 4E) against
ESTPOS base station data and produces camera positions for photogrammetry (WebODM `geo.txt`).

ESTPOS is the Estonian national GNSS reference station network run by the Land Board (Maa-amet); its portal
delivers Virtual RINEX files, base station observations computed for any point you choose, which this tool uses
as the base for post-processed kinematic (PPK) correction of the drone's positions, replacing an on-site base or
a real-time RTK link.

Engine: **RTKLIB-EX** from [rtklibexplorer/RTKLIB](https://github.com/rtklibexplorer/RTKLIB), tag `v2.5.1`,
selectable with the `RTKLIB_REF` build argument. RTKLIB is compiled with four carrier frequency slots
(`RTKLIB_NFREQ=4`, upstream default 3) so `pos1-frequency=l1+l2+l5+l6` is accepted; the default configuration
still uses three, because the fourth slot made both test flights worse (table below).

## Reference result

A 20 min, 5 Hz flight with 1230 photos and an ESTPOS Virtual RINEX base 130 m from the site, compared with
Emlid Studio 1.10 (1229/1230 photos fixed). The comparison report is in `examples/reference-result/`.
Emlid was fed the DJI `.NAV` only, so its reference is a Galileo-only solution (see the NAV note below); the
rows with base navigation files therefore differ more from Emlid while using three times as many satellites.

| configuration | photos fixed | trajectory fixed | median 3D vs Emlid |
|---|---|---|---|
| `dji_m4e.conf` (L1+L2+L5, GPS+SBAS+Galileo+QZSS+BeiDou, base PCO+PCV, base navigation files) **default** | 1230/1230 | 100 % | 12.2 mm; RTKLIB std 3.5 mm horizontal, 5.6 mm vertical, 19-23 satellites |
| same, DJI NAV only (no GPS, no BeiDou: Galileo on 4-7 satellites, like Emlid) | 1230/1230 | 99.6 % | 5.8 mm (1.5 mm horizontal, 5.7 mm vertical) |
| default + `--set pos1-posopt2=off` (PCO only) | 1230/1230 | 99.5 % | 2.1 mm |
| `dji_m4e_l1l2.conf` (L1+L2) | 1230/1230 | 100 % | 14.1 mm (vertical) |
| RTKLIB-EX `main` @ `06e86442` (2026-08-31), same conf | 1230/1230 | 99.0 % | 5.8 mm (photo positions equal to v2.5.1 within 0.5 mm) |
| `--set pos1-navsys=27` (GPS+Galileo only, the default before 2026-09-21) | 1230/1230 | 100 % | 11.6 mm; photo positions within 2.8 mm median of the BeiDou default |
| GLONASS added, `pos2-gloarmode=autocal` | 0 | 0 % | never fixes: DJI vs Leica inter-channel biases |
| GLONASS added as float only (`pos1-navsys=31`) | 1230/1230 | 100 % | 11.5 mm, but the 2026-09-18 flight drops to 1141/1225 fixed |
| `--set pos1-frequency=l1+l2+l5+l6` (Galileo E6 + BeiDou B3I) | 1230/1230 | 99.3 % | 37 mm shift, std 4.5/7.1 mm; the 2026-09-18 flight gets 0 % fixed |
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
dji-ppk              host launcher: menu / run / status, drives the two containers
Dockerfile           multi-stage build: RTKLIB-EX (rnx2rtkp, convbin, pos2kml), crx2rnx, IGS ANTEX, python package
Dockerfile.estpos    Playwright + Chromium image for ordering Virtual RINEX on the ESTPOS portal (`ppk-estpos` service)
compose.yaml         services `ppk` (one-shot CLI), `ppk-estpos` (portal automation), both profile "cli", and `ppk-watch`
.env.example         template for host directories (FLIGHTS_DIR, BASE_DIR, OUT_DIR), poll interval, RTKLIB_REF
config/dji_m4e.conf  RTKLIB options (three frequencies, GPS+SBAS+Galileo+QZSS+BeiDou, fix-and-hold, combined filter)
config/dji_m4e_l1l2.conf  two-frequency variant
config/dji_m4e_main.conf  variant for RTKLIB-EX `main` (satellite-count semantics differ from v2.5.1)
ppk/                 python package `ppk` (stdlib only) with the CLI, parsers, portal automation, status and tests
examples/            reference comparison report for the flight above
```

Mounted paths inside the containers: `/data/flights` (read-write, results are written next to the photos),
`/data/base` (read-only), `/data/out` (only used with `PPK_IN_PLACE=0`).

## Usage

```sh
cp .env.example .env     # FLIGHTS_DIR = folder that holds the DJI flight folders; ESTPOS_USER / ESTPOS_PASSWORD
./dji-ppk build          # builds the processing image and the portal image
./dji-ppk                # interactive menu
```

`./dji-ppk` is a small launcher on the host that drives the two containers. Without arguments it shows one line
per flight folder and lets you pick what to do:

```
folder                              sess/photos  flown                          base                result                              next
DJI_202609051108_033_site-a         2/1234       2026-09-05 11:34 - 12:04 EEST  virt248i15.rnx.zip  1234 rows in geo.txt, 1234/1234 fixed  done
DJI_202609181702_037_site-b         1/1225       2026-09-18 17:19 - 17:38 EEST  missing             not processed                       order
```

For a folder: **run** (order the Virtual RINEX if no base file covers the sessions yet, then process), order only,
process only, show the order parameters, or a dry run of the order. Non-interactive forms:

```sh
./dji-ppk status                 # the table above (add --json for scripts)
./dji-ppk run <folder>           # order if needed + process -> geo.txt, events.csv, summary.json, accuracy.txt in the folder
./dji-ppk run --all              # every folder whose next step is not "done"
./dji-ppk download <folder>      # fetch an order that already exists on the portal (matched by the flight's span), never orders
./dji-ppk order|process|window|dry-run <folder>
./dji-ppk watch                  # start the folder watcher and follow its log
```

`<folder>` is the folder name under `FLIGHTS_DIR` (or any path to it). The `next` column is `order` when no base
file covers every session, `process` when the base is there, `reprocess` when an input or the base is newer than
the result, `expired` when the flight is older than ESTPOS's 90-day RINEX retention and no base file is in the
folder, and `done` otherwise. Moving to another machine: copy the flight folders there and run `./dji-ppk download`
(or `run`) for each, the orders placed earlier are found on the portal by project name and span and downloaded
without re-ordering (the portal keeps results for 14 days).

<details>
<summary>What the launcher runs</summary>

```sh
docker compose --profile cli run --rm ppk status /data/flights
docker compose --profile cli run --rm ppk-estpos estpos-order /data/flights/<folder>      # Playwright image
docker compose --profile cli run --rm ppk process /data/flights/<folder>                  # RTKLIB image
docker compose --profile cli run --rm ppk estpos-window /data/flights/<folder>            # order parameters only
docker compose --profile cli run --rm ppk check-base /data/flights/<folder>/<zip> --flight /data/flights/<folder>
docker compose --profile cli run --rm ppk compare /data/flights/<folder> "/data/flights/<folder>/<name>_events.pos"
```

Options for `process`: `--conf <file>`, `--set key=value` (repeatable RTKLIB override), `--geo-accuracy`
(adds horizontal/vertical accuracy columns to `geo.txt`), `--fixed-only`, `--keep-work`, `--in-place` / `--no-in-place`,
`--out-dir`, `--name`. `PPK_IN_PLACE=1` (the compose default) makes `--in-place` the default for `process` and `watch`.
Without `--base` the first RINEX file inside the flight folder or under `/data/base` that covers the flight is used.
Ordering by hand instead: https://gnss-rtk.maaamet.ee/sbc, Järeltöötlemine -> RINEX andmed -> tick "Virtuaalne RINEX",
enter the values `window` prints, Esita, then Tulemused -> Lae alla, and copy the zip into the flight folder.
</details>

### Ordering from the ESTPOS portal automatically

`ppk estpos-order <flight>` runs in the separate `ppk-estpos` image (Playwright + Chromium, `Dockerfile.estpos`):

1. logs in to the Spider Business Center with `ESTPOS_USER` / `ESTPOS_PASSWORD` from `.env`,
2. fills the *RINEX andmed* form from the same numbers `estpos-window` prints: start on the quarter hour before
   the flight (Estonian time), length, the mean photo position as the Virtual RINEX point, height, 1 s rate,
   project name = flight folder name,
3. **verifies before submitting**: the portal recomputes data availability after every change with the exact
   parameters it will send; the tool reads that request back and aborts if start, end, latitude, longitude, rate
   or project differ from the order,
4. presses *Esita*, polls *Tulemused -> Virtuaalse RINEX-i andmed* until the entry with that project name is
   ready (usually a few minutes), downloads the zip into the flight folder and validates it with `check-base`.

Nothing is ordered twice: when a base file in the folder already covers every session the command stops
(`--force` overrides), and when the portal already lists an order with the same project name, start and length
it is downloaded instead of re-ordered. The portal truncates project names to 30 characters and asks for a
confirmation (`Kinnita`) after `Esita`; both are handled. Options: `--dry-run` (fill, verify, save `estpos_order_form.png`, do not submit), `--no-wait` and later
`ppk estpos-download <flight> --project <name>`, `--project`, `--rate`, `--timeout`, `--no-send-height`.
The portal keeps results for 14 days and raw data for 90 days. The Virtual RINEX service is free on ESTPOS
accounts, but every run places a real order, so the watcher does not order on its own.

### Watcher

```sh
docker compose up -d ppk-watch
docker compose logs -f ppk-watch
```

Every `PPK_POLL_SECONDS` the watcher scans `/data/flights` for folders containing an `.OBS`/`.NAV`/`.MRK`
triplet. A flight is processed once its files are stable and a base file covering it exists (in the flight
folder or in `/data/base`). Results go into the flight folder (or `/data/out/<flight>/` with `PPK_IN_PLACE=0`);
a `DONE` marker prevents reprocessing and a `FAILED.log` records errors (retried when the input files change).
Drop a new flight folder and its base RINEX under `FLIGHTS_DIR` and the watcher picks it up on the next poll.

## Outputs (flight folder, or `/data/out/<flight>/`)

| file | content |
|---|---|
| `geo.txt` | WebODM/ODM geo file: `EPSG:4326`, then `<image> <lon> <lat> <ellipsoidal height>` per photo (camera position) |
| `events.csv` | per photo: image, GPST, camera lat/lon/h, Q, satellites, std, ratio, antenna lat/lon/h, MRK offsets, DJI RTK position |
| `<obs>_trajectory.pos` | full 5 Hz RTKLIB trajectory (antenna) |
| `<obs>_trajectory_events.pos` | RTKLIB solutions at the exposure times (antenna) |
| `summary.json` | counts, fix ratios, inputs, versions, RTKLIB standard deviations, on-board RTK vs PPK offset, comparison statistics |
| `accuracy.txt` | the one-table summary printed at the end of a run: typical photo position error with the on-board RTK vs after PPK |
| `compare_report.txt` | comparison with the best reference `*_events*.pos` found in the flight folder |
| `rtklib.log`, `rtklib_used.conf` | exact command, messages and options used |

After each run two reports are printed: the solution quality (fix counts, satellites, RTKLIB's estimated standard
deviations, ambiguity ratio) and the drone's on-board RTK positions from the `.MRK` compared with the PPK result.
The mean of that difference is the position error of the on-board RTK base (e.g. a PPP-surveyed D-RTK 3, usually
decimetres); the scatter is dominated by the drone's motion between the RTK epoch and the exposure. The run ends
with a short table (`accuracy.txt`) giving the typical and worst photo position error as flown and RTKLIB's estimate
after PPK, horizontal and vertical, in centimetres.

**One folder is one project.** A folder may hold several flight sessions (battery swaps, a rain break: DJI
writes a new OBS/NAV/MRK triplet and restarts the photo index at 0001 each time). `process <folder>` handles
all of them: photos are assigned to their session by the capture time in the file name, every session is
processed against the base, and the results are merged into one `geo.txt` / `events.csv` / `summary.json` /
`accuracy.txt` for the whole folder, with per-session files prefixed by the session stem
(`<stem>_summary.json`, `<stem>_trajectory.pos`, ...). `estpos-order <folder>` places one Virtual RINEX order
spanning all sessions plus a 5 min buffer; a day longer than `--max-hours` (default 6) is split into several
orders at the gaps between sessions. Pass a single `.OBS` to process one session only.

Camera positions apply the DJI `.MRK` lever arm: camera = antenna + N, + E (mm), height − V. This matches
Emlid Studio (verified to ~1 mm). Image names are the real `DJI_..._NNNN_V.JPG` files matched by the photo index.

## How it works

1. Parse the `.MRK` (photo index, GPS week/seconds with microseconds, lever arm, DJI RTK position).
2. Copy the rover `.OBS` into the work directory inserting one RINEX event record (epoch flag 5) per
   exposure. RTKLIB interpolates its solution to those times and writes `*_events.pos`, exactly as Emlid does.
3. Run `rnx2rtkp -k <conf> -o <trajectory.pos> <rover_events.obs> <base.obs> <rover.nav> <base navs...>`.
   Navigation files that come with the base (the `.26n/.26g/.26l/.26f` members of the ESTPOS zip, or
   siblings of a plain `.26o`) are passed as well.
   The base position comes from the RINEX header and the base antenna (`LEIAR25.R4 LEIT` in ESTPOS
   Virtual RINEX files) is corrected with `igs20.atx`, which is downloaded at image build time.
4. Match the event solutions back to the MRK rows (±1.5 ms), apply the lever arm, write the outputs.

## Notes and pitfalls

- GPS L5, Galileo E5a/E5b and Galileo E1 use different tracking codes on the DJI (I, B) and Leica (Q, C)
  receivers. RTKLIB-EX resolves ambiguities fine anyway; `misc-rnxopt1/2` (e.g. `-EL5Q=-0.25`) exists
  for per-receiver code selection/phase shifts if another base ever needs it.
- The `.trace` files that Emlid Studio leaves in flight folders can be gigabytes; nothing in the flight folder is
  copied except the `.OBS` (with events) into a temporary `work/` directory, which is removed afterwards.
- Emlid Studio `*_events.pos` files in the flight folder are used as comparison references; the tool's own
  `*_trajectory_events.pos` is excluded, so re-running in place does not compare against itself.
- Base files may be `.??o`, RINEX 3 long names (`.rnx`), Hatanaka (`.crx`, `.??d`), `.gz`, `.Z` or `.zip`.
- ESTPOS has no public API; the portal is Leica Spider Business Center and `estpos-order` drives its UI.
  Raw data is available for 90 days, prepared results for 14 days.
- Time zones: the DJI folder name and the ESTPOS order form use Estonian time, so the order text and the log lines
  about time spans lead with Estonian time (`TZ`, default `Europe/Tallinn`). RTKLIB output, `events.csv` and the
  values in parentheses are GPST, which is UTC + 18 s.
- **DJI `.NAV` files carry GPS ephemerides dated 1024 weeks early** (2007 instead of 2026, the GPS week rollover
  bug). RTKLIB silently ignores them, so with the rover NAV alone GPS satellites are not used at all. Always
  download the ESTPOS base **as the zip with its navigation files**: the processor extracts and uses them, and
  logs a warning when the rover NAV is unusable for a constellation.

## Development

```sh
cd ppk && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests      # host
docker compose --profile cli run --rm --entrypoint pytest ppk /app/ppk/tests   # in the image
docker compose --profile cli build --build-arg RTKLIB_REF=main ppk       # another RTKLIB-EX tag or branch
```

The test fixtures are excerpts of real DJI and ESTPOS files. Their coordinates are shifted by a few hundred metres
and the raw observations in `sample.obs` are perturbed, so they exercise the parsers but cannot be used for positioning.

## License

MIT, see `LICENSE`. The Docker image builds and bundles third-party software under its own terms:

- [RTKLIB-EX](https://github.com/rtklibexplorer/RTKLIB) (`rnx2rtkp`, `convbin`, `pos2kml`), BSD-2-Clause, Copyright T. Takasu and rtklibexplorer
- [RNXCMP](https://terras.gsi.go.jp/ja/crx2rnx.html) (`crx2rnx`), Geospatial Information Authority of Japan, redistributable with attribution
- [IGS ANTEX](https://files.igs.org/pub/station/general/) `igs20.atx`, antenna calibration data from the International GNSS Service
