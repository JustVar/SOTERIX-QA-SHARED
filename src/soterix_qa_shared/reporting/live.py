"""The live record of a run in progress, and the estimate of what is left.

The archive is written when a run ends, so a viewer during the run sees
the previous one and nothing says a run is happening. The archive plugin
writes `live.json` beside the index at session start and refreshes it as
tests finish; the index and health pages fetch it and show a banner; the
plugin removes it once the run is archived.

The estimate comes from the archive's own history: every summary.json
carries per-test durations, so the remaining tests' median durations over
the last runs of the same target, divided by the worker count, is the time
left. Before that history exists it falls back to the median total duration
of those runs less the time elapsed; with no history at all it says so. A
heartbeat older than STALE_AFTER_S reads as stalled: a scheduled run that
hangs gives no other signal, and a week of hung runs once passed unseen.

Pure functions take their inputs; the plugin does the I/O through
write_live / clear_live.
"""
from __future__ import annotations

import json
import os
import statistics
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

LIVE_FILE = 'live.json'
HISTORY_RUNS = 5          # runs of the same target the estimate reads
STALE_AFTER_S = 15 * 60   # no heartbeat for this long reads as stalled
MIN_WRITE_GAP_S = 3.0     # the plugin refreshes the file at most this often


def live_paths(runs_dir: Path) -> list[Path]:
    """Where the record is written: beside the archive index. A list, so a
    consumer that mirrors the archive elsewhere can add a path."""
    return [Path(runs_dir) / LIVE_FILE]


def write_live(paths: Iterable[Path], record: Mapping[str, Any]) -> None:
    """Write the record atomically to every path; a path that cannot be
    written is skipped (the record is a courtesy, never a verdict)."""
    text = json.dumps(dict(record), sort_keys=True)
    for path in paths:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix('.json.tmp')
            tmp.write_text(text, encoding='utf-8')
            os.replace(tmp, path)
        except OSError:
            continue


def clear_live(paths: Iterable[Path]) -> None:
    for path in paths:
        try:
            Path(path).unlink()
        except OSError:
            continue


# ---------------------------------------------------------------- history

def _run_matches(summary: Mapping[str, Any], target: list[str]) -> bool:
    """Same target list, in order. An older summary may carry a `box` label
    in its provenance; it is not part of the match."""
    return list(summary.get('target') or []) == list(target)


