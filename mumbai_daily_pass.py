# -*- coding: utf-8 -*-
"""Daily MUMBAI_CSP99_SPL monitor - DM only, read-only, advisory. Never writes to Meta.

Built 2026-09-12 when the Mumbai Plan D ad set went live (ad set 120251928228180343, Rs 13k/day,
serviceable_page_loaded event, 72 circles, 99 CSPs). Reads D-1 from Meta Insights AND the
growth-dashboard install-cohort funnel (checks, pass rate, bookings, connections - the T3/T5
tripwires below depend on these). Originally shipped with only a Meta-side timing assumption
("Meta is complete by 10:30 IST") and no gate on the dashboard side - Nikhil caught this
2026-09-12: the dashboard's own ETL is the same erratic-timing risk documented in
dashboard_readiness.py (D-1 data lands clustered around 13:00-13:30 and 15:00-15:30 IST), so a
T3 pass-rate or T5 CPI/connections read taken at 10:30 IST could easily be judging partial data
without any warning. Now uses the SAME 3-attempt retry ladder as bharat_weekly_pass.py -
dashboard_readiness.is_dashboard_data_ready() gates the run, 13:30/15:30/17:30 IST, own
idempotency state (mumbai_daily_readiness_state.json) so a later attempt that day is a no-op
once a real run has completed for D-1.

What it evaluates every day, in the order the plan's tripwires are dated (see
Downloads/Ajinkya - Mumbai Restart Files - 11 Sep/1 plan/Mumbai ad set - final plan.md):
  learning     Meta learning_stage_info; optimisation events per trailing 7 days from daily snapshots
               kept in mumbai_pass_state.json (Insights cannot isolate one custom app event: every
               custom event lands in app_custom_event.other, so learning-stage conversions are the
               clean SPL count while the ad set is in learning, and the dashboard funnel after).
  T1 D3+       CPM > Rs 75 on each of the last 3 days       -> creative/placement check, no budget move
  T2 D10+      SPL < 50 per trailing 7 days                 -> pause and re-plan the event
  T3 D14+      pass rate (dashboard, cohorts >= 50 checks) < 70%
                                                            -> switch geo to planD_minus_supplyfail_69
  T4 weekly    Rs per reached person (7d) > 0.25, or frequency (7d) > 5
                                                            -> ground saturated, stop adding budget
  T5 D30       CPI (Meta spend / dashboard installs) vs Rs 110; connections vs 35
  budget guard live daily_budget vs value 3 days ago in state -> flag moves > 15%
Zone-level reads (SPL per 1k HH in the 29 supply-fail zones; bookings by CSP supply row) are not
automatable from the APIs here - they run in-session on day 7 / 14 / 30 (mumbai_zone_read.py).

Usage: python mumbai_daily_pass.py [--dry-run] [--date YYYY-MM-DD]
"""
import argparse
import datetime
import json
import os
import urllib.parse
import urllib.request

import rule_pass as rp

ADSET_ID = '120251928228180343'
ADSET_NAME = 'MUMBAI_CSP99_SPL_L1-L2-L3_BROAD_MULTI_APPSTORE_ABO_BFC-VOLUME_MULTI_NA'
LIVE_DATE = datetime.date(2026, 9, 12)
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mumbai_pass_state.json')
CPM_TRIP, SPL_WEEK_FLOOR, PASS_FLOOR, RS_PER_REACH, FREQ_CAP, CPI_TARGET, CONN_TARGET = 75.0, 50, 0.70, 0.25, 5.0, 110.0, 35
MODEL = dict(cpm='50-55', installs_mo=3700, spl_mo=730, bookings_mo=186, conns_mo=43)


def meta_get(path, params):
    tok = os.environ['META_ACCESS_TOKEN']
    ver = os.environ.get('META_API_VERSION', rp.META_VER_DEFAULT)
    p = dict(params); p['access_token'] = tok
    url = f'https://graph.facebook.com/{ver}/{path}?' + urllib.parse.urlencode(p)
    with urllib.request.urlopen(url, timeout=120) as r:
        return json.loads(r.read().decode())


def f(v):
    try: return float(v)
    except (TypeError, ValueError): return 0.0


