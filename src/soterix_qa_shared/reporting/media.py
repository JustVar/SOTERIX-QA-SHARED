"""Screenshot reporter -- pytest plugin.

Writes one labeled screenshot per UI test to disk and embeds failures into
the HTML report (reports/report.html), so a run can be reviewed straight
from the report or from the folder - no rerun needed.

What gets captured:
  - Every test that reaches its body: one screenshot as the test finishes,
    labeled PASSED or FAILED. A PASSED shot first waits up to 5 s for the
    app's loading spinners (.MuiCircularProgress-root) to leave: a Views
    test passes the moment its tile is consumed, while the stream still
    loads, and the green shot would otherwise show the loading circle
    instead of the painted tile. Best-effort - a spinner that stays is
    filed as-is. Failures skip the wait: their spinner is evidence.
  - A test whose setup fails with its page already reachable (parked on
    self.page or requested directly by the test): labeled SETUP FAILED.
  - Drag-and-drop tests: the mid-drag frames ViewsPage.mouse_drag stashed
    while the pointer was down - the only frames that show the dragover
    styling and the drag tests' source/target outlines. Filed beside the
    final screenshot, at most 8 per test.
  - Skipped tests: no final screenshot - but a call-phase skip that has
    mid-drag frames stashed still files them to disk as
    <test>__SKIPPED__mid-drag-<n>.png, never embedded. The drag tests
    skip precisely when a drag fails, so those frames are the most
    diagnostic evidence a skipped drag leaves behind.

What gets embedded in the report:
  - Failures (FAILED / SETUP FAILED) under the test's own row.
  - PASSED screenshots stay on disk only, unless REPORT_SCREENSHOTS=all -
    the self-contained report inlines every embedded image, and a full
    run's worth of green shots makes it too big to open or email.
  - Mid-drag frames follow the final screenshot's rule: embedded when it
    embeds, disk-only otherwise.

Where images land:
  reports/screenshots/<test>__PASSED.png (or __FAILED.png / __SETUP_FAILED.png).
  Mid-drag frames sit beside them as <test>__<suffix>__mid-drag-<n>.png.
  The folder only ever holds the LATEST run - it is emptied at session start.

Failed tests also get a link in the report to the Playwright trace zip
(reports/artifacts/<test>-trace.zip; open it with `playwright show-trace`).

Kill switch: set REPORT_SCREENSHOTS=off to disable the plugin entirely
(no capture, no attach, no folder cleanup). Stashed mid-drag frames are
still discarded so they cannot sit on a long-lived page. Default is on -
nothing to set up.

Screenshots are reporting, not testing: every error in this plugin is logged
and swallowed, so a capture problem can never change a test's outcome.

A screenshot call that never returns (a dead Playwright driver whose
pipes Chrome kept open) is capped by a watchdog: REPORT_MEDIA_CAP_S,
45 s by default, 0 disables. Past it an xdist worker exits so the
controller reschedules the rest; a plain session is only told. A driver
already known dead is not asked at all. See the note above _Watchdog.
"""
from __future__ import annotations

import base64
import logging
import os
import re
import sys
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Self

import pytest

from soterix_qa_shared.reporting.paths import fit_name

try:
    from pytest_html import extras
except ImportError:
    # Without pytest-html the disk copies still get written; only the
    # report attachments are skipped.
    extras = None

log = logging.getLogger('report_media')

SCREENSHOT_DIR = Path('reports') / 'screenshots'

# Trace zips are written by conftest's authenticated_page fixture; the
# constants here must stay in lockstep with conftest._trace_path so the
# report link points at the file the fixture actually writes.
TRACE_DIR = Path('reports') / 'artifacts'


def configure(root):
    """Point the capture at THIS repo's evidence dirs, from a repo root.

    The pytest plugin calls this from pytest_sessionstart with
    config.rootpath. The module-level defaults above are CWD-relative, so a
    plugin that never configured still worked when pytest ran from the root;
    this makes the root explicit and correct wherever pytest runs from.
    """
    from soterix_qa_shared.reporting.layout import paths_for
    global SCREENSHOT_DIR, TRACE_DIR
    p = paths_for(root)
    SCREENSHOT_DIR = p['screenshots']
    TRACE_DIR = p['artifacts']
TRACE_NAME_MAX = 150

