# -*- coding: utf-8 -*-
"""EXPERIMENTAL L7D kill-pass diff - NOT the production pass, does not affect it.

Compares the production daily kill-pass's ACTUAL logged decisions (from
kill_pass_log.json - what really got posted and evaluated) against a fresh run of
decide(..., variant='l7d') for the same date, and DMs Nikhil ONLY (hardcoded
dm_only=True - never posts to #growth-reports, no matter what). Purely diagnostic:
writes nothing to kill_pass_log.json, kill_action_log.csv, or creative_activation_state.json
- only its own small idempotency marker (l7d_diff_state.json).

Diffs against the production log's recorded decisions (not a fresh lifetime
recompute) deliberately: Meta's live effective_status drifts between runs (a creative
paused after production ran would silently change a fresh recompute's pool), so
reading what was actually posted is the only faithful ground truth for "did the L7D
variant actually disagree with what really happened."

Runs via .github/workflows/l7d-diff-pass.yml, triggered by workflow_run after "Daily
BFC-VOLUME rule-pass" completes - so it always has that run's fresh log entry to
diff against.

Run: python l7d_diff_pass.py [--dry-run] [--date YYYY-MM-DD]
"""
import os, sys, json, datetime, argparse
import rule_pass as rp

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'l7d_diff_state.json')


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {'last_diffed': None}


def save_state(state):
    with open(STATE_PATH, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)


def latest_production_entry():
    log = rp.load_log()
    if not log:
        return None
    return max(log, key=lambda e: e['date'])


def build_diff_message(prod_entry, res_l7d, d1):
    prod_kills = {r['concept_id']: r for r in prod_entry.get('recos', []) if r.get('verdict') == 'KILL'}
    l7d_kills = {t[0]: t for t in res_l7d['kills']}
    l7d_verdicts = res_l7d.get('verdict', {})

    spared = [c for c in prod_kills if c not in l7d_kills]      # prod killed it, L7D would not
    new_kills = [c for c in l7d_kills if c not in prod_kills]   # L7D kills it, prod did not

    lines = [
        f":test_tube: *EXPERIMENTAL (L7D variant) kill-pass diff* ({d1.isoformat()}, DEL BOOKNOW)",
        "_Not a recommendation - do not act on this. Compares production's actual logged decisions "
        "against an L7-day-CPBC variant still under observation. DM-only, never posted to #growth-reports._",
        "",
    ]
    if not spared and not new_kills:
        lines.append("No disagreement today - the L7D variant agrees with every production KILL.")
    else:
        if spared:
            lines.append(f"*L7D would SPARE ({len(spared)})* - production killed these, L7D variant would not:")
            for c in spared:
                pr = prod_kills[c]
                l7v = l7d_verdicts.get(c, 'no longer active in Meta right now (paused since?) - can\'t re-judge live')
                x = pr.get('cpbc'); xs = f"Rs{x:,.0f}" if x is not None else 'inf'
                lines.append(f"   - `{c}` prod killed at {xs} ({pr.get('reason','')})  ->  L7D verdict: *{l7v}*")
            lines.append("")
        if new_kills:
            lines.append(f"*L7D would ALSO KILL ({len(new_kills)})* - production did not flag these:")
            for c in new_kills:
                lines.append(rp._row(*l7d_kills[c]))
            lines.append("")
    prod_med = prod_entry.get('median_cpbc')
    l7_med = res_l7d.get('median')
    med_line = "median basis: production (lifetime) "
    med_line += f"Rs{prod_med:,.0f}" if prod_med is not None else "n/a"
    med_line += " | L7D "
    med_line += f"Rs{l7_med:,.0f}" if l7_med is not None else "n/a"
    lines.append(f"_{med_line}_")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help='print the message, do not post or write state')
    ap.add_argument('--date', help='override; default = latest date in production kill_pass_log.json')
    ap.add_argument('--no-post', action='store_true',
                     help='run for real (write state) but skip Slack - for backfilling on corrected data '
                          'without re-notifying; print the message instead of posting it')
    args = ap.parse_args()
    rp.load_env()

    prod_entry = latest_production_entry()
    if not prod_entry:
        print('no production kill-pass log entries yet - nothing to diff against')
        return
    d1 = datetime.date.fromisoformat(args.date) if args.date else datetime.date.fromisoformat(prod_entry['date'])

    state = load_state()
    if not args.dry_run and state.get('last_diffed') == d1.isoformat():
        print(f'already diffed {d1} - skipping (idempotent retry guard)')
        return

    last_activation = rp.get_last_activation_dates(d1)
    data, age, cstar, funnel_geo = rp.compute(d1, last_activation)
    active, _ad_ids_map = rp.meta_active_del()
    res_l7d = rp.decide(data, age, cstar, active, funnel_geo=funnel_geo, variant='l7d')

    msg = build_diff_message(prod_entry, res_l7d, d1)
    if args.dry_run:
        print(msg)
    else:
        print(msg)
        if not args.no_post:
            rp.slack_post(msg, dm_only=True)  # hardcoded - this script never posts to the channel
        state['last_diffed'] = d1.isoformat()
        save_state(state)


if __name__ == '__main__':
    main()
