# -*- coding: utf-8 -*-
"""One-off check: click->install rate, W-o-W, for the Play Store screenshot A/B test that
went live 2026-09-07 (per #demand-gen-and-cap - Karishni/Shiva/Nikhil thread). Nikhil asked
for this specifically on 2026-09-15 (Tue) so a full Mon-Sun post-change week (Sep7-13) has
matured and can be compared against the full week before (Aug31-Sep6) - the partial-week
comparison done live on Sep10 showed the pre/post cut was misleading (confounded by Fri-Sun
being structurally the strongest days of the week) and Nikhil said W-o-W, full week, was the
right comparison. DM-only, one-off - not a recurring pass. Delete playstore_ab_check.yml and
this file once this has run and been reviewed.

Reminder: the screenshot change is an A/B TEST at a confirmed 50/50 split (Nikhil, 2026-09-10),
not a full rollout - Meta/Google can't see which Play Store experiment arm a given install
landed in, so these platform-level numbers are a blend of both arms. With a known 50/50 split
we can back out an estimate of the new-screenshot arm's own CVR: assuming the control arm's CVR
held at the pre-period level (it's still showing the same old screenshots throughout), then
blended_post = 0.5*pre + 0.5*test_arm  =>  test_arm = 2*blended_post - pre. Reported alongside
the raw blended numbers, not in place of them - it's an estimate built on that one assumption,
not a direct measurement.

Usage: python playstore_ab_check.py [--dry-run]
"""
import argparse
import datetime
import json
import os
import urllib.parse
import urllib.request
from collections import defaultdict

import rule_pass as rp

META_CAMPS = {
    'META_BA_MULTI_SCALE_ABO_AppP_BFC-VOLUME_040626',
    'META_BA_DEL_LEARN_ABO_AppP_CREATIVE-TESTING_010626',
}
GOOGLE_CAMPS = [
    "GOOGLE_BA_DEL_SCALE_ABO_UAC_BFC-VOLUME_010626",
    "GOOGLE_BA_DEL_SCALE_ABO_SEARCH_BRAND_01072026",
    "GOOGLE_BA_DEL_SCALE_ABO_SEARCH_L1_03072026",
    "GOOGLE_BA_DEL_SCALE_ABO_SEARCH_L2_04072026",
    "GOOGLE_BA_DEL_SCALE_ABO_SEARCH_P2_05072026",
    "GOOGLE_BA_DEL_SCALE_ABO_DEMANDGEN_YT_BFC_VOLUME_10062026",
]
PRE_WEEK = ('2026-08-31', '2026-09-06')   # Mon-Sun, before the Sep7 deploy
POST_WEEK = ('2026-09-07', '2026-09-13')  # Mon-Sun, after


def meta_week(since, until):
    tok = os.environ['META_ACCESS_TOKEN']
    acc = os.environ.get('META_AD_ACCOUNT_ID', rp.META_ACC_DEFAULT)
    if not str(acc).startswith('act_'):
        acc = 'act_' + str(acc)
    ver = os.environ.get('META_API_VERSION', rp.META_VER_DEFAULT)
    url = f'https://graph.facebook.com/{ver}/{acc}/insights?' + urllib.parse.urlencode({
        'level': 'campaign',
        'fields': 'campaign_name,clicks,actions',
        'time_range': json.dumps({'since': since, 'until': until}),
        'limit': 500,
        'access_token': tok,
    })
    clicks = 0
    installs = 0
    while url:
        with urllib.request.urlopen(url, timeout=60) as r:
            j = json.loads(r.read().decode())
        for row in j.get('data', []):
            if row.get('campaign_name') not in META_CAMPS:
                continue
            clicks += int(row.get('clicks', 0))
            for a in row.get('actions', []):
                if a['action_type'] in ('mobile_app_install', 'omni_app_install'):
                    installs += int(float(a['value']))
        url = (j.get('paging') or {}).get('next')
    return clicks, installs