# Shorter cap than the trace path's 150: the screenshot name also carries a
# __SETUP_FAILED suffix (plus __mid-drag-<n> on in-flight frames). The stem
# cap is the first step. fit_name then keeps the absolute path within the
# Windows cap on a deep checkout, where the stem cap alone is not enough.
SAFE_NAME_MAX = 120

# At most this many in-flight drag frames get filed per test: a drag-heavy
# test could stash dozens, and every embedded frame is inlined into the
# self-contained report. Overflow is logged and dropped.
DRAG_CAPTURE_MAX = 8

# The app's loading spinner (MUI CircularProgress) - the selector the page
# objects wait on - and how long a PASSED screenshot waits for it to leave.
LOADER_SELECTOR = '.MuiCircularProgress-root'
SETTLE_MS = 5000

_OFF_VALUES = ('off', '0', 'false', 'no')
_EMBED_ALL_VALUES = ('all',)

_PHASE_OUTCOME_SUFFIX = {
    ('call', 'passed'): 'PASSED',
    ('call', 'failed'): 'FAILED',
    ('setup', 'failed'): 'SETUP_FAILED',
}


def _screenshots_disabled(environ: Mapping[str, str]) -> bool:
    """Kill switch: REPORT_SCREENSHOTS=off turns the whole plugin off."""
    return environ.get('REPORT_SCREENSHOTS', '').strip().lower() in _OFF_VALUES


def _should_embed(suffix: str, environ: Mapping[str, str]) -> bool:
    """Decide whether this screenshot also goes into the HTML report row.

    Failures always embed - they are what the report gets opened for. PASSED
    embeds only under REPORT_SCREENSHOTS=all: the self-contained report
    inlines every attached image as base64, so a full run's ~900 green
    viewport shots would balloon reports/report.html into something too big
    to open - and sink the SEND_TEST_REPORT email, which carries the report
    twice (inlined body + attachment). The disk copy is written either way.
    """
    if suffix != 'PASSED':
        return True
    return environ.get('REPORT_SCREENSHOTS', '').strip().lower() in _EMBED_ALL_VALUES


def _safe_name(nodeid: str) -> str:
    """Build a filesystem-safe screenshot stem from a test node id.

    Same sanitizer as conftest._trace_path so a test's screenshot and trace
    sort together; only the cap differs (see SAFE_NAME_MAX).
    """
    return re.sub(r'[^\w.-]+', '-', nodeid).strip('-')[:SAFE_NAME_MAX]


def _capture_filename(nodeid: str, suffix: str, label: str, index: int) -> str:
    """Filename for an in-flight frame, e.g. <safe>__FAILED__mid-drag-1.png.

    Same stem and suffix as the test's final screenshot so the frames sort
    directly next to it; the 1-based index keeps a multi-drag test's frames
    in the order the drags happened. (A skipped test files no final shot;
    its frames carry the SKIPPED suffix on their own.) Fitted to the path
    cap like the final shot, so a deep checkout never loses a frame.
    """
    return fit_name(SCREENSHOT_DIR,
                    f'{_safe_name(nodeid)}__{suffix}__{_safe_name(label)}-{index}', '.png')


def _final_filename(nodeid: str, suffix: str) -> str:
    """Filename for the test's final screenshot, e.g. <safe>__FAILED.png.

    The stem cap is the first step. fit_name then keeps the absolute path
    within the Windows cap: past it the write fails and the shot is lost.
    """
    return fit_name(SCREENSHOT_DIR, f'{_safe_name(nodeid)}__{suffix}', '.png')


def _drain_drag_captures(page: Any, cap: int = DRAG_CAPTURE_MAX) -> list:
    """Take the in-flight (label, png-bytes) frames off the page's stash.

    ViewsPage.mouse_drag appends to page._drag_captures while the pointer
    is down. The stash is cleared in place on every drain - even when
    nothing gets filed - because the page outlives the test (session-scoped
    authenticated_page), and leftover bytes would be filed under the next
    test that shares it. Returns at most `cap` frames, oldest first;
    overflow is logged and dropped.
    """
    if page is None:
        return []
    captures = getattr(page, '_drag_captures', None)
    if not captures:
        return []
    taken = list(captures[:cap])
    if len(captures) > cap:
        log.warning(f'report_media: dropped {len(captures) - cap} mid-drag '
                    f'frame(s) beyond the {cap}-per-test cap')
    try:
        captures.clear()
    except Exception as e:  # noqa: BLE001 - an unclearable stash must not cost the frames
        log.debug(f'report_media: could not clear the drag stash: {e}')
    return taken


