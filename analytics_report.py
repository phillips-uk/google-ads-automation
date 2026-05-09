"""
Analytics Report — GA4 Data + Search Console combined.

Pulls website analytics and search performance for any configured client
and writes a dated Obsidian note. Works for e-commerce clients and
the personal portfolio alike.

Usage:
  python3 analytics_report.py                             # all ANALYTICS_CLIENTS
  python3 analytics_report.py --client "Lee Renee Jewellery"
  python3 analytics_report.py --client "Phillips Portfolio"
  python3 analytics_report.py --no-sc                    # skip Search Console
  python3 analytics_report.py --no-ga4                   # skip GA4 data
  python3 analytics_report.py --no-monday                # skip Monday.com posting
  python3 analytics_report.py --days 28                  # lookback window (default 28)

Called from weekly_report.py to append site analytics to each client's
weekly note. Also run standalone as needed.
"""

import argparse
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import OBSIDIAN_BASE, load_env

load_env()

VAULT_ROOT = os.path.dirname(OBSIDIAN_BASE)  # .../My Brain

try:
    from clients import ANALYTICS_CLIENTS
except ImportError:
    print("[config] ERROR: ANALYTICS_CLIENTS not found in clients.py.")
    print("         Add it — see the clients.example.py template.")
    sys.exit(1)

import anthropic
import ga4_report as ga4
from seo_search_console import (
    get_sc_service,
    fetch_analytics,
    rows_to_dict,
    totals,
    quick_wins,
    ctr_opportunities,
    declining_queries,
    improving_queries,
    page_ctr_opportunities,
    top_pages,
    new_in_top20,
    _date_range as sc_date_range,
)
from monday_helper import post_audit_issues

TODAY = date.today().isoformat()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _arrow(now, prev, lower_is_better=False):
    if not prev:
        return "—"
    pct = (now - prev) / prev * 100
    better = pct < 0 if lower_is_better else pct > 0
    sym = "▲" if better else "▼"
    return f"{sym} {abs(pct):.1f}%"


def _pos_arrow(now, prev):
    if not prev:
        return "—"
    diff = now - prev
    if abs(diff) < 0.2:
        return "→ unchanged"
    direction = "▲ better" if diff < 0 else "▼ worse"
    return f"{direction} ({prev:.1f} → {now:.1f})"


def _fmt_dur(seconds):
    s = int(seconds)
    m, s = divmod(s, 60)
    return f"{m}m {s:02d}s" if m else f"{s}s"


def _report_dir(cfg):
    """Resolve the correct Obsidian folder for this client."""
    if cfg.get("obsidian_path"):
        return os.path.join(VAULT_ROOT, cfg["obsidian_path"], "Analytics Reports")
    folder = cfg.get("folder")
    if folder:
        return os.path.join(OBSIDIAN_BASE, folder, "Analytics Reports")
    raise ValueError(f"Client config must have 'folder' or 'obsidian_path'.")


# ── AI analysis ────────────────────────────────────────────────────────────────

