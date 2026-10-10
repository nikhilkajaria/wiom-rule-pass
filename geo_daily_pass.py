# -*- coding: utf-8 -*-
"""Daily non-Delhi BoF monitor for BHARAT_ALL_SPL and BHARAT_LUCKNOW_SPL - DM only, read-only, advisory. Never writes to Meta.

Built 2026-10-11 (Nikhil: "put Lucknow and Bharat also like Mumbai ... change all non-delhi ones to 7-day reads instead of
14-day reads"). Same shape as mumbai_daily_pass.py, without Mumbai's plan-specific tripwires (T1-T5 were dated to the Mumbai
Plan D launch). Replaces bharat_weekly_pass.py (Monday, #demand-reports) - Bharat now reads daily into Nikhil's DM like Mumbai.

Every run, per geo (one ad set each - see GEOS):
  D-1        Meta spend, CPM, impressions, installs, link clicks
  7d         spend, reach, frequency, CPM; dashboard installs / checks / pass rate (install cohort, still maturing) and
             bookings / connections (booking-date keyed, master_export - the same split mumbai_daily_pass.py uses)
  geo verdict  7d CPBL vs the median of the previous 8 seven-day blocks of the SAME ad set (Bharat's own-history
             benchmark from bharat_weekly_pass.py, 2026-09-05: non-Delhi CPBL is structurally ~3x Delhi's, so comparing
             to Delhi's C* says nothing): <= -15% SCALE, >= +15% WATCH, else HOLD
  flags      ad set not ACTIVE; Meta learning FAIL (learning limited); budget moved > 15% inside 3 days (daily
             snapshots in geo_pass_state.json); 7d frequency > 5; no bookings in 7d with spend
  creatives  trailing-7d advisory ranking by CPBC against the ad set's own median, live Meta active-status vetted,
             booking-date keyed - not a kill list (same design as Bharat's / Mumbai's ranking)
Same 3-attempt dashboard-readiness gate as the other passes; own idempotency state per geo ('<geo>_daily').

Usage: python geo_daily_pass.py --geo bharat|lucknow [--dry-run] [--date YYYY-MM-DD] [--last-retry]
"""
import argparse
import collections
import datetime
import json
import os
import statistics
import urllib.parse
import urllib.request

import rule_pass as rp

GEOS = {
    'bharat': dict(adset_id='120248674329450343', adset_name='BHARAT_ALL_SPL_L1-L2-L3_BROAD_MULTI_APPSTORE_ABO_BFC-VOLUME_MULTI_NA',
                   label='BHARAT_ALL_SPL', live=datetime.date(2026, 6, 6), note=''),
    'lucknow': dict(adset_id='120251575719170343', adset_name='BHARAT_LUCKNOW_SPL_L1-L2-L3_BROAD_BOOKNOW_APPSTORE_ABO_BFC-VOLUME_MULTI_NA',
                    label='BHARAT_LUCKNOW_SPL', live=datetime.date(2026, 8, 21),
                    note='Targeting changed 6-Oct (16 exclusion circles, budget 4,300 -> 5,000; registry D0360/D0361): '
                         'blocks before that are a different setup.'),
}
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'geo_pass_state.json')
WINDOW_DAYS = 7          # Nikhil 2026-10-11: non-Delhi reads are 7-day, not 14-day
BENCH_BLOCKS = 8         # previous 7-day blocks for the own-history median
SCALE_BAND = 0.15
FREQ_CAP = 5.0


def f(v):
    try: return float(v)
    except (TypeError, ValueError): return 0.0


def actions(row, key):
    return sum(f(a.get('value')) for a in row.get('actions', []) if a.get('action_type') == key)


def meta_get(path, params):
    ver = os.environ.get('META_API_VERSION', rp.META_VER_DEFAULT)
    url = f'https://graph.facebook.com/{ver}/{path}?' + urllib.parse.urlencode(params)
    with urllib.request.urlopen(rp._meta_req(url), timeout=120) as r:
        return json.loads(r.read().decode())