# ---------------------------------------------------------------- the watchdog
#
# On a Linux runner, two xdist workers once spun at 100% CPU for two hours
# inside page.screenshot() in this plugin's makereport hook. Their
# Playwright driver (node) had died, but the Chrome children still held its
# pipes open, so Python never saw EOF and the sync API's wait loop never
# returned. pytest-timeout could not help: its signal lands inside the
# asyncio loop step, which logs the exception and carries on. The whole
# scheduled run then waits for the run cap. Two defences, both cheap:
#   1. do not ask a dead driver for a screenshot (_driver_alive);
#   2. a wall-clock watchdog on the call; when it expires in an xdist
#      worker, the worker exits and the controller marks the test failed
#      and reschedules the rest. In a plain session it only says so: the
#      human at the keyboard decides, and an exit would lose the archive.
HANG_CAP_ENV = 'REPORT_MEDIA_CAP_S'
DEFAULT_HANG_CAP_S = 45.0
_exit = os._exit   # a module-level name so the pins can replace it


def _hang_cap(environ: Mapping[str, str]) -> float:
    """Seconds a screenshot may take before the watchdog acts; 0 disables."""
    try:
        return max(float(environ.get(HANG_CAP_ENV) or DEFAULT_HANG_CAP_S), 0.0)
    except ValueError:
        return DEFAULT_HANG_CAP_S


def _driver_alive(page: Any) -> bool | None:
    """Is the Playwright driver process behind `page` still running?

    True/False when it can be told, None when it cannot (another
    transport, no /proc on this platform, a private attribute renamed).
    Reads Playwright internals on purpose: the public API has no such
    question, and asking a dead driver is what hung a whole run.
    """
    try:
        proc = page._impl_obj._connection._transport._proc
        pid = int(proc.pid)
    except Exception:  # noqa: BLE001 - why: unknown is an answer; the watchdog still guards the call
        return None
    if getattr(proc, 'returncode', None) is not None:
        return False
    try:
        with open(f'/proc/{pid}/status', encoding='utf-8') as f:
            for line in f:
                if line.startswith('State:'):
                    # a driver whose Chrome children kept its pipes open is a zombie here
                    return 'Z' not in line.split()[1]
    except OSError:
        return None
    return None


class _Watchdog:
    """A wall-clock cap on one screenshot call (see the note above)."""

    def __init__(self, nodeid: str, cap_s: float, environ: Mapping[str, str]) -> None:
        self.nodeid, self.cap_s = nodeid, cap_s
        self.in_worker = bool(environ.get('PYTEST_XDIST_WORKER'))
        self._timer: threading.Timer | None = None
        self.expired = False

    def __enter__(self) -> Self:
        if self.cap_s > 0:
            self._timer = threading.Timer(self.cap_s, self._expire)
            self._timer.daemon = True
            self._timer.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._timer is not None:
            self._timer.cancel()

    def _expire(self) -> None:
        self.expired = True
        msg = (f'report_media: the screenshot for {self.nodeid} did not return in {self.cap_s:g} s; '
               f'the Playwright driver is gone or stuck'
               + (' - ending this worker so the run goes on' if self.in_worker else
                  ' - a plain session is left to the person running it'))
        log.error(msg)
        print(msg, file=sys.stderr, flush=True)
        if self.in_worker:
            _exit(3)


def _settle_loaders(page: Any, timeout_ms: int = SETTLE_MS) -> bool:
    """Best-effort wait for every loading spinner to leave before a PASSED shot.

    A Views test passes on the consumed tile while its stream still loads;
    without this the green shot shows the loading circle, not the painted
    tile. Returns True when the spinners left within the bound, False for
    everything else (no page, timeout, any error) - the shot is then taken
    as-is. Evidence only, never a verdict.
    """
    if page is None:
        return False
    try:
        page.wait_for_function(
            f'() => document.querySelectorAll({LOADER_SELECTOR!r}).length === 0',
            timeout=timeout_ms,
        )
        return True
    except Exception as e:  # noqa: BLE001 - evidence only; a spinner that stays is filed as-is
        log.debug(f'report_media: spinners still up after {timeout_ms} ms: {e}')
        return False


