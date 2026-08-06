# -*- coding: utf-8 -*-
"""Weekly creative-staleness check - NOT part of the kill-pass, does not affect it.

Prompted by a LinkedIn post (Arindam Paul, 2026-08-06) arguing that most accounts that
struggle to scale Meta profitably have a creative problem, not a media-buying one, and
proposing 3 diagnostic questions: who are the top spenders, how recent are they, and
are they distinct concepts or variants of each other. This is that audit, automated,
for BFC-VOLUME DEL.

Ranks by CURRENT run-rate (L7-day daily average: w7s/7), not lifetime spend -
lifetime spend is an age-confounded metric (an old concept accumulates a large total
even at a low current rate, a new one looks small even if it's winning right now).
A same-day gut-check on 2026-08-06 found 8 of the top-10 LIFETIME spenders were
sitting at Rs0/day currently, while 6 of the real top-10 CURRENT spenders were <=6
days old - lifetime ranking would have completely inverted the read. Every section
below uses w7s (or w7s/7) as the ranking basis for exactly this reason.

Does NOT do concept-vs-variant clustering (would need the creative registry's
brief_id, which needs Google Sheets access this script's CI runner doesn't have -
gspread needs local OAuth creds, see rule_pass.py's sheet_sync()). That check stays a
manual/session pass for now (ad-hoc, as done 2026-08-06) - flagged here rather than
silently skipped.

Run: python creative_staleness_check.py [--dry-run] [--date YYYY-MM-DD] [--dm-only]
"""
import os, datetime, argparse
import rule_pass as rp

ZOMBIE_LIFETIME_THRESHOLD = 20000   # Rs lifetime spend, above which a Rs0 L7-day rate is worth flagging
NEW_BATCH_WINDOW_DAYS     = 14      # "still early" - matches roughly 2x AGE_GRACE_DAYS
NEW_BATCH_MIN_L7_SPEND    = 2000    # Rs - the bar for "actually got tested", not just parked
FRESHNESS_AGE_THRESHOLD   = 14      # flag if nothing this young cracks the top-5 by current rate
TOP2_CONCENTRATION_WARN   = 0.40    # flag if top-2 concepts hold >40% of current pool spend


