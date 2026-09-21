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

    59.4372403 -> ("592614065", "59° 26' 14.065\\"") with deg_width=2; longitudes use deg_width=3.
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


def _dismiss_cookie_dialog(page) -> None:
    """The portal shows a modal (#dialogAcceptCookiePolicy) that intercepts every click until accepted."""
    dlg = page.locator("#dialogAcceptCookiePolicy")
    try:
        dlg.wait_for(state="visible", timeout=4_000)
    except Exception:  # noqa: BLE001 - no dialog this time
        return
    btn = dlg.locator("button.btn-primary, button:has-text('Accept'), button:has-text('Nõus'), button:has-text('OK')").first
    if btn.count():
        btn.click()
    try:
        dlg.wait_for(state="hidden", timeout=5_000)
    except Exception:  # noqa: BLE001 - remove it by force
        page.evaluate("() => { document.querySelectorAll('#dialogAcceptCookiePolicy, .modal-backdrop').forEach(e => e.remove()); document.body.classList.remove('modal-open'); }")
    log.debug("cookie dialog dismissed")


def login(page, user: str, password: str) -> None:
    page.goto(PORTAL + "/sbc")
    _dismiss_cookie_dialog(page)
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
    """Run `action()` and return the parameters of the availability request it triggers.

    Before the Virtual RINEX panel is open the page queries /rinexavailability/sites (start/end only); after
    that /vrinex/dataAvailability (start/end/lat/lon/rate/name). Both carry the exact values the form holds.
    """
    with page.expect_request(lambda r: "vrinex/dataAvailability" in r.url or "rinexavailability/sites" in r.url,
                             timeout=timeout_ms) as info:
        action()
    return availability_params(info.value.url)


def fill_order_form(page, order: EstposOrder, project: str, rate_s: int = 1, height: float | None = None) -> dict:
    """Fill the RINEX andmed form for a Virtual RINEX order and return the parameters the page echoed back.

    Raises RuntimeError if the page would send something else than `order`.
    """
    page.goto(PORTAL + ORDER_PAGE)
    page.wait_for_selector("#enableVRinex")
    max_len = page.evaluate("() => document.getElementById('projectName').maxLength") or 0
    if max_len > 0 and len(project) > max_len:
        log.warning("project name %r is longer than the portal's %d characters, using %r", project, max_len, project[:max_len])
        project = project[:max_len]

    # 1. start time (Estonian local, 'yyyy-mm-dd h:ii', hour without leading zero)
    start_text = f"{order.start_local:%Y-%m-%d} {order.start_local.hour}:{order.start_local:%M}"

    def set_start():
        # the visible input is read-only; the smalot datetimepicker on #startDate owns it and the page
        # listens to 'change' on that element
        page.evaluate("v => { const $d = window.jQuery('#startDate'); $d.datetimepicker('update', v); $d.trigger('change'); }", start_text)
    try:
        p = _wait_availability(page, set_start)
        log.debug("after start: %s - %s UTC", p["start_utc"], p["end_utc"])
    except Exception:  # noqa: BLE001 - the page may not fire on an unchanged value; verified at the end anyway
        log.debug("no availability request after setting the start time")

    # 2. duration in hours on the bootstrap-slider
    hours = round(order.duration.total_seconds() / 3600 / 0.25) * 0.25
    hours = min(max(hours, 0.25), 24)

    def set_slider():
        # seiyria bootstrap-slider registers as bootstrapSlider because jQuery UI's slider is also loaded
        page.evaluate(
            """h => { const $s = window.jQuery('#timeSlider');
                     if ($s.bootstrapSlider) { $s.bootstrapSlider('setValue', h, true, true); }
                     else { $s.slider('setValue', h, true, true); } }""",
            hours)
    try:
        p = _wait_availability(page, set_slider)
        log.debug("after slider: %s - %s UTC", p["start_utc"], p["end_utc"])
    except Exception:  # noqa: BLE001
        log.debug("no availability request after setting the slider")

    # 3. Virtual RINEX point
    # styled checkbox: the real <input> is hidden, so click it through the DOM
    if not page.evaluate("() => document.getElementById('enableVRinex').checked"):
        page.evaluate("() => document.getElementById('enableVRinex').click()")
    page.wait_for_selector("#vRinexLat", state="visible")
    _, lat_text = dms(order.lat, 2)
    _, lon_text = dms(order.lon, 3)
    # the fields use jquery.inputmask; a formatted value through val() + input/change/blur is accepted
    page.evaluate("""([la, lo]) => { const $ = window.jQuery;
        $('#vRinexLat').val(la).trigger('input').trigger('change').trigger('blur');
        $('#vRinexLog').val(lo).trigger('input').trigger('change').trigger('blur'); }""",
                  [f"{lat_text} N", f"{lon_text} E"])
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
    params["project"] = project
    return params


def submit_order(page) -> None:
    """Click 'Esita', confirm the summary dialog ('Kinnita') and wait for the portal to accept the request."""
    btn = page.get_by_role("button", name=re.compile(r"^(Esita|Submit)$"))
    btn.first.click()
    confirm = page.get_by_role("button", name=re.compile(r"^(Kinnita|Confirm)$"))
    confirm.first.wait_for(state="visible", timeout=15_000)
    summary = page.locator(".modal:visible, [role=dialog]:visible").first.inner_text()
    log.info("portal summary before confirming:\n%s", re.sub(r"\d{2} ° \d{2} ' [\d.]+ \" [NE]", "<dms>", summary).strip())
    with page.expect_response(lambda r: "/Xpos/API" in r.url and r.request.method == "POST", timeout=60_000) as info:
        confirm.first.click()
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
    paths = order_and_download_many([(order, project)], dest_dir, user, password, rate_s=rate_s, height=height,
                                    wait=wait, timeout_min=timeout_min, headed=headed, dry_run=dry_run, screenshot=screenshot)
    return paths[0] if paths else None


def order_and_download_many(orders: list[tuple[EstposOrder, str]], dest_dir: Path, user: str, password: str, *,
                            rate_s: int = 1, height: float | None = None, wait: bool = True, timeout_min: float = 60,
                            headed: bool = False, dry_run: bool = False, screenshot: Path | None = None) -> list[Path]:
    """Place one or more orders (e.g. a long flight day split at the portal's length limit) in one browser
    session, then wait for and download each of them. Returns the downloaded files (empty for dry runs)."""
    pw, browser, context = _browser(headed)
    page = None
    try:
        page = context.new_page()
        login(page, user, password)
        since = datetime.now().replace(microsecond=0) - timedelta(minutes=2)
        placed: list[str] = []
        for i, (order, project) in enumerate(orders, 1):
            if len(orders) > 1:
                log.info("--- order %d/%d ---", i, len(orders))
            project = fill_order_form(page, order, project, rate_s, height)["project"]
            if screenshot:
                shot = screenshot if len(orders) == 1 else screenshot.with_name(f"{screenshot.stem}_{i}{screenshot.suffix}")
                page.screenshot(path=str(shot), full_page=True)
                log.info("form screenshot saved to %s", shot)
            if dry_run:
                log.info("dry run: not submitting")
                continue
            submit_order(page)
            placed.append(project)
        if dry_run or not wait:
            for project in placed:
                log.info("order placed; fetch it later with: ppk estpos-download <flight> --project %r", project)
            return []
        paths = []
        for project in placed:
            entry = wait_for_result(page, project, since, timeout_min)
            paths.append(download_entry(page, entry, dest_dir))
        return paths
    except Exception:
        _error_screenshot(page, dest_dir)
        raise
    finally:
        context.close()
        browser.close()
        pw.stop()


def _error_screenshot(page, dest_dir: Path) -> None:
    if page is None:
        return
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / "estpos_error.png"
        page.screenshot(path=str(target), full_page=True)
        log.error("screenshot of the failing page saved to %s (url %s)", target, page.url)
    except Exception:  # noqa: BLE001
        pass


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