def _suffix_for(when: str, outcome: str) -> str | None:
    """Map a report phase + outcome to a screenshot suffix, or None for no image.

    Skips and teardown reports return None - no final screenshot for
    either: a skipped test never reached a verdict worth a labeled shot,
    and by teardown the fixtures are already gone. (_attach_media still
    files a call-phase skip's stashed mid-drag frames; see there.)
    """
    return _PHASE_OUTCOME_SUFFIX.get((when, outcome))


def _expected_trace_path(nodeid: str) -> Path:
    """Predict where conftest._trace_path will save this test's failure trace.

    The zip is written at fixture teardown, after this report is built, so
    the link has to point at where the file WILL land - same sanitizer, same
    150-char cap, same directory, same path fit.
    """
    safe = re.sub(r'[^\w.-]+', '-', nodeid).strip('-')[:TRACE_NAME_MAX]
    return TRACE_DIR / fit_name(TRACE_DIR, f'{safe}-trace', '.zip')


def _should_clean(config: Any, environ: Mapping[str, str]) -> bool:
    """Decide whether this process may empty the screenshot folder.

    - --collect-only never cleans: run_suite's planning pass collects before
      the real run, and wiping here would delete the last real run's images
      (the same wipe class that once took out the HTML report).
    - xdist workers never clean: each worker starts its own session, and
      concurrent deletes would race the controller and each other.
    """
    collect_only = config.getoption('--collect-only', default=False)
    worker = environ.get('PYTEST_XDIST_WORKER')
    return not collect_only and not worker


def _clean_screenshot_dir(directory: Path) -> int:
    """Delete the *.png files sitting directly in `directory`; return the count.

    Deliberately narrow - no recursion, nothing but .png - so a misconfigured
    path can never eat anything beyond stale screenshots.
    """
    if not directory.is_dir():
        return 0
    removed = 0
    for png in directory.glob('*.png'):
        if not png.is_file():
            continue
        try:
            png.unlink()
            removed += 1
        except OSError as e:
            log.warning(f'report_media: could not delete {png}: {e}')
    return removed


def _resolve_page(item: Any):
    """Find a live Playwright page on the test item, or None.

    Class-based tests' `self.page` first (same order as qatouch_reporter):
    the suite's autouse setup fixtures - admin, c2c, and the rest - all park
    their page there, and after a setup crash it is the only handle left,
    because pytest fills item.funcargs one fixture at a time and stops at
    the failure, so a page pulled in as a dependency never reaches funcargs.
    Function-style tests fall back to the `authenticated_page`/`page`
    fixtures they request directly. None when no candidate is a live page
    (pure-Python tests, no page ever opened, or already closed).
    """
    instance = getattr(item, 'instance', None)
    funcargs = getattr(item, 'funcargs', None) or {}
    candidates = (getattr(instance, 'page', None),
                  funcargs.get('authenticated_page'),
                  funcargs.get('page'))
    for page in candidates:
        if page is None:
            continue
        try:
            if not page.is_closed():
                return page
        except Exception:  # noqa: BLE001, S112 - a page we cannot even query is not usable, skip it
            continue
    return None


