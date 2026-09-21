"""Order and download ESTPOS Virtual RINEX through the Spider Business Center web portal with Playwright.

The portal (https://gnss-rtk.maaamet.ee/sbc) has no public API: the pages talk to an internal Xpos API
with a token the page fetches itself. So this module drives the real UI in headless Chromium:

  login -> RINEX andmed page -> fill start / duration / Virtual RINEX point -> verify what the page is
  about to send (it calls GET /Xpos/API/vrinex/dataAvailability with the exact parameters after every
  change) -> "Esita" -> Tulemused page, tab "Virtuaalse RINEX-i andmed" -> "Lae alla" -> "Jah" -> zip.

Playwright is an optional dependency (pip install playwright && playwright install chromium); the
`ppk-estpos` compose service provides it. Nothing here logs credentials.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .estpos import EstposOrder

log = logging.getLogger("ppk.estpos")

PORTAL = "https://gnss-rtk.maaamet.ee"
ORDER_PAGE = "/sbc/User/Xpos/RinexDataRequest"
RESULTS_PAGE = "/sbc/User/Xpos/Results"
LANG_ET = "/sbc/Home/SetLanguage?lang=et"


# ----------------------------------------------------------------------------- pure helpers (unit tested)

def dms(value: float, deg_width: int) -> tuple[str, str]:
    """(digits for the input mask, human readable) for a positive angle.

    58.4014069 -> ("582405065", "58° 24' 05.065\\"") with deg_width=2; longitudes use deg_width=3.
    """
    total_ms = round(abs(value) * 3600 * 1000)  # milliarcseconds
    deg, rem = divmod(total_ms, 3600 * 1000)
    minutes, rem = divmod(rem, 60 * 1000)
    sec = rem / 1000
    digits = f"{deg:0{deg_width}d}{minutes:02d}{int(sec):02d}{round((sec - int(sec)) * 1000):03d}"
    text = f"{deg:0{deg_width}d}° {minutes:02d}' {sec:06.3f}\""
    return digits, text


def availability_params(url: str) -> dict:
    """Parameters of a /Xpos/API/vrinex/dataAvailability request as python values."""
    q = parse_qs(urlparse(url).query)

    def one(k):
        return q.get(k, [None])[0]

    def iso(s):
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None) if s else None

    return {"start_utc": iso(one("startTime")), "end_utc": iso(one("endTime")),
            "lat": float(one("latitude")) if one("latitude") else None,
            "lon": float(one("longitude")) if one("longitude") else None,
            "rate_ms": int(one("observationRate")) if one("observationRate") else None,
            "name": one("name") or ""}


def order_mismatch(params: dict, order: EstposOrder, rate_s: int, project: str, tol_deg: float = 2e-6) -> list[str]:
    """Human readable list of differences between what the page will send and what we want; empty = OK."""
    bad = []
    if params["start_utc"] != order.start_utc:
        bad.append(f"start {params['start_utc']} UTC on the page, wanted {order.start_utc} UTC")
    if params["end_utc"] != order.end_utc:
        bad.append(f"end {params['end_utc']} UTC on the page, wanted {order.end_utc} UTC")
    if params["lat"] is None or abs(params["lat"] - order.lat) > tol_deg:
        bad.append(f"latitude {params['lat']} on the page, wanted {order.lat:.7f}")
    if params["lon"] is None or abs(params["lon"] - order.lon) > tol_deg:
        bad.append(f"longitude {params['lon']} on the page, wanted {order.lon:.7f}")
    if params["rate_ms"] is not None and params["rate_ms"] != rate_s * 1000:
        bad.append(f"observation rate {params['rate_ms']} ms on the page, wanted {rate_s * 1000} ms")
    if project and params["name"] != project:
        bad.append(f"project name {params['name']!r} on the page, wanted {project!r}")
    return bad


@dataclass
class ResultEntry:
    requested: datetime | None
    project: str
    start_local: datetime | None
    duration_h: float | None
    ready: bool
    button_id: str
    text: str


_RE_REQUESTED = re.compile(r"Taotletud\s+(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
_RE_PROJECT = re.compile(r"Projekt:\s*(.*?)\s*(?:\n|Soovitatud|$)")
_RE_START = re.compile(r"Soovitatud algusaeg:\s*(\d{4}-\d\d-\d\d \d\d:\d\d(?::\d\d)?)")
_RE_DURATION = re.compile(r"Kestvus:\s*(\d\d):(\d\d)\s*h")


def parse_result_entry(text: str, button_id: str, ready: bool) -> ResultEntry:
    """Parse one Virtual RINEX result card (Estonian UI) into a ResultEntry."""
    t = text.replace("\r", "")
    m = _RE_REQUESTED.search(t)
    requested = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") if m else None
    m = _RE_PROJECT.search(t)
    project = m.group(1).strip() if m else ""
    if project.lower() in ("tühi", "empty"):
        project = ""
    m = _RE_START.search(t)
    start = None
    if m:
        s = m.group(1)
        start = datetime.strptime(s, "%Y-%m-%d %H:%M:%S" if s.count(":") == 2 else "%Y-%m-%d %H:%M")
    m = _RE_DURATION.search(t)
    duration = int(m.group(1)) + int(m.group(2)) / 60 if m else None
    return ResultEntry(requested, project, start, duration, ready, button_id, t)


# ----------------------------------------------------------------------------- browser driving

def _browser(headed: bool):
    from playwright.sync_api import sync_playwright  # lazy: optional dependency

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=not headed)
    context = browser.new_context(accept_downloads=True, locale="et-EE", timezone_id="Europe/Tallinn")
    context.set_default_timeout(30_000)
    return pw, browser, context


def login(page, user: str, password: str) -> None:
    page.goto(PORTAL + "/sbc")
    for label in ("Accept", "Nõustun", "Nõustu"):
        btn = page.get_by_role("button", name=label)
        if btn.count():
            btn.first.click()
            break
    if "/Account/" in page.url:
        form = page.locator("form").filter(has=page.locator("input[type=password]")).first
        form.locator("input[type=text]").first.fill(user)
        form.locator("input[type=password]").fill(password)
        form.locator("button[type=submit], input[type=submit]").first.click()
        page.wait_for_load_state("networkidle")
    if "/Account/" in page.url:
        raise RuntimeError("ESTPOS login failed (still on the login page). Check ESTPOS_USER / ESTPOS_PASSWORD.")
    page.goto(PORTAL + LANG_ET)  # deterministic labels for the parsers below
    page.wait_for_load_state("networkidle")
    log.info("logged in to ESTPOS as %s***", user[:2])


def _wait_availability(page, action, timeout_ms: int = 20_000) -> dict:
    """Run `action()` and return the parameters of the dataAvailability request it triggers."""
    with page.expect_request(lambda r: "vrinex/dataAvailability" in r.url, timeout=timeout_ms) as info:
        action()
    return availability_params(info.value.url)


def fill_order_form(page, order: EstposOrder, project: str, rate_s: int = 1, height: float | None = None) -> dict:
    """Fill the RINEX andmed form for a Virtual RINEX order and return the parameters the page echoed back.

    Raises RuntimeError if the page would send something else than `order`.
    """
    page.goto(PORTAL + ORDER_PAGE)
    page.wait_for_selector("#enableVRinex")

    # 1. start time (Estonian local, 'yyyy-mm-dd h:ii', hour without leading zero)
    start_text = f"{order.start_local:%Y-%m-%d} {order.start_local.hour}:{order.start_local:%M}"
    visible = page.locator("input.startDateInput")

    def set_start():
        visible.click()
        visible.press("Control+a")
        visible.fill(start_text)
        visible.press("Enter")
        page.keyboard.press("Escape")
        page.evaluate("v => { const h = document.getElementById('startDateInput'); if (h && h.value !== v) { h.value = v; h.dispatchEvent(new Event('change', {bubbles: true})); } }", start_text)
        page.locator("#projectName").click()
    try:
        _wait_availability(page, set_start)
    except Exception:  # noqa: BLE001 - the page may not fire on an unchanged value; verified at the end anyway
        log.debug("no availability request after setting the start time")

    # 2. duration in hours on the bootstrap-slider
    hours = round(order.duration.total_seconds() / 3600 / 0.25) * 0.25
    hours = min(max(hours, 0.25), 24)

    def set_slider():
        page.evaluate(
            """h => { const $s = window.jQuery('#timeSlider'); $s.slider('setValue', h, true, true);
                     $s.trigger('slideStop', h); $s.trigger('change', {oldValue: 0, newValue: h}); }""",
            hours)
    try:
        _wait_availability(page, set_slider)
    except Exception:  # noqa: BLE001
        log.debug("no availability request after setting the slider")

    # 3. Virtual RINEX point
    cb = page.locator("#enableVRinex")
    if not cb.is_checked():
        cb.check()
        page.wait_for_selector("#vRinexLat")
    lat_digits, lat_text = dms(order.lat, 2)
    lon_digits, lon_text = dms(order.lon, 3)
    for sel, digits in (("#vRinexLat", lat_digits), ("#vRinexLog", lon_digits)):
        el = page.locator(sel)
        el.click()
        el.press("Control+a")
        el.press("Backspace")
        el.type(digits, delay=20)
        el.press("Tab")
    if height is not None:
        page.locator("#vRinexHeight").fill(f"{height:.2f}")
    page.locator("#vRinexMarkerName").fill("Virtual RINEX")
    page.locator("#vRinexMarkerNumber").fill("VRNX")
    page.locator("#observationRate").select_option(str(rate_s))

    def set_project():
        page.locator("#projectName").fill(project)
        page.locator("#projectName").press("Tab")
    try:
        params = _wait_availability(page, set_project)
    except Exception:  # noqa: BLE001
        # force one more availability query by re-selecting the rate
        params = _wait_availability(page, lambda: page.locator("#observationRate").select_option(str(rate_s)))

    log.info("portal echo: %s - %s UTC, lat %.7f lon %.7f, rate %s ms, project %r",
             params["start_utc"], params["end_utc"], params["lat"] or 0, params["lon"] or 0, params["rate_ms"], params["name"])
    problems = order_mismatch(params, order, rate_s, project)
    if problems:
        raise RuntimeError("the portal form does not match the order, not submitting:\n  - " + "\n  - ".join(problems))
    log.info("form verified: %s local, %.2f h, %s %s", start_text, hours, lat_text, lon_text)
    return params


def submit_order(page) -> None:
    """Click 'Esita' and wait for the portal to accept the request."""
    btn = page.get_by_role("button", name=re.compile(r"^(Esita|Submit)$"))
    with page.expect_response(lambda r: "/Xpos/API" in r.url and r.request.method == "POST", timeout=60_000) as info:
        btn.first.click()
    resp = info.value
    if resp.status >= 400:
        raise RuntimeError(f"the portal rejected the order: HTTP {resp.status} {resp.url}\n{resp.text()[:500]}")
    log.info("order submitted (HTTP %d)", resp.status)
    page.wait_for_timeout(1500)


def list_results(page) -> list[ResultEntry]:
    page.goto(PORTAL + RESULTS_PAGE)
    page.wait_for_load_state("networkidle")
    tab = page.locator('a[href="#VRinexResults"]')
    if tab.count():
        tab.first.click()
    page.wait_for_timeout(1500)
    raw = page.evaluate(
        """() => {
            const pane = document.getElementById('VRinexResults') || document.body;
            const btns = [...pane.querySelectorAll('button[id^=vrinexFileDownloadButton], button[id^=fileDownloadButton]')];
            return btns.map(b => {
              let card = b;
              for (let i = 0; i < 8 && card.parentElement; i++) { card = card.parentElement; if (/Taotletud/.test(card.innerText || '')) break; }
              const ready = !b.disabled && !!card.querySelector('.fa-check, .icon-check, .glyphicon-ok, [class*=check]');
              return {id: b.id, ready, disabled: b.disabled, status: b.getAttribute('data-status'), text: card.innerText};
            });
        }""")
    entries = []
    for r in raw:
        ready = (not r["disabled"]) and (r["ready"] or r.get("status") in ("0", "2", "done", "Ready"))
        entries.append(parse_result_entry(r["text"], r["id"], ready))
    return entries


def find_entry(entries: list[ResultEntry], project: str, since: datetime | None) -> ResultEntry | None:
    cands = [e for e in entries if e.project == project and (since is None or (e.requested and e.requested >= since))]
    cands.sort(key=lambda e: e.requested or datetime.min, reverse=True)
    return cands[0] if cands else None


def wait_for_result(page, project: str, since: datetime | None, timeout_min: float, poll_s: int = 30) -> ResultEntry:
    deadline = time.time() + timeout_min * 60
    while True:
        entry = find_entry(list_results(page), project, since)
        if entry and entry.ready:
            return entry
        if time.time() > deadline:
            raise TimeoutError(f"order {project!r} not ready after {timeout_min:.0f} min"
                               + (" (not listed at all)" if entry is None else ""))
        log.info("%s: %s, checking again in %d s", project, "listed, still processing" if entry else "not listed yet", poll_s)
        page.wait_for_timeout(poll_s * 1000)


def download_entry(page, entry: ResultEntry, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    btn = page.locator(f"#{entry.button_id}")
    btn.scroll_into_view_if_needed()
    with page.expect_download(timeout=180_000) as info:
        btn.click()
        confirm = page.locator("#confirmDownload")
        try:
            confirm.wait_for(state="visible", timeout=5_000)
            confirm.click()
        except Exception:  # noqa: BLE001 - no confirm dialog
            pass
    dl = info.value
    name = dl.suggested_filename or f"{entry.project}.zip"
    target = dest_dir / name
    dl.save_as(target)
    log.info("downloaded %s (%.1f MB)", target, target.stat().st_size / 1e6)
    return target


# ----------------------------------------------------------------------------- high level

def order_and_download(order: EstposOrder, project: str, dest_dir: Path, user: str, password: str, *,
                       rate_s: int = 1, height: float | None = None, wait: bool = True, timeout_min: float = 60,
                       headed: bool = False, dry_run: bool = False, screenshot: Path | None = None) -> Path | None:
    pw, browser, context = _browser(headed)
    try:
        page = context.new_page()
        login(page, user, password)
        since = datetime.now().replace(microsecond=0) - timedelta(minutes=2)
        fill_order_form(page, order, project, rate_s, height)
        if screenshot:
            page.screenshot(path=str(screenshot), full_page=True)
            log.info("form screenshot saved to %s", screenshot)
        if dry_run:
            log.info("dry run: not submitting")
            return None
        submit_order(page)
        if not wait:
            log.info("order placed; fetch it later with: ppk estpos-download <flight> --project %r", project)
            return None
        entry = wait_for_result(page, project, since, timeout_min)
        return download_entry(page, entry, dest_dir)
    finally:
        context.close()
        browser.close()
        pw.stop()


def download_existing(project: str, dest_dir: Path, user: str, password: str, *, since: datetime | None = None,
                      timeout_min: float = 0, headed: bool = False) -> Path:
    pw, browser, context = _browser(headed)
    try:
        page = context.new_page()
        login(page, user, password)
        entry = wait_for_result(page, project, since, timeout_min) if timeout_min > 0 else find_entry(list_results(page), project, since)
        if entry is None:
            raise FileNotFoundError(f"no Virtual RINEX result with project name {project!r} on the portal")
        if not entry.ready:
            raise RuntimeError(f"result {project!r} is still processing; try again later or use --timeout")
        return download_entry(page, entry, dest_dir)
    finally:
        context.close()
        browser.close()
        pw.stop()