def google_week(since, until):
    from google.ads.googleads.client import GoogleAdsClient
    config = {
        "developer_token": os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"],
        "client_id": os.environ["GOOGLE_ADS_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_ADS_CLIENT_SECRET"],
        "refresh_token": os.environ["GOOGLE_ADS_REFRESH_TOKEN"],
        "login_customer_id": os.environ.get("GOOGLE_ADS_CUSTOMER_ID", "").replace("-", ""),
        "use_proto_plus": True,
    }
    client = GoogleAdsClient.load_from_dict(config)
    ga = client.get_service("GoogleAdsService")
    cid = config["login_customer_id"]
    camps_sql = ",".join(f"'{c}'" for c in GOOGLE_CAMPS)

    clicks = 0
    q1 = f"""
        SELECT metrics.clicks FROM campaign
        WHERE segments.date BETWEEN '{since}' AND '{until}' AND campaign.name IN ({camps_sql})
    """
    for row in ga.search(customer_id=cid, query=q1):
        clicks += row.metrics.clicks

    installs = 0
    q2 = f"""
        SELECT segments.conversion_action_name, metrics.conversions FROM campaign
        WHERE segments.date BETWEEN '{since}' AND '{until}' AND campaign.name IN ({camps_sql})
          AND segments.conversion_action_name LIKE '%first_open%'
    """
    for row in ga.search(customer_id=cid, query=q2):
        installs += row.metrics.conversions
    return clicks, int(installs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    rp.load_env()

    m_pre_clicks, m_pre_installs = meta_week(*PRE_WEEK)
    m_post_clicks, m_post_installs = meta_week(*POST_WEEK)
    g_pre_clicks, g_pre_installs = google_week(*PRE_WEEK)
    g_post_clicks, g_post_installs = google_week(*POST_WEEK)

    def cvr(c, i):
        return i / c * 100 if c else 0.0

    m_pre_cvr, m_post_cvr = cvr(m_pre_clicks, m_pre_installs), cvr(m_post_clicks, m_post_installs)
    g_pre_cvr, g_post_cvr = cvr(g_pre_clicks, g_pre_installs), cvr(g_post_clicks, g_post_installs)
    m_delta = (m_post_cvr / m_pre_cvr - 1) * 100 if m_pre_cvr else 0.0
    g_delta = (g_post_cvr / g_pre_cvr - 1) * 100 if g_pre_cvr else 0.0

    # Confirmed 50/50 split (Nikhil, 2026-09-10). Back out the new-screenshot arm's own CVR,
    # assuming the control arm held at the pre-period rate: test_arm = 2*blended_post - pre.
    m_test_arm = 2 * m_post_cvr - m_pre_cvr
    g_test_arm = 2 * g_post_cvr - g_pre_cvr
    m_test_delta = (m_test_arm / m_pre_cvr - 1) * 100 if m_pre_cvr else 0.0
    g_test_delta = (g_test_arm / g_pre_cvr - 1) * 100 if g_pre_cvr else 0.0

    msg = (
        f":camera_with_flash: *Play Store screenshot A/B test - full-week W-o-W check*\n"
        f"_Pre: {PRE_WEEK[0]} to {PRE_WEEK[1]} (Mon-Sun) | Post: {POST_WEEK[0]} to {POST_WEEK[1]} (Mon-Sun)_\n"
        f"_Confirmed 50/50 A/B split, not a full rollout - blended numbers below mix both arms; "
        f"'est. new-screenshot arm' backs out the test arm assuming control held at the pre rate._\n\n"
        f"*Meta* (BFC-VOLUME + Creative-Testing, excl. Retargeting)\n"
        f"  Pre:  {m_pre_clicks:,} clicks -> {m_pre_installs:,} installs = {m_pre_cvr:.2f}%\n"
        f"  Post (blended): {m_post_clicks:,} clicks -> {m_post_installs:,} installs = {m_post_cvr:.2f}% ({m_delta:+.1f}%)\n"
        f"  Est. new-screenshot arm: {m_test_arm:.2f}% ({m_test_delta:+.1f}% vs pre)\n\n"
        f"*Google* (UAC + Search Brand/L1/L2/P2 + DemandGen)\n"
        f"  Pre:  {g_pre_clicks:,} clicks -> {g_pre_installs:,} installs = {g_pre_cvr:.2f}%\n"
        f"  Post (blended): {g_post_clicks:,} clicks -> {g_post_installs:,} installs = {g_post_cvr:.2f}% ({g_delta:+.1f}%)\n"
        f"  Est. new-screenshot arm: {g_test_arm:.2f}% ({g_test_delta:+.1f}% vs pre)"
    )

    if args.dry_run:
        print(msg)
    else:
        rp.slack_post(msg, dm_only=True)


if __name__ == '__main__':
    main()