def build_report(pool, age, d1):
    spent = [(c, pool[c]) for c in pool if pool[c]['spend'] > 0]
    by_l7 = sorted(spent, key=lambda t: -t[1].get('w7s', 0))
    by_lifetime = sorted(spent, key=lambda t: -t[1]['spend'])

    total_l7 = sum(rec.get('w7s', 0) for _c, rec in spent)
    top10_current = by_l7[:10]

    zombies = [(c, rec) for c, rec in by_lifetime
               if rec['spend'] >= ZOMBIE_LIFETIME_THRESHOLD and rec.get('w7s', 0) == 0]

    new_batch = [(c, rec) for c, rec in spent if age.get(c, 999) <= NEW_BATCH_WINDOW_DAYS]
    new_tested = [(c, rec) for c, rec in new_batch if rec.get('w7s', 0) >= NEW_BATCH_MIN_L7_SPEND]
    new_stuck = [(c, rec) for c, rec in new_batch if rec.get('w7s', 0) < NEW_BATCH_MIN_L7_SPEND]

    top5_fresh = [c for c, rec in by_l7[:5] if age.get(c, 999) <= FRESHNESS_AGE_THRESHOLD]

    top2_share = None
    if total_l7 > 0 and len(by_l7) >= 1:
        top2_spend = sum(rec.get('w7s', 0) for _c, rec in by_l7[:2])
        top2_share = top2_spend / total_l7

    problems = []
    if not top5_fresh:
        problems.append(f"No concept under {FRESHNESS_AGE_THRESHOLD}d old is in the current top-5 by spend rate - "
                         f"nothing fresh has broken into real scale recently.")
    if top2_share is not None and top2_share > TOP2_CONCENTRATION_WARN:
        problems.append(f"Top 2 concepts hold {top2_share*100:.0f}% of current pool spend "
                         f"(warn line {TOP2_CONCENTRATION_WARN*100:.0f}%) - high reliance on a couple of creatives.")
    if new_batch and not new_tested:
        problems.append(f"{len(new_batch)} concept(s) launched in the last {NEW_BATCH_WINDOW_DAYS}d, "
                         f"none have crossed Rs{NEW_BATCH_MIN_L7_SPEND:,} in L7-day spend - new batch isn't getting tested.")
    if zombies:
        problems.append(f"{len(zombies)} concept(s) have real lifetime spend (>=Rs{ZOMBIE_LIFETIME_THRESHOLD:,}) "
                         f"but Rs0 in the last 7 days - dead but still showing up in lifetime-spend views.")

    lines = [f":microscope: *Weekly creative-staleness check* ({d1.isoformat()}, DEL BFC-VOLUME)",
             "_Ranked by CURRENT run-rate (L7-day avg), not lifetime spend - lifetime is age-confounded. "
             "Not a kill-pass action, no auto-decisions here. DM-only._", ""]

    if problems:
        lines.append(f"*FLAGGED ({len(problems)})*")
        for p in problems:
            lines.append(f"   - {p}")
        lines.append("")
    else:
        lines.append(":white_check_mark: No staleness flags this week.")
        lines.append("")

    lines.append(f"*Top 10 by current run-rate* (of {len(spent)} active, pool L7 spend/day Rs{total_l7/7:,.0f})")
    for c, rec in top10_current:
        a = age.get(c, '?')
        l7d = rec.get('w7s', 0) / 7
        fresh_tag = "  :seedling: fresh" if isinstance(a, int) and a <= FRESHNESS_AGE_THRESHOLD else ""
        lines.append(f"   - `{c}` [{rec['layer']}/{rec['need']}] Rs{l7d:,.0f}/day now "
                     f"(Rs{rec['spend']:,.0f} lifetime, {a}d old){fresh_tag}")
    lines.append("")

    lines.append(f"*New batch* (<= {NEW_BATCH_WINDOW_DAYS}d old): {len(new_batch)} concept(s), "
                 f"{len(new_tested)} tested (>=Rs{NEW_BATCH_MIN_L7_SPEND:,} L7 spend), {len(new_stuck)} stuck near-zero")
    if new_stuck:
        lines.append("   stuck: " + ", ".join(f"`{c}`" for c, _r in new_stuck))
    lines.append("")

    if zombies:
        lines.append(f"*Zombies* (big lifetime spend, Rs0 last 7d) ({len(zombies)})")
        lines.append("   " + ", ".join(f"`{c}` (Rs{rec['spend']:,.0f} lifetime)" for c, rec in zombies[:10]))
        lines.append("")

    lines.append("_Not covered: concept-vs-variant clustering (needs registry access this automated run doesn't have) "
                 "- do that as a manual pass when this flags concentration or staleness._")
    lines.append(rp.ads_link())
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--date', help='override D-1 anchor YYYY-MM-DD (default = yesterday IST)')
    ap.add_argument('--dm-only', action='store_true', default=True,
                     help='defaults to True - this report has not been reviewed for #growth-reports yet')
    ap.add_argument('--post-to-channel', action='store_true', help='override --dm-only, post to #growth-reports too')
    args = ap.parse_args()
    rp.load_env()

    if args.date:
        d1 = datetime.date.fromisoformat(args.date)
    else:
        now_ist = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)
        d1 = (now_ist - datetime.timedelta(days=1)).date()

    last_activation = rp.get_last_activation_dates(d1)
    data, age, _cstar, _funnel_geo = rp.compute(d1, last_activation)
    pool = data.get('Delhi', {})

    msg = build_report(pool, age, d1)
    if args.dry_run:
        print(msg)
    else:
        rp.slack_post(msg, dm_only=not args.post_to_channel)


if __name__ == '__main__':
    main()