def history(runs_dir: Path, target: list[str], last: int = HISTORY_RUNS) -> list[dict]:
    """The newest `last` archived summaries of this target, newest first.
    Runs the cap or a kill ended have no summary and do not count, which
    keeps a week of hung runs out of the estimate."""
    found = []
    runs_dir = Path(runs_dir)
    if not runs_dir.is_dir():
        return found
    for folder in sorted(runs_dir.iterdir(), reverse=True):
        summary_path = folder / 'summary.json'
        if not folder.is_dir() or not summary_path.exists():
            continue
        try:
            summary = json.loads(summary_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if _run_matches(summary, target):
            found.append(summary)
            if len(found) >= last:
                break
    return found


def median_durations(summaries: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    """{nodeid: median duration_s} across the summaries' test records."""
    seen: dict[str, list[float]] = {}
    for summary in summaries:
        for test in summary.get('tests') or []:
            duration = test.get('duration_s')
            if isinstance(duration, (int, float)) and test.get('nodeid'):
                seen.setdefault(test['nodeid'], []).append(float(duration))
    return {nodeid: statistics.median(values) for nodeid, values in seen.items()}


def estimate_remaining_s(remaining: Iterable[str], durations: Mapping[str, float],
                         workers: int, summaries: Iterable[Mapping[str, Any]] = (),
                         elapsed_s: float = 0.0) -> tuple[float | None, str]:
    """(seconds left, basis). Per-test history first; a test the history
    never saw costs the median of the tests it did see. Without per-test
    history, the median total of the runs less the time elapsed. Without
    runs, None."""
    remaining = list(remaining)
    workers = max(int(workers or 1), 1)
    if durations:
        known = [durations[n] for n in remaining if n in durations]
        typical = statistics.median(durations.values())
        total = sum(known) + typical * (len(remaining) - len(known))
        runs = len(list(summaries)) if summaries else 0
        basis = (f'per-test durations from {runs} earlier run{"s" if runs != 1 else ""} '
                 f'of this target' if runs else 'per-test durations from earlier runs')
        return total / workers, basis
    totals = [s.get('duration_s') for s in summaries if isinstance(s.get('duration_s'), (int, float))]
    if totals:
        left = max(float(statistics.median(totals)) - elapsed_s, 0.0)
        return left, f'the median length of {len(totals)} earlier run{"s" if len(totals) != 1 else ""} of this target'
    return None, 'no earlier run of this target, so no estimate'


# ---------------------------------------------------------------- the record

def build_record(*, started: float, now: float, target: list[str], env: str,
                 product: str, workers: int, collected: int, counts: Mapping[str, int],
                 current: str | None, remaining: Iterable[str], runs_dir: Path,
                 hostname: str = '') -> dict[str, Any]:
    """Everything the banner shows, computed from the session's facts."""
    summaries = history(runs_dir, target)
    durations = median_durations(summaries)
    elapsed = max(now - started, 0.0)
    if int(collected) > 0:
        eta_s, basis = estimate_remaining_s(remaining, durations, workers, summaries, elapsed)
    else:
        eta_s, basis = None, 'still collecting the tests'
    done = sum(int(v) for v in counts.values())
    return {
        'state': 'running',
        'started': _iso(started), 'updated': _iso(now),
        'target': list(target), 'env': env, 'product': product,
        'host': hostname, 'workers': workers,
        'collected': int(collected), 'done': done,
        'counts': {k: int(counts.get(k, 0)) for k in ('passed', 'failed', 'skipped', 'errors')},
        'current': current,
        'elapsed_s': round(elapsed, 1),
        'eta_s': round(eta_s, 1) if eta_s is not None else None,
        'eta_basis': basis,
        'stale_after_s': STALE_AFTER_S,
    }


def _iso(ts: float) -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(ts))


# ---------------------------------------------------------------- the banner

def banner_html() -> str:
    """The banner slot and the script that fills it from live.json.

    Inline on the index and health pages. Fetches live.json beside the page
    every 30 s with the cache off; a 404 hides the banner, a heartbeat older
    than stale_after_s says stalled instead of pretending. No library, no
    build step: the pages stay static files any web server can serve.
    """
    return '''<div id="live" class="live" hidden></div>
<style>
.live{margin:12px 0 18px;padding:12px 16px;border-radius:8px;border:1px solid #d9a400;background:#fff6d6;color:#3b2f00;font-size:14px;line-height:1.5}
.live.stalled{border-color:#c0392b;background:#fbe3e0;color:#4a1410}
.live b{font-weight:600}.live .bar{height:6px;background:#eadca0;border-radius:3px;margin-top:8px;overflow:hidden}
.live .bar i{display:block;height:100%;background:#b8860b}.live.stalled .bar i{background:#c0392b}
</style>
<script>
(function(){
  var el=document.getElementById('live');
  function mins(s){s=Math.round(s);if(s<90)return s+' s';var m=Math.round(s/60);if(m<90)return m+' min';return (m/60).toFixed(1)+' h';}
  function show(r){
    var now=Date.now(),upd=Date.parse(r.updated),stale=(now-upd)/1000>(r.stale_after_s||900);
    var c=r.counts||{},pct=r.collected?Math.round(100*r.done/r.collected):0;
    var head=stale?'Run stalled: no progress since '+r.updated.replace('T',' '):'Run in progress';
    var eta=r.eta_s==null?'no estimate ('+r.eta_basis+')':'about '+mins(r.eta_s)+' left ('+r.eta_basis+')';
    el.className='live'+(stale?' stalled':'');
    el.innerHTML='<b>'+head+'</b>: '+(r.target||[]).join(' ')+(r.product?', '+r.product:'')+
      ', started '+r.started.replace('T',' ')+(r.host?' on '+r.host:'')+', '+r.workers+' worker'+(r.workers==1?'':'s')+'.<br>'+
      (r.collected?r.done+' of '+r.collected+' done ('+(c.passed||0)+' passed, '+(c.failed||0)+' failed, '+(c.skipped||0)+' skipped'+((c.errors||0)?', '+c.errors+' errors':'')+')':'collecting the tests')+', '+
      'elapsed '+mins(r.elapsed_s)+', '+eta+'.'+(r.current?'<br>Now: <code>'+r.current+'</code>':'')+
      '<div class="bar"><i style="width:'+pct+'%"></i></div>';
    el.hidden=false;
  }
  function poll(){
    fetch('live.json',{cache:'no-store'}).then(function(res){
      if(!res.ok){el.hidden=true;return null;}return res.json();
    }).then(function(r){if(r&&r.state==='running')show(r);else el.hidden=true;}).catch(function(){el.hidden=true;});
  }
  poll();setInterval(poll,30000);
})();
</script>'''