def _attach_media(item: Any, report: Any) -> None:
    """Save the labeled screenshots to disk and attach them to the report row."""
    suffix = _suffix_for(report.when, report.outcome)
    page = _resolve_page(item)
    if suffix is None:
        # No image for this phase, but the drag stash still empties: the
        # page outlives the test, and stale frames left sitting on it
        # would be filed under the next test that shares the page.
        drag_frames = _drain_drag_captures(page)
        # A call-phase skip is how the drag tests bail on a failed drag -
        # exactly when the stashed mid-drag frame is the most diagnostic
        # evidence. File those frames to disk only: no final shot and no
        # report embed, so the skipped-tests contract above still holds
        # for the report and the labeled screenshot. Setup skips drain
        # without filing - their frames predate this test.
        if report.when == 'call' and report.outcome == 'skipped' and drag_frames:
            try:
                SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
                for index, (frame_label, frame_png) in enumerate(drag_frames, start=1):
                    name = _capture_filename(item.nodeid, 'SKIPPED', frame_label, index)
                    (SCREENSHOT_DIR / name).write_bytes(frame_png)
            except Exception as e:  # noqa: BLE001 - drag forensics only; a disk problem must not break the skip
                log.debug(f'report_media: could not file skipped-drag frames for {item.nodeid}: {e}')
        return
    label = suffix.replace('_', ' ')

    try:
        if page is not None:
            # Drain before any write so a disk problem below can never
            # leave this test's frames on the stash for the next test.
            drag_frames = _drain_drag_captures(page)
            embed = extras is not None and _should_embed(suffix, os.environ)
            SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
            # Mid-drag frames first: mouse_drag stashed them while the
            # pointer was down, when the dragover styling and the drag
            # tests' source/target outlines were on screen - state the
            # final screenshot below can never show.
            for index, (frame_label, frame_png) in enumerate(drag_frames, start=1):
                name = _capture_filename(item.nodeid, suffix, frame_label, index)
                (SCREENSHOT_DIR / name).write_bytes(frame_png)
                if embed:
                    report.extras = [*getattr(report, 'extras', []),
                                     extras.png(base64.b64encode(frame_png).decode('ascii'),
                                                name=f'{item.name} - {frame_label} {index}')]
            # A green Views test is still loading its stream when the
            # verdict lands; give the spinner a bounded moment to leave so
            # the shot shows the painted tile. Failures keep theirs.
            if _driver_alive(page) is False:
                log.warning(f'report_media: no screenshot for {item.nodeid}: the Playwright driver is gone')
            else:
                with _Watchdog(item.nodeid, _hang_cap(os.environ), os.environ):
                    if suffix == 'PASSED':
                        _settle_loaders(page)
                    # Viewport-only (not full_page) keeps payload size reasonable -
                    # the self-contained report inlines every image.
                    png = page.screenshot()
                (SCREENSHOT_DIR / _final_filename(item.nodeid, suffix)).write_bytes(png)
                if embed:
                    report.extras = [*getattr(report, 'extras', []),
                                     extras.png(base64.b64encode(png).decode('ascii'),
                                                name=f'{item.name} - {label}')]
    except Exception as e:  # noqa: BLE001 - a lost screenshot must not cost the trace link too
        log.debug(f'report_media: screenshot failed for {item.nodeid}: {e}')

    if (suffix == 'FAILED' and extras is not None
            and 'authenticated_page' in (getattr(item, 'funcargs', None) or {})):
        # authenticated_page saves its trace at fixture teardown (conftest
        # _finish_tracing), after this report exists - so link to where the
        # zip will land and the report row leads straight to the trace.
        trace_uri = _expected_trace_path(item.nodeid).resolve().as_uri()
        report.extras = [*getattr(report, 'extras', []),
                         extras.url(trace_uri, name=f'{item.name} - failure trace')]


def pytest_sessionstart(session: Any) -> None:
    """Empty reports/screenshots/ so the folder only holds the latest run."""
    configure(session.config.rootpath)
    if _screenshots_disabled(os.environ):
        return
    if not _should_clean(session.config, os.environ):
        return
    try:
        removed = _clean_screenshot_dir(SCREENSHOT_DIR)
        if removed:
            log.info(f'report_media: cleared {removed} screenshot(s) from the previous run')
    except Exception as e:  # noqa: BLE001 - cleanup is best-effort; never block the session
        log.warning(f'report_media: could not clear {SCREENSHOT_DIR}: {e}')


def _process_report(item: Any, report: Any, environ: Mapping[str, str]) -> None:
    """Route one phase report: attach its media, or honor the kill switch.

    The kill switch skips every write and attachment but never the drag-
    stash cleanup - mouse_drag stashes frames regardless of this plugin's
    setting, and stale bytes must not sit on a page that outlives the test.
    """
    if _screenshots_disabled(environ):
        _drain_drag_captures(_resolve_page(item))
        return
    _attach_media(item, report)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: Any, call: Any):
    """Attach the phase's screenshots while the test's fixtures are still alive.

    Runs alongside conftest's rep_<when> stashing wrapper; this one only
    decorates the report and never influences the outcome.
    """
    outcome = yield
    report = outcome.get_result()
    try:
        _process_report(item, report, os.environ)
    except Exception as e:  # noqa: BLE001 - reporting must never break testing
        log.debug(f'report_media: could not attach media for {item.nodeid}: {e}')