def active_concepts(adset_name):
    """Live ACTIVE concept ids in this ad set (paused creatives excluded from the ranking). None if Meta is unreachable."""
    if not os.environ.get('META_ACCESS_TOKEN'): return None
    acc = os.environ.get('META_AD_ACCOUNT_ID', rp.META_ACC_DEFAULT)
    if not str(acc).startswith('act_'): acc = 'act_' + str(acc)
    ver = os.environ.get('META_API_VERSION', rp.META_VER_DEFAULT)
    url = f'https://graph.facebook.com/{ver}/{acc}/ads?' + urllib.parse.urlencode({'fields': 'id,name,effective_status,adset{name}', 'limit': 500})
    active, calls = set(), 0
    try:
        while url and calls < 25:
            with urllib.request.urlopen(rp._meta_req(url), timeout=90) as r:
                j = json.loads(r.read().decode())
            if 'error' in j:
                print('warn: Meta active-filter unavailable ->', j['error'].get('message')); return None
            for a in j.get('data', []):
                if a.get('effective_status') != 'ACTIVE' or (a.get('adset') or {}).get('name') != adset_name: continue
                m = rp.CONCEPT_RE.search(a.get('name', '') or '')
                if m: active.add(m.group(0))
            calls += 1
            url = (j.get('paging') or {}).get('next')
        return active
    except Exception as e:
        print('warn: Meta active-filter fetch failed ->', e); return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--geo', required=True, choices=sorted(GEOS))
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--date', help='D-1 anchor YYYY-MM-DD (default yesterday IST)')
    ap.add_argument('--last-retry', action='store_true',
                    help='final scheduled attempt of the day - alert (DM) if dashboard data is still not ready')
    args = ap.parse_args()
    g = GEOS[args.geo]; label = g['label']; ready_key = f'{args.geo}_daily'
    rp.load_env()
    now_ist = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)
    d1 = datetime.date.fromisoformat(args.date) if args.date else (now_ist - datetime.timedelta(days=1)).date()

    if not args.dry_run and not args.date:
        from dashboard_readiness import is_dashboard_data_ready, already_completed_today
        if already_completed_today(ready_key, d1):
            print(f'already completed for {d1} - skipping (idempotent retry guard)'); return
        ready, dash_total, actual_total = is_dashboard_data_ready(d1)
        if not ready:
            dash_s = f"Rs{dash_total:,.0f}" if dash_total is not None else 'n/a'
            act_s = f"Rs{actual_total:,.0f}" if actual_total is not None else 'n/a'
            if args.last_retry:
                rp.slack_post(f":rotating_light: *{label} daily* - dashboard data for {d1} still incomplete after 3 attempts "
                              f"(dashboard spend {dash_s} vs actual Meta+Google spend {act_s}). Pass did NOT run today - check the dashboard ETL.",
                              dm_only=True)
                print(f'last retry - data still not ready for {d1} - alerted, giving up for today')
            else:
                print(f'dashboard data not ready for {d1} (dashboard={dash_s}, actual={act_s}) - postponing to next retry')
            return

    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    snaps = state.setdefault(args.geo, {})
    w0 = d1 - datetime.timedelta(days=WINDOW_DAYS - 1)

    # ---- Meta: ad set state, D-1, trailing 7d
    aset = meta_get(g['adset_id'], {'fields': 'name,effective_status,daily_budget,learning_stage_info'})
    lsi = aset.get('learning_stage_info') or {}
    budget = int(aset.get('daily_budget') or 0) / 100
    dd = (meta_get(f"{g['adset_id']}/insights", {'time_range': json.dumps({'since': d1.isoformat(), 'until': d1.isoformat()}),
                                                  'fields': 'spend,impressions,cpm,inline_link_clicks,actions'}).get('data') or [{}])[0]
    wk = (meta_get(f"{g['adset_id']}/insights", {'time_range': json.dumps({'since': w0.isoformat(), 'until': d1.isoformat()}),
                                                  'fields': 'spend,impressions,reach,frequency,cpm,actions'}).get('data') or [{}])[0]
    snaps[d1.isoformat()] = {'budget': budget, 'learning': lsi.get('status')}
    budget_3d = snaps.get((d1 - datetime.timedelta(days=3)).isoformat(), {}).get('budget')

    # ---- dashboard: install-cohort funnel (7d) and booking-date outcomes (7d + benchmark blocks)
    fun = {'installs': 0.0, 'checks': 0.0, 'passed': 0.0}
    try:
        rows = rp.dget('/api/funnel_rows?' + urllib.parse.urlencode({'start': w0.isoformat(), 'end': d1.isoformat()}))
        for r in rows if isinstance(rows, list) else []:
            if r.get('ad_set') != g['adset_name']: continue
            fun['installs'] += f(r.get('app_installs')); fun['checks'] += f(r.get('serviceable_check')); fun['passed'] += f(r.get('serviceable_true'))
    except Exception as e:
        print('warn: dashboard funnel unavailable ->', e)
    bench_start = d1 - datetime.timedelta(days=WINDOW_DAYS * (BENCH_BLOCKS + 1) - 1)
    mrows = []
    try:
        mrows = [r for r in rp.dget('/api/master_export?' + urllib.parse.urlencode({'start': bench_start.isoformat(), 'end': d1.isoformat()}))
                 if r.get('ad_set') == g['adset_name'] and r.get('channel') == 'META']
    except Exception as e:
        print('warn: master_export unavailable ->', e)
    blocks = collections.defaultdict(lambda: {'spend': 0.0, 'bc': 0.0, 'conn': 0.0})
    for r in mrows:
        k = (d1 - datetime.date.fromisoformat(str(r.get('date'))[:10])).days // WINDOW_DAYS
        if 0 <= k <= BENCH_BLOCKS:
            b = blocks[k]; b['spend'] += f(r.get('spend')); b['bc'] += f(r.get('booking_confirmed')); b['conn'] += f(r.get('connection_installed'))
    cur = blocks[0]
    cpbl = cur['spend'] / cur['bc'] if cur['bc'] else None
    # only blocks that start on or after the ad set's live date (a part-live block would understate spend)
    hist = [blocks[k]['spend'] / blocks[k]['bc'] for k in range(1, BENCH_BLOCKS + 1)
            if blocks[k]['bc'] > 0 and d1 - datetime.timedelta(days=WINDOW_DAYS * k + WINDOW_DAYS - 1) >= g['live']]
    bench = statistics.median(hist) if hist else None
    if cpbl is None or bench is None:
        verdict, vline = 'INSUFFICIENT DATA', f"7d bookings {cur['bc']:.0f} - not enough to compare"
    else:
        delta = (cpbl - bench) / bench
        verdict = 'SCALE' if delta <= -SCALE_BAND else ('WATCH' if delta >= SCALE_BAND else 'HOLD')
        vline = f"7d CPBL Rs{cpbl:,.0f} vs own median of the previous {len(hist)} seven-day blocks Rs{bench:,.0f} ({delta * 100:+.0f}%)"

    # ---- flags
    freq7 = f(wk.get('frequency')); sp7 = f(wk.get('spend'))
    rs_reach = sp7 / f(wk.get('reach')) if f(wk.get('reach')) else None
    pass_rate = fun['passed'] / fun['checks'] if fun['checks'] >= 30 else None
    flags = []
    if aset.get('effective_status') != 'ACTIVE':
        flags.append(f":red_circle: ad set is {aset.get('effective_status')} - nothing below is live")
    if lsi.get('status') == 'FAIL':
        flags.append(':warning: Meta learning status FAIL (learning limited) - fewer than ~50 optimisation events a week')
    if budget_3d and abs(budget - budget_3d) / budget_3d > 0.15:
        flags.append(f":warning: budget moved {budget_3d:,.0f} -> {budget:,.0f} inside 3 days (> 15%). Learning likely reset")
    if freq7 > FREQ_CAP:
        flags.append(f":warning: 7d frequency {freq7:.1f} > {FREQ_CAP:.0f} - ground saturated, no more budget on this targeting")
    if sp7 > 0 and cur['bc'] == 0:
        flags.append(f":red_circle: no bookings in 7d on Rs {sp7:,.0f} spend")
    if verdict == 'WATCH':
        flags.append(f":warning: geo WATCH - 7d CPBL is 15%+ worse than this ad set's own recent history")
    if not flags:
        flags.append(':white_check_mark: no flag hit')

    # ---- creative ranking (trailing 7d, booking-date, own median, live-active only)
    active = active_concepts(g['adset_name'])
    by_cid = collections.defaultdict(lambda: {'spend': 0.0, 'bc': 0.0})
    for r in mrows:
        if str(r.get('date'))[:10] < w0.isoformat(): continue
        m = rp.CONCEPT_RE.search(r.get('creative', '') or '')
        if not m or (active is not None and m.group(0) not in active): continue
        by_cid[m.group(0)]['spend'] += f(r.get('spend')); by_cid[m.group(0)]['bc'] += f(r.get('booking_confirmed'))
    with_bc = {c: v for c, v in by_cid.items() if v['bc'] > 0}
    zero_bc = sorted(((c, v) for c, v in by_cid.items() if v['bc'] == 0 and v['spend'] > 0), key=lambda kv: -kv[1]['spend'])
    cpbcs = {c: v['spend'] / v['bc'] for c, v in with_bc.items()}
    med = statistics.median(cpbcs.values()) if cpbcs else None
    cl = ['', f"*Creative ranking, trailing {WINDOW_DAYS}d ({w0.isoformat()} to {d1.isoformat()}), booking-date basis - advisory, no kill recommendation*"]
    if active is None:
        cl.append('_Integrity: live Meta active-status check unavailable this run - list may include paused creatives._')
    if cpbcs:
        cl.append(f"{len(with_bc)}/{len(by_cid)} active creatives have a booking to rank by CPBC (pool median Rs{med:,.0f})")
        for c, x in sorted(cpbcs.items(), key=lambda kv: kv[1]):
            v = with_bc[c]
            tag = ':large_green_circle:' if x <= med * 0.85 else (':red_circle:' if x >= med * 1.3 else ':white_circle:')
            cl.append(f"   {tag} `{c}` {v['bc']:.0f} BC, Rs{v['spend']:,.0f}, CPBC Rs{x:,.0f}")
    else:
        cl.append('No creative has a booking in the window yet.')
    if zero_bc:
        cl.append('Zero bookings, real spend - ranked by spend (no CPBC to judge by):')
        for c, v in zero_bc: cl.append(f"   :black_circle: `{c}` Rs{v['spend']:,.0f}, 0 BC")

    text = (f"*{label} daily* - {d1:%a %d %b}, budget Rs {budget:,.0f}/day, learning {lsi.get('status') or 'n/a'}\n"
            + (f"_{g['note']}_\n" if g['note'] else '')
            + f"D-1: spend Rs {f(dd.get('spend')):,.0f}, CPM Rs {f(dd.get('cpm')):.0f}, impr {f(dd.get('impressions')):,.0f}, "
              f"installs (Meta) {actions(dd, 'mobile_app_install'):.0f}, link clicks {f(dd.get('inline_link_clicks')):.0f}\n"
            + f"Trailing 7d: spend Rs {sp7:,.0f}, reach {f(wk.get('reach')):,.0f}, freq {freq7:.2f}, CPM Rs {f(wk.get('cpm')):.0f}, "
              f"Rs/reached {('%.3f' % rs_reach) if rs_reach else 'n/a'} | installs {fun['installs']:.0f}, checks {fun['checks']:.0f}, "
              f"pass {('%.0f%%' % (100 * pass_rate)) if pass_rate is not None else 'n/a (<30 checks)'} (install cohort, maturing) | "
              f"bookings {cur['bc']:.0f}, connections {cur['conn']:.0f}, CPBL {('Rs %s' % format(round(cpbl), ',')) if cpbl else 'n/a'}\n"
            + f"Geo: {verdict} - {vline}\n"
            + "\n".join(flags) + "\n".join(cl))
    print(text)
    if args.dry_run: return
    rp.slack_post(text, dm_only=True)
    keep = sorted(snaps)[-60:]
    state[args.geo] = {k: snaps[k] for k in keep}
    json.dump(state, open(STATE, 'w'), indent=1)
    if not args.date:
        from dashboard_readiness import mark_completed_today
        mark_completed_today(ready_key, d1)


if __name__ == '__main__':
    main()