def _ai_analysis(client_name, ga4_data, sc_cur_t, sc_prev_t, wins, ctr_opps):
    """Claude generates a concise cross-channel commentary."""
    c = anthropic.Anthropic()

    cur = ga4_data["overview"]["current"]
    prv = ga4_data["overview"]["previous"]

    top_ch = "\n".join(
        f"- {r['channel']}: {r['sessions']:,} sessions, {r['conversions']} conversions"
        for r in ga4_data["channels"][:5]
    ) or "No channel data"

    top_ev = "\n".join(
        f"- {e['event']}: {e['count']:,} ({e['conversions']} counted as conversion)"
        for e in ga4_data["events"][:8]
        if e["count"] > 0
    ) or "No event data"

    sc_block = ""
    if sc_cur_t:
        sc_block = f"""
## Search Console (last 7 days)
- Clicks: {sc_cur_t['clicks']:,} ({_arrow(sc_cur_t['clicks'], sc_prev_t['clicks'])})
- Impressions: {sc_cur_t['impressions']:,} ({_arrow(sc_cur_t['impressions'], sc_prev_t['impressions'])})
- Avg CTR: {sc_cur_t['ctr']*100:.2f}%
- Avg Position: {sc_cur_t['position']:.1f}
Quick wins (pos 5–15): {len(wins)} identified
CTR opportunities: {len(ctr_opps)} below benchmark"""

    prompt = f"""You are a digital marketing analyst reviewing a weekly site analytics report for {client_name}.

## GA4 Traffic (last 28 days vs prior 28)
| Metric | This period | Previous | Change |
|---|---|---|---|
| Sessions | {cur['sessions']:,} | {prv['sessions']:,} | {_arrow(cur['sessions'], prv['sessions'])} |
| Users | {cur['users']:,} | {prv['users']:,} | {_arrow(cur['users'], prv['users'])} |
| New Users | {cur['new_users']:,} | {prv['new_users']:,} | {_arrow(cur['new_users'], prv['new_users'])} |
| Engagement Rate | {cur['eng_rate']}% | {prv['eng_rate']}% | — |
| Avg Session | {_fmt_dur(cur['avg_session'])} | {_fmt_dur(prv['avg_session'])} | — |
| Conversions | {cur['conversions']:,} | {prv['conversions']:,} | {_arrow(cur['conversions'], prv['conversions'])} |

## Top Channels
{top_ch}

## Key Events
{top_ev}
{sc_block}

Write a concise analytics commentary with two sections:

## Overall Assessment
2–3 sentences on traffic health, trend direction, and any standout patterns.

## Priority Actions
The 3 most actionable things based on this data. Be specific — reference actual numbers. No generic advice."""

    msg = c.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


# ── Note builder ───────────────────────────────────────────────────────────────

def _build_ga4_section(data):
    cur = data["overview"]["current"]
    prv = data["overview"]["previous"]
    p   = data["period"]

    lines = [
        "## GA4 — Traffic Overview",
        "",
        f"> Period: **{p['start']} → {p['end']}** vs {p['prev_start']} → {p['prev_end']}",
        "",
        "| Metric | This period | vs Previous |",
        "| --- | --- | --- |",
        f"| Sessions | {cur['sessions']:,} | {_arrow(cur['sessions'], prv['sessions'])} |",
        f"| Users | {cur['users']:,} | {_arrow(cur['users'], prv['users'])} |",
        f"| New Users | {cur['new_users']:,} | {_arrow(cur['new_users'], prv['new_users'])} |",
        f"| Engagement Rate | {cur['eng_rate']}% | — |",
        f"| Avg Session Duration | {_fmt_dur(cur['avg_session'])} | — |",
        f"| Pageviews | {cur['pageviews']:,} | {_arrow(cur['pageviews'], prv['pageviews'])} |",
        f"| Conversions | {cur['conversions']:,} | {_arrow(cur['conversions'], prv['conversions'])} |",
        "",
        "---",
        "",
        "## GA4 — Channel Breakdown",
        "",
        "| Channel | Sessions | Users | Conversions | Eng. Rate |",
        "| --- | --- | --- | --- | --- |",
    ]
    for ch in data["channels"]:
        lines.append(
            f"| {ch['channel']} | {ch['sessions']:,} | {ch['users']:,} | "
            f"{ch['conversions']} | {ch['eng_rate']}% |"
        )

    lines += [
        "",
        "---",
        "",
        "## GA4 — Source / Medium",
        "",
        "| Source / Medium | Sessions | Users | Conversions |",
        "| --- | --- | --- | --- |",
    ]
    for s in data["sources"]:
        lines.append(
            f"| {s['source_medium']} | {s['sessions']:,} | {s['users']:,} | {s['conversions']} |"
        )

    lines += [
        "",
        "---",
        "",
        "## GA4 — Top Pages",
        "",
        "| Page | Sessions | Pageviews | Users | Eng. Rate |",
        "| --- | --- | --- | --- | --- |",
    ]
    for pg in data["pages"]:
        lines.append(
            f"| {pg['page']} | {pg['sessions']:,} | {pg['pageviews']:,} | "
            f"{pg['users']:,} | {pg['eng_rate']}% |"
        )

    lines += [
        "",
        "---",
        "",
        "## GA4 — Events",
        "",
        "| Event | Count | Counted as Conversion |",
        "| --- | --- | --- |",
    ]
    for ev in data["events"]:
        conv_flag = "✓" if ev["conversions"] > 0 else "—"
        lines.append(f"| {ev['event']} | {ev['count']:,} | {conv_flag} |")

    lines += [
        "",
        "---",
        "",
        "## GA4 — Devices",
        "",
        "| Device | Sessions | Users |",
        "| --- | --- | --- |",
    ]
    for d in data["devices"]:
        lines.append(f"| {d['device']} | {d['sessions']:,} | {d['users']:,} |")

    return lines