def actions(row, key):
    return sum(f(a.get('value')) for a in row.get('actions', []) if a.get('action_type') == key)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--date', help='D-1 anchor YYYY-MM-DD (default yesterday IST)')
    ap.add_argument('--last-retry', action='store_true',
                     help='final scheduled attempt of the day - alert (DM) if dashboard data is still '
                          'not ready, instead of quietly postponing to the next retry')
    args = ap.parse_args()
    rp.load_env()
    now_ist = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)
    d1 = datetime.date.fromisoformat(args.date) if args.date else (now_ist - datetime.timedelta(days=1)).date()
    day_n = (d1 - LIVE_DATE).days + 1

    # Same dashboard-readiness gate + idempotency pattern as bharat_weekly_pass.py - own
    # script_name ('mumbai_daily') so state never collides with any other pass.
    if not args.dry_run and not args.date:
        from dashboard_readiness import is_dashboard_data_ready, already_completed_today, mark_completed_today
        if already_completed_today('mumbai_daily', d1):
            print(f'already completed for {d1} - skipping (idempotent retry guard)')
            return
        ready, dash_total, actual_total = is_dashboard_data_ready(d1)
        if not ready:
            dash_s = f"Rs{dash_total:,.0f}" if dash_total is not None else 'n/a'
            act_s = f"Rs{actual_total:,.0f}" if actual_total is not None else 'n/a'
            if args.last_retry:
                rp.slack_post(
                    f":rotating_light: *MUMBAI_CSP99_SPL daily* - dashboard data for {d1} still "
                    f"incomplete after 3 attempts (dashboard spend {dash_s} vs actual Meta+Google spend {act_s}). "
                    f"Pass did NOT run today - check the dashboard ETL.",
                    dm_only=True)
                print(f'last retry - data still not ready for {d1} (dashboard={dash_s}, actual={act_s}) - alerted, giving up for today')
            else:
                print(f'dashboard data not ready for {d1} (dashboard={dash_s}, actual={act_s}) - postponing to next retry')
            return

    state = json.load(open(STATE)) if os.path.exists(STATE) else {'snapshots': {}}

    # ---- Meta: ad set state + daily rows since live + 7d reach
    aset = meta_get(ADSET_ID, {'fields': 'name,effective_status,daily_budget,learning_stage_info'})
    lsi = aset.get('learning_stage_info') or {}
    budget = int(aset.get('daily_budget') or 0) / 100
    days = meta_get(f'{ADSET_ID}/insights', {'time_range': json.dumps({'since': LIVE_DATE.isoformat(), 'until': d1.isoformat()}), 'time_increment': 1,
                                            'fields': 'date_start,spend,impressions,reach,frequency,cpm,inline_link_clicks,actions', 'limit': 100}).get('data', [])
    days.sort(key=lambda r: r['date_start'])
    w_since = max(LIVE_DATE, d1 - datetime.timedelta(days=6))
    wk = (meta_get(f'{ADSET_ID}/insights', {'time_range': json.dumps({'since': w_since.isoformat(), 'until': d1.isoformat()}), 'fields': 'spend,impressions,reach,frequency,cpm,actions'}).get('data') or [{}])[0]
    ads = meta_get(f'{ADSET_ID}/insights', {'level': 'ad', 'time_range': json.dumps({'since': w_since.isoformat(), 'until': d1.isoformat()}), 'fields': 'ad_name,spend,impressions,cpm,actions', 'limit': 50}).get('data', [])

    # snapshot learning-stage conversions for the trailing-7d SPL count
    snap = state['snapshots']
    snap[d1.isoformat()] = {'conversions': int(lsi.get('conversions') or 0), 'budget': budget, 'status': lsi.get('status')}
    conv_now = snap[d1.isoformat()]['conversions']
    prev_key = (d1 - datetime.timedelta(days=7)).isoformat()
    spl_7d = conv_now - snap[prev_key]['conversions'] if prev_key in snap else None
    b3_key = (d1 - datetime.timedelta(days=3)).isoformat()
    budget_3d = snap.get(b3_key, {}).get('budget')

    # ---- dashboard install-cohort funnel for this ad set (maturing)
    fun = {'installs': 0, 'checks': 0, 'passed': 0, 'bfc': 0, 'conn': 0, 'rows': 0}
    try:
        rows = rp.dget('/api/funnel_rows?' + urllib.parse.urlencode({'start': LIVE_DATE.isoformat(), 'end': d1.isoformat()}))
        for r in rows if isinstance(rows, list) else []:
            if r.get('ad_set') != ADSET_NAME: continue
            fun['rows'] += 1; fun['installs'] += f(r.get('app_installs')); fun['checks'] += f(r.get('serviceable_check')); fun['passed'] += f(r.get('serviceable_true'))
            fun['bfc'] += f(r.get('bfc')); fun['conn'] += f(r.get('connection_installed'))
    except Exception as e:
        print('warn: dashboard funnel unavailable ->', e)

    # ---- aggregates
    spend_tot = sum(f(r['spend']) for r in days); impr_tot = sum(f(r['impressions']) for r in days)
    inst_meta = sum(actions(r, 'mobile_app_install') for r in days)
    last3 = days[-3:]
    cpm_trip = day_n >= 3 and len(last3) == 3 and all(f(r.get('cpm')) > CPM_TRIP for r in last3)
    rs_reach = f(wk.get('spend')) / f(wk.get('reach')) if f(wk.get('reach')) else None
    freq7 = f(wk.get('frequency'))
    pass_rate = fun['passed'] / fun['checks'] if fun['checks'] >= 50 else None
    cpi = spend_tot / fun['installs'] if fun['installs'] else (spend_tot / inst_meta if inst_meta else None)

    # ---- verdicts
    flags = []
    if aset.get('effective_status') != 'ACTIVE':
        flags.append(f":red_circle: ad set is {aset.get('effective_status')} - nothing below applies")
    if cpm_trip:
        flags.append(f":warning: T1 CPM above Rs {CPM_TRIP:.0f} on each of the last 3 days ({', '.join('%.0f' % f(r['cpm']) for r in last3)}). Check placements and creative; do NOT move budget")
    if day_n >= 10 and spl_7d is not None and spl_7d < SPL_WEEK_FLOOR:
        flags.append(f":red_circle: T2 optimisation events last 7d = {spl_7d} (< {SPL_WEEK_FLOOR}). Learning cannot exit: pause and re-plan the event")
    if day_n >= 14 and pass_rate is not None and pass_rate < PASS_FLOOR:
        flags.append(f":warning: T3 pass rate {pass_rate:.0%} (< {PASS_FLOOR:.0%}, {fun['checks']:.0f} checks). Switch geo to 2 targeting/../5 working/mumbai_targeting_planD_minus_supplyfail_69.csv; run mumbai_zone_read.py first to confirm it is the 29 zones")
    if day_n >= 7 and rs_reach is not None and rs_reach > RS_PER_REACH:
        flags.append(f":warning: T4 Rs {rs_reach:.2f} per reached person (7d) > {RS_PER_REACH}. Ground saturated; stop adding budget")
    if day_n >= 7 and freq7 > FREQ_CAP:
        flags.append(f":warning: T4 frequency {freq7:.1f} (7d) > {FREQ_CAP:.0f}. Same call: no more budget on these circles")
    if budget_3d and abs(budget - budget_3d) / budget_3d > 0.15:
        flags.append(f":warning: budget moved {budget_3d:,.0f} -> {budget:,.0f} inside 3 days (> 15%). Learning likely reset")
    if day_n >= 30:
        cpi_s = f"Rs {cpi:.0f}" if cpi else 'n/a'
        verdict = 'HOLD' if (cpi and cpi < CPI_TARGET and fun['conn'] >= CONN_TARGET) else 'REVIEW'
        flags.append(f":checkered_flag: T5 day-30: CPI {cpi_s} vs {CPI_TARGET:.0f}, connections {fun['conn']:.0f} vs {CONN_TARGET} -> {verdict}. Scale only if idle CSPs are returning and CPM < 60, +15% per 3 days")
    if not flags:
        flags.append(':white_check_mark: no tripwire hit')

    # ---- message
    d = days[-1] if days else {}
    ad_lines = sorted(ads, key=lambda a: -f(a['spend']))[:6]
    text = (f"*MUMBAI_CSP99_SPL daily* - {d1:%a %d %b} (day {day_n} since 12 Sep), budget Rs {budget:,.0f}/day, "
            f"Meta learning: {lsi.get('status', '?')} ({conv_now} events so far)\n"
            f"D-1: spend Rs {f(d.get('spend')):,.0f}, CPM Rs {f(d.get('cpm')):.0f}, impr {f(d.get('impressions')):,.0f}, "
            f"installs (Meta) {actions(d, 'mobile_app_install'):.0f}, link clicks {f(d.get('inline_link_clicks')):.0f}\n"
            f"Trailing 7d: spend Rs {f(wk.get('spend')):,.0f}, reach {f(wk.get('reach')):,.0f}, freq {freq7:.2f}, CPM Rs {f(wk.get('cpm')):.0f}, "
            f"Rs/reached {('%.3f' % rs_reach) if rs_reach else 'n/a'}, SPL events {spl_7d if spl_7d is not None else 'n/a (needs 7 snapshots)'}\n"
            f"Since live: spend Rs {spend_tot:,.0f}, impr {impr_tot:,.0f}, installs Meta {inst_meta:.0f} / dashboard {fun['installs']:.0f}, "
            f"checks {fun['checks']:.0f}, pass {('%.0f%%' % (100 * pass_rate)) if pass_rate is not None else 'n/a (<50 checks)'}, "
            f"bookings {fun['bfc']:.0f}, connections {fun['conn']:.0f}, CPI {('Rs %.0f' % cpi) if cpi else 'n/a'} (cohorts mature 14d)\n"
            f"Model at 13k: CPM {MODEL['cpm']}, ~{MODEL['spl_mo'] // 30}/day SPL, ~{MODEL['installs_mo'] // 30}/day installs, ~{MODEL['bookings_mo'] // 30}/day bookings, ~{MODEL['conns_mo']}/mo connections\n"
            + "\n".join(flags) + "\n"
            + "Top ads 7d: " + "; ".join(f"{(a['ad_name'] or '').split('_')[0]} Rs {f(a['spend']):,.0f} / CPM {f(a['cpm']):.0f} / inst {actions(a, 'mobile_app_install'):.0f}" for a in ad_lines))
    print(text)
    if args.dry_run:
        return
    rp.slack_post(text, dm_only=True)
    json.dump(state, open(STATE, 'w'), indent=1)
    if not args.date:
        from dashboard_readiness import mark_completed_today
        mark_completed_today('mumbai_daily', d1)


if __name__ == '__main__':
    main()