def _build_sc_section(sc_data):
    if not sc_data:
        return ["## Search Console", "", "_Not configured for this client._", ""]

    cur_t, prev_t, wins, ctr_opps, dec, imp, page_op, top_pgs, new_q, period_str = sc_data

    lines = [
        "## Search Console — Overview",
        "",
        f"> Period: **{period_str}**",
        "",
        "| Metric | This week | vs Last week |",
        "| --- | --- | --- |",
        f"| Clicks | {cur_t['clicks']:,} | {_arrow(cur_t['clicks'], prev_t['clicks'])} |",
        f"| Impressions | {cur_t['impressions']:,} | {_arrow(cur_t['impressions'], prev_t['impressions'])} |",
        f"| Avg CTR | {cur_t['ctr']*100:.2f}% | {_arrow(cur_t['ctr'], prev_t['ctr'])} |",
        f"| Avg Position | {cur_t['position']:.1f} | {_pos_arrow(cur_t['position'], prev_t['position'])} |",
        "",
        "---",
        "",
        "## Search Console — Quick Wins (position 5–15)",
        "",
        "| Query | Position | Impressions | Clicks | CTR |",
        "| --- | --- | --- | --- | --- |",
    ]
    for w in wins:
        lines.append(f"| {w['query']} | {w['position']} | {w['impressions']:,} | {w['clicks']} | {w['ctr']}% |")

    lines += [
        "",
        "---",
        "",
        "## Search Console — CTR Opportunities",
        "",
        "| Query | Position | Impressions | CTR | Benchmark | Gap |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for o in ctr_opps:
        lines.append(f"| {o['query']} | {o['position']} | {o['impressions']:,} | {o['ctr_actual']}% | {o['ctr_bench']}% | −{o['gap']}% |")

    lines += [
        "",
        "---",
        "",
        "## Search Console — Top Pages",
        "",
        "| Page | Clicks | Impressions | CTR | Avg Position |",
        "| --- | --- | --- | --- | --- |",
    ]
    for pg in top_pgs:
        short = pg['page'].replace("https://", "").replace("http://", "")
        lines.append(f"| {short} | {pg['clicks']} | {pg['impressions']:,} | {pg['ctr']}% | {pg['position']} |")

    lines += [
        "",
        "---",
        "",
        "## Search Console — Declining Queries",
        "",
        "| Query | Now | Previous | Drop |",
        "| --- | --- | --- | --- |",
    ]
    for d in dec:
        lines.append(f"| {d['query']} | {d['pos_now']} | {d['pos_prev']} | +{d['pos_change']} |")
    if not dec:
        lines.append("| — | — | — | — |")

    lines += [
        "",
        "---",
        "",
        "## Search Console — Improving Queries",
        "",
        "| Query | Now | Previous | Gain |",
        "| --- | --- | --- | --- |",
    ]
    for i in imp:
        lines.append(f"| {i['query']} | {i['pos_now']} | {i['pos_prev']} | +{i['pos_gain']} |")
    if not imp:
        lines.append("| — | — | — | — |")

    return lines


def write_report(client_name, cfg, ga4_data, sc_data, ai_text):
    report_dir = _report_dir(cfg)
    os.makedirs(report_dir, exist_ok=True)
    path = os.path.join(report_dir, f"analytics_{TODAY}.md")

    sc_cur_t = sc_data[0] if sc_data else None
    period_label = f"{ga4_data['period']['start']} → {ga4_data['period']['end']}"

    header = [
        "---",
        f"tags: [analytics, ga4, search-console, {client_name.lower().replace(' ', '-')}]",
        f"date: {TODAY}",
        f"period: {period_label}",
        "---",
        "",
        f"# Analytics Report — {client_name} — {TODAY}",
        "",
        "---",
        "",
        "## AI Commentary",
        "",
        ai_text,
        "",
        "---",
        "",
    ]

    ga4_section = _build_ga4_section(ga4_data)
    sc_section  = _build_sc_section(sc_data)

    lines = header + ga4_section + ["", "---", ""] + sc_section

    with open(path, "w") as f:
        f.write("\n".join(lines))
    return path


# ── Main runner ────────────────────────────────────────────────────────────────

def run_analytics_report(client_name=None, include_ga4=True, include_sc=True,
                         post_to_monday=True, days=28):
    clients_to_run = ANALYTICS_CLIENTS
    if client_name:
        if client_name not in ANALYTICS_CLIENTS:
            print(f"[ERROR] '{client_name}' not in ANALYTICS_CLIENTS. Check clients.py.")
            sys.exit(1)
        clients_to_run = {client_name: ANALYTICS_CLIENTS[client_name]}

    for name, cfg in clients_to_run.items():
        print("\n" + "=" * 65)
        print(f"  Analytics Report — {name}")
        print("=" * 65)

        # ── GA4 Data ───────────────────────────────────────────────────────────
        ga4_data = None
        if include_ga4 and cfg.get("ga4_property_id"):
            try:
                ga4_data = ga4.fetch_all(cfg["ga4_property_id"], days=days)
            except Exception as e:
                print(f"  [GA4 ERROR] {e}")

        if not ga4_data:
            print("  ⚠️  No GA4 data — skipping.")
            continue

        # ── Search Console ─────────────────────────────────────────────────────
        sc_data = None
        if include_sc and cfg.get("sc_site_url"):
            site_url = cfg["sc_site_url"]
            print(f"    Fetching Search Console data for {site_url}...")
            try:
                svc = get_sc_service()
                cur_start, cur_end   = sc_date_range(10, 3)
                prev_start, prev_end = sc_date_range(17, 10)
                period_str = f"{cur_start} → {cur_end}"

                cur_queries  = rows_to_dict(fetch_analytics(svc, site_url, cur_start,  cur_end,  ["query"]))
                prev_queries = rows_to_dict(fetch_analytics(svc, site_url, prev_start, prev_end, ["query"]))
                cur_pages    = rows_to_dict(fetch_analytics(svc, site_url, cur_start,  cur_end,  ["page"]))

                if cur_queries:
                    cur_t  = totals(cur_queries)
                    prev_t = totals(prev_queries) if prev_queries else cur_t
                    wins      = quick_wins(cur_queries)
                    ctr_opps  = ctr_opportunities(cur_queries)
                    dec       = declining_queries(cur_queries, prev_queries)
                    imp       = improving_queries(cur_queries, prev_queries)
                    page_op   = page_ctr_opportunities(cur_pages)
                    top_pgs   = top_pages(cur_pages)
                    new_q     = new_in_top20(cur_queries, prev_queries)

                    sc_data = (cur_t, prev_t, wins, ctr_opps, dec, imp, page_op, top_pgs, new_q, period_str)
                    print(f"    SC — Clicks: {cur_t['clicks']:,}  Impressions: {cur_t['impressions']:,}  "
                          f"Avg pos: {cur_t['position']:.1f}")
                    print(f"    Quick wins: {len(wins)}  |  CTR opps: {len(ctr_opps)}  |  Declining: {len(dec)}")
                else:
                    print("    ⚠️  No Search Console data returned.")
            except Exception as e:
                print(f"  [SC ERROR] {e}")
        elif include_sc and not cfg.get("sc_site_url"):
            print("    ℹ️  No sc_site_url configured — skipping Search Console.")

        # ── AI Commentary ──────────────────────────────────────────────────────
        print("    Generating AI commentary...")
        sc_cur_t  = sc_data[0] if sc_data else None
        sc_prev_t = sc_data[1] if sc_data else None
        wins_     = sc_data[2] if sc_data else []
        ctr_opps_ = sc_data[3] if sc_data else []
        ai_text = _ai_analysis(name, ga4_data, sc_cur_t, sc_prev_t, wins_, ctr_opps_)

        # ── Write note ─────────────────────────────────────────────────────────
        print("    Writing Obsidian note...")
        path = write_report(name, cfg, ga4_data, sc_data, ai_text)
        print(f"  ✅ Report: {path}")

        # ── Monday.com ─────────────────────────────────────────────────────────
        if post_to_monday and cfg.get("monday_client"):
            monday_issues = []
            cur = ga4_data["overview"]["current"]
            prv = ga4_data["overview"]["previous"]
            if prv["sessions"] > 0:
                pct = (cur["sessions"] - prv["sessions"]) / prv["sessions"] * 100
                if pct < -20:
                    monday_issues.append({
                        "severity": "HIGH",
                        "msg": f"GA4 traffic down {abs(pct):.0f}% vs prior 28 days "
                               f"({cur['sessions']:,} sessions vs {prv['sessions']:,}). Investigate.",
                    })
            if sc_data:
                cur_t = sc_data[0]
                if wins_:
                    monday_issues.append({
                        "severity": "MEDIUM",
                        "msg": f"SC quick win: \"{wins_[0]['query']}\" — pos {wins_[0]['position']}, "
                               f"{wins_[0]['impressions']:,} impressions. Push toward top 5.",
                    })
                if ctr_opps_:
                    monday_issues.append({
                        "severity": "MEDIUM",
                        "msg": f"SC CTR fix: \"{ctr_opps_[0]['query']}\" — "
                               f"{ctr_opps_[0]['ctr_actual']}% CTR vs {ctr_opps_[0]['ctr_bench']}% expected. "
                               "Rewrite title and meta description.",
                    })
            if monday_issues:
                post_audit_issues(cfg["monday_client"], monday_issues)

    print("\n" + "=" * 65 + "\n")


# ── Integration: called from weekly_report.py ──────────────────────────────────

def run_for_weekly(client_name, days=28):
    """
    Lightweight version for weekly_report.py integration.
    Returns (ga4_data, sc_data) tuple for embedding in the weekly note,
    or (None, None) if the client isn't in ANALYTICS_CLIENTS.
    """
    if client_name not in ANALYTICS_CLIENTS:
        return None, None

    cfg = ANALYTICS_CLIENTS[client_name]

    ga4_data = None
    if cfg.get("ga4_property_id"):
        try:
            ga4_data = ga4.fetch_all(cfg["ga4_property_id"], days=days)
        except Exception as e:
            print(f"  [GA4 ERROR for {client_name}] {e}")
            return None, None

    sc_data = None
    if cfg.get("sc_site_url") and ga4_data:
        site_url = cfg["sc_site_url"]
        try:
            svc = get_sc_service()
            cur_start, cur_end   = sc_date_range(10, 3)
            prev_start, prev_end = sc_date_range(17, 10)
            period_str = f"{cur_start} → {cur_end}"

            cur_queries  = rows_to_dict(fetch_analytics(svc, site_url, cur_start,  cur_end,  ["query"]))
            prev_queries = rows_to_dict(fetch_analytics(svc, site_url, prev_start, prev_end, ["query"]))
            cur_pages    = rows_to_dict(fetch_analytics(svc, site_url, cur_start,  cur_end,  ["page"]))

            if cur_queries:
                cur_t  = totals(cur_queries)
                prev_t = totals(prev_queries) if prev_queries else cur_t
                wins      = quick_wins(cur_queries)
                ctr_opps  = ctr_opportunities(cur_queries)
                dec       = declining_queries(cur_queries, prev_queries)
                imp       = improving_queries(cur_queries, prev_queries)
                page_op   = page_ctr_opportunities(cur_pages)
                top_pgs   = top_pages(cur_pages)
                new_q     = new_in_top20(cur_queries, prev_queries)
                sc_data = (cur_t, prev_t, wins, ctr_opps, dec, imp, page_op, top_pgs, new_q, period_str)
        except Exception as e:
            print(f"  [SC ERROR for {client_name}] {e}")

    return ga4_data, sc_data


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Analytics report — GA4 + Search Console")
    parser.add_argument("--client",     help="Run for a specific client only")
    parser.add_argument("--no-sc",      action="store_true", help="Skip Search Console")
    parser.add_argument("--no-ga4",     action="store_true", help="Skip GA4 data")
    parser.add_argument("--no-monday",  action="store_true", help="Skip Monday.com")
    parser.add_argument("--days",       type=int, default=28, help="GA4 lookback days (default 28)")
    args = parser.parse_args()

    run_analytics_report(
        client_name=args.client,
        include_ga4=not args.no_ga4,
        include_sc=not args.no_sc,
        post_to_monday=not args.no_monday,
        days=args.days,
    )


if __name__ == "__main__":
    main()
