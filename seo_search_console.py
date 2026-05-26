"""
Search Console Performance Agent

Pulls 7-day query and page performance from Google Search Console,
compares to the previous 7 days, and surfaces:
  - Quick wins (position 5-15, high impressions — close to ranking, easy push)
  - CTR opportunities (ranking well but not getting clicked — title/meta problem)
  - Top pages by clicks
  - Declining queries (position worsening week-on-week)
  - Improving queries
  - New queries entering the top 20

Uses Claude to generate prioritised, jewellery-specific recommendations.
Writes a dated Markdown report to Obsidian.
Posts actionable items to Monday.com.

Usage:
  python3 seo_search_console.py                            # all configured SC clients
  python3 seo_search_console.py --client "Lee Renee Jewellery"
  python3 seo_search_console.py --client "Lee Renee Jewellery" --no-monday
"""

import os
import sys
import argparse
from datetime import date, timedelta

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import anthropic
from config import OBSIDIAN_BASE, load_env
from monday_helper import post_audit_issues

load_env()

# ── Client config — loaded from clients.py (gitignored, never committed) ───────
try:
    from clients import SC_CLIENTS
except ImportError:
    print("[config] ERROR: clients.py not found or SC_CLIENTS not defined.")
    print("         Add SC_CLIENTS to your clients.py. See clients.example.py.")
    sys.exit(1)

_DIR = os.path.dirname(__file__)

# Expected CTR benchmarks by rounded position (rough industry averages for informational/commercial)
_CTR_BENCH = {1: 0.28, 2: 0.15, 3: 0.10, 4: 0.07, 5: 0.05,
              6: 0.04, 7: 0.03, 8: 0.03, 9: 0.02, 10: 0.02}


def _expected_ctr(position):
    return _CTR_BENCH.get(max(1, min(10, round(position))), 0.01)


# ── Auth — OAuth (sc_token.json) ──────────────────────────────────────────────
# Search Console UI and API do not accept service account emails as users.
# OAuth token (webmasters.readonly) is retained. Auto-refreshes on use.

_SC_TOKEN_FILE  = os.path.join(_DIR, "sc_token.json")
_SC_CLIENT_FILE = os.path.join(_DIR, "gtm_client.json")  # same OAuth client
_SC_SCOPES      = ["https://www.googleapis.com/auth/webmasters.readonly"]


def get_sc_service():
    """Build Search Console service using OAuth (sc_token.json). Auto-refreshes."""
    import json as _json
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    with open(_SC_TOKEN_FILE) as f:
        token_data = _json.load(f)
    with open(_SC_CLIENT_FILE) as f:
        client_raw = _json.load(f)
    client = client_raw.get("installed") or client_raw.get("web") or client_raw

    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data["refresh_token"],
        client_id=client["client_id"],
        client_secret=client["client_secret"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=_SC_SCOPES,
    )
    if not creds.valid:
        creds.refresh(Request())
        token_data["token"] = creds.token
        with open(_SC_TOKEN_FILE, "w") as f:
            _json.dump(token_data, f, indent=2)
    return build("webmasters", "v3", credentials=creds)


# ── Date helpers ──────────────────────────────────────────────────────────────

def _date_range(days_ago_start, days_ago_end):
    """Returns (start, end) as ISO strings, offset from today to account for SC's ~3-day data lag."""
    today = date.today()
    return (
        (today - timedelta(days=days_ago_start)).isoformat(),
        (today - timedelta(days=days_ago_end)).isoformat(),
    )


# ── Data fetch ────────────────────────────────────────────────────────────────

def fetch_analytics(service, site_url, start_date, end_date, dimensions, row_limit=1000):
    """Fetch search analytics rows for given dimensions and date range."""
    body = {
        "startDate":  start_date,
        "endDate":    end_date,
        "dimensions": dimensions,
        "rowLimit":   row_limit,
    }
    try:
        resp = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
        return resp.get("rows", [])
    except HttpError as e:
        print(f"  [SC API error] {e}")
        return []


def rows_to_dict(rows):
    """Convert API rows to {key: {clicks, impressions, ctr, position}} dict.
    Key is the first dimension value (query string or page URL)."""
    result = {}
    for row in rows:
        key = row["keys"][0]
        result[key] = {
            "clicks":      int(row.get("clicks", 0)),
            "impressions": int(row.get("impressions", 0)),
            "ctr":         float(row.get("ctr", 0.0)),
            "position":    float(row.get("position", 0.0)),
        }
    return result


# ── Analysis ──────────────────────────────────────────────────────────────────

def totals(data):
    clicks = sum(v["clicks"] for v in data.values())
    imps   = sum(v["impressions"] for v in data.values())
    ctr    = clicks / imps if imps else 0.0
    pos_vals = [v["position"] for v in data.values() if v["impressions"] > 0]
    avg_pos  = sum(pos_vals) / len(pos_vals) if pos_vals else 0.0
    return {"clicks": clicks, "impressions": imps, "ctr": ctr, "position": avg_pos}


def quick_wins(query_data, min_impressions=20, pos_min=4.0, pos_max=15.0):
    """Queries ranking 5–15 with decent impressions — best ROI to push toward top 3."""
    result = [
        {
            "query":       q,
            "position":    round(m["position"], 1),
            "impressions": m["impressions"],
            "clicks":      m["clicks"],
            "ctr":         round(m["ctr"] * 100, 1),
        }
        for q, m in query_data.items()
        if pos_min < m["position"] <= pos_max and m["impressions"] >= min_impressions
    ]
    return sorted(result, key=lambda x: x["impressions"], reverse=True)[:20]


def ctr_opportunities(query_data, min_impressions=30):
    """Queries where CTR is >40% below the expected benchmark for their position."""
    result = []
    for q, m in query_data.items():
        if m["impressions"] < min_impressions:
            continue
        bench  = _expected_ctr(m["position"])
        actual = m["ctr"]
        if bench > 0 and actual < bench * 0.60:
            result.append({
                "query":       q,
                "position":    round(m["position"], 1),
                "impressions": m["impressions"],
                "ctr_actual":  round(actual * 100, 1),
                "ctr_bench":   round(bench * 100, 1),
                "gap":         round((bench - actual) * 100, 1),
            })
    return sorted(result, key=lambda x: x["impressions"], reverse=True)[:15]


def declining_queries(current, previous, min_impressions=15, threshold=3.0):
    """Queries where average position worsened by 3+ places WoW."""
    result = []
    for q, cur in current.items():
        if q not in previous or cur["impressions"] < min_impressions:
            continue
        drop = cur["position"] - previous[q]["position"]
        if drop >= threshold:
            result.append({
                "query":      q,
                "pos_now":    round(cur["position"], 1),
                "pos_prev":   round(previous[q]["position"], 1),
                "pos_change": round(drop, 1),
                "impressions": cur["impressions"],
            })
    return sorted(result, key=lambda x: x["pos_change"], reverse=True)[:10]


def improving_queries(current, previous, min_impressions=15, threshold=3.0):
    """Queries gaining 3+ positions WoW."""
    result = []
    for q, cur in current.items():
        if q not in previous or cur["impressions"] < min_impressions:
            continue
        gain = previous[q]["position"] - cur["position"]
        if gain >= threshold:
            result.append({
                "query":      q,
                "pos_now":    round(cur["position"], 1),
                "pos_prev":   round(previous[q]["position"], 1),
                "pos_gain":   round(gain, 1),
                "impressions": cur["impressions"],
            })
    return sorted(result, key=lambda x: x["pos_gain"], reverse=True)[:10]


def new_in_top20(current, previous):
    """Queries that appeared in the data this week and weren't present last week."""
    result = [
        {
            "query":       q,
            "position":    round(m["position"], 1),
            "impressions": m["impressions"],
            "clicks":      m["clicks"],
        }
        for q, m in current.items()
        if q not in previous and m["position"] <= 20
    ]
    return sorted(result, key=lambda x: x["impressions"], reverse=True)[:10]


def page_ctr_opportunities(page_data, min_impressions=30):
    """Pages where CTR is significantly below benchmark — usually a title/meta issue."""
    result = []
    for page, m in page_data.items():
        if m["impressions"] < min_impressions:
            continue
        bench  = _expected_ctr(m["position"])
        actual = m["ctr"]
        if bench > 0 and actual < bench * 0.60:
            result.append({
                "page":        page,
                "position":    round(m["position"], 1),
                "impressions": m["impressions"],
                "clicks":      m["clicks"],
                "ctr":         round(actual * 100, 1),
                "ctr_bench":   round(bench * 100, 1),
            })
    return sorted(result, key=lambda x: x["impressions"], reverse=True)[:10]


def top_pages(page_data, n=10):
    """Top pages by click volume."""
    result = [
        {
            "page":        page,
            "clicks":      m["clicks"],
            "impressions": m["impressions"],
            "ctr":         round(m["ctr"] * 100, 1),
            "position":    round(m["position"], 1),
        }
        for page, m in page_data.items()
    ]
    return sorted(result, key=lambda x: x["clicks"], reverse=True)[:n]


# ── AI analysis ───────────────────────────────────────────────────────────────

def ai_analysis(client_name, cur_t, prev_t, wins, ctr_opps, declining, page_opps, new_q):
    """Claude generates prioritised, client-specific recommendations."""
    client = anthropic.Anthropic()

    def pct_str(now, prev):
        if not prev:
            return "—"
        c = (now - prev) / prev * 100
        return f"{'+'  if c >= 0 else ''}{c:.1f}%"

    wins_txt = "\n".join(
        f"- \"{w['query']}\" — pos {w['position']}, {w['impressions']:,} impressions, {w['ctr']}% CTR"
        for w in wins[:8]
    ) or "None identified"

    ctr_txt = "\n".join(
        f"- \"{o['query']}\" — pos {o['position']}, CTR {o['ctr_actual']}% vs {o['ctr_bench']}% expected, {o['impressions']:,} impressions"
        for o in ctr_opps[:6]
    ) or "None identified"

    dec_txt = "\n".join(
        f"- \"{d['query']}\" — pos {d['pos_prev']} → {d['pos_now']} ({d['pos_change']:+.1f})"
        for d in declining[:5]
    ) or "None"

    page_txt = "\n".join(
        f"- {p['page'].replace('https://', '')} — pos {p['position']}, CTR {p['ctr']}% vs {p['ctr_bench']}% expected"
        for p in page_opps[:5]
    ) or "None"

    new_txt = "\n".join(
        f"- \"{n['query']}\" — pos {n['position']}, {n['impressions']} impressions"
        for n in new_q[:5]
    ) or "None"

    prompt = f"""You are an SEO specialist reviewing a weekly Search Console report for {client_name}, a fine jewellery e-commerce brand selling sterling silver, gold vermeil, and gemstone pieces.

## Performance: this week vs last week
| Metric | This week | Last week | Change |
|---|---|---|---|
| Clicks | {cur_t['clicks']:,} | {prev_t['clicks']:,} | {pct_str(cur_t['clicks'], prev_t['clicks'])} |
| Impressions | {cur_t['impressions']:,} | {prev_t['impressions']:,} | {pct_str(cur_t['impressions'], prev_t['impressions'])} |
| Avg CTR | {cur_t['ctr']*100:.2f}% | {prev_t['ctr']*100:.2f}% | {pct_str(cur_t['ctr'], prev_t['ctr'])} |
| Avg Position | {cur_t['position']:.1f} | {prev_t['position']:.1f} | — |

## Quick wins — position 5–15
{wins_txt}

## CTR below benchmark
{ctr_txt}

## Declining queries (position drop WoW)
{dec_txt}

## Pages with CTR below benchmark
{page_txt}

## New queries in top 20
{new_txt}

Provide:
## Overall Assessment
2–3 sentences on the week's organic performance.

## Priority Actions
The 3–5 most impactful things to act on this week. For title tag and meta description issues, write the specific revised copy — don't just say "improve the title tag." Consider jewellery buyer intent (gift occasion, material, style, price point).

## CTR Fixes
For each low-CTR query above, suggest a specific revised title tag (under 60 chars) and meta description (under 160 chars) that better matches buyer intent for a jewellery brand.

## Watch List
Declining queries to monitor. Any pattern or likely cause?

## Opportunity
Anything in new entries or quick wins worth investing in — content, internal links, or product page improvements?

Be specific. No generic SEO advice."""

    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1800,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


# ── Report writing ────────────────────────────────────────────────────────────

def write_report(client_name, folder, cur_t, prev_t,
                 wins, ctr_opps, declining, improving,
                 page_opps, top_pgs, new_q, ai_text, period_str):
    report_dir = os.path.join(OBSIDIAN_BASE, folder, "Search Console")
    os.makedirs(report_dir, exist_ok=True)
    today = date.today().isoformat()
    path  = os.path.join(report_dir, f"sc_weekly_{today}.md")

    def arrow(now, prev, lower_is_better=False):
        if not prev:
            return "—"
        change = (now - prev) / prev * 100
        better = change < 0 if lower_is_better else change > 0
        sym    = "▲" if better else "▼"
        return f"{sym} {abs(change):.1f}%"

    def pos_arrow(now, prev):
        # For position, lower is better
        if not prev:
            return "—"
        diff = now - prev
        if abs(diff) < 0.2:
            return "→ unchanged"
        sym = "▲ better" if diff < 0 else "▼ worse"
        return f"{sym} ({prev:.1f} → {now:.1f})"

    lines = [
        "---",
        f"tags: [search-console, seo, {client_name.lower().replace(' ', '-')}]",
        f"date: {today}",
        f"period: {period_str}",
        "---",
        "",
        f"# Search Console Weekly — {client_name} — {today}",
        "",
        "## Performance Overview",
        "",
        "| Metric | This week | vs Last week |",
        "| --- | --- | --- |",
        f"| Clicks | {cur_t['clicks']:,} | {arrow(cur_t['clicks'], prev_t['clicks'])} |",
        f"| Impressions | {cur_t['impressions']:,} | {arrow(cur_t['impressions'], prev_t['impressions'])} |",
        f"| Avg CTR | {cur_t['ctr']*100:.2f}% | {arrow(cur_t['ctr'], prev_t['ctr'])} |",
        f"| Avg Position | {cur_t['position']:.1f} | {pos_arrow(cur_t['position'], prev_t['position'])} |",
        "",
        "---",
        "",
        "## AI Analysis & Recommendations",
        "",
        ai_text,
        "",
        "---",
        "",
        "## Quick Wins — Position 5–15",
        "",
        "> Queries where Google already ranks you close to page 1. A small push (better content, internal links, title relevance) can move position 8 to position 3 — which gets 3–4× more clicks.",
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
        "## CTR Opportunities — Query Level",
        "",
        "> Ranking well but not getting clicked. The title tag and meta description are your 'ad copy' for organic search. If CTR is half the benchmark, rewriting them can double clicks with no change in ranking.",
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
        "## CTR Opportunities — Page Level",
        "",
        "| Page | Position | Impressions | Clicks | CTR | Benchmark |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for p in page_opps:
        short_url = p['page'].replace("https://", "").replace("http://", "")
        lines.append(f"| {short_url} | {p['position']} | {p['impressions']:,} | {p['clicks']} | {p['ctr']}% | {p['ctr_bench']}% |")

    lines += [
        "",
        "---",
        "",
        "## Top Pages by Clicks",
        "",
        "| Page | Clicks | Impressions | CTR | Avg Position |",
        "| --- | --- | --- | --- | --- |",
    ]
    for p in top_pgs:
        short_url = p['page'].replace("https://", "").replace("http://", "")
        lines.append(f"| {short_url} | {p['clicks']} | {p['impressions']:,} | {p['ctr']}% | {p['position']} |")

    lines += [
        "",
        "---",
        "",
        "## Declining Queries",
        "",
        "> Position worsening WoW. One week of decline is normal — investigate if it persists for 2+ weeks.",
        "",
        "| Query | Now | Previous | Drop |",
        "| --- | --- | --- | --- |",
    ]
    for d in declining:
        lines.append(f"| {d['query']} | {d['pos_now']} | {d['pos_prev']} | +{d['pos_change']} |")

    lines += [
        "",
        "---",
        "",
        "## Improving Queries",
        "",
        "| Query | Now | Previous | Gain |",
        "| --- | --- | --- | --- |",
    ]
    for i in improving:
        lines.append(f"| {i['query']} | {i['pos_now']} | {i['pos_prev']} | +{i['pos_gain']} |")

    lines += [
        "",
        "---",
        "",
        "## New in Top 20",
        "",
        "| Query | Position | Impressions | Clicks |",
        "| --- | --- | --- | --- |",
    ]
    for n in new_q:
        lines.append(f"| {n['query']} | {n['position']} | {n['impressions']} | {n['clicks']} |")

    lines += [
        "",
        "---",
        "",
        "## Learning Notes",
        "",
        "**Quick wins (position 5–15):** Google has already decided you're relevant for these queries — you're on page 1 or just off it. Position 3 gets roughly 3× more clicks than position 8. The lever: better content depth, more relevant title tag, internal links from other pages using the query as anchor text.",
        "",
        "**CTR below benchmark:** You're ranking but people aren't clicking. The title tag and meta description are your organic 'ad copy' — they don't affect ranking directly, but they determine whether the ranking translates into traffic. Jewellery CTR is often hurt by generic titles ('Silver Ring — Lee Renée') when buyers are searching for something specific ('sterling silver pearl ring gift'). Match the searcher's intent in the title.",
        "",
        "**Declining queries:** A single week of position drop is rarely meaningful — could be a crawl fluctuation, a competitor publishing new content, or Google testing a layout change. Watch for 2+ consecutive weeks of decline before acting. When you do act: check if a competitor has recently published better content on the same topic.",
        "",
        "**Average CTR for jewellery e-commerce:** Position 1 averages ~28-30% CTR. Position 3 ~10%. Position 5 ~5%. Anything significantly below these benchmarks at the same position = the title/meta isn't compelling enough for that specific query.",
    ]

    with open(path, "w") as f:
        f.write("\n".join(lines))
    return path


# ── Monday.com ────────────────────────────────────────────────────────────────

def _monday_issues(wins, ctr_opps, declining):
    issues = []
    if wins:
        top = wins[0]
        issues.append({
            "severity": "MEDIUM",
            "msg": (
                f"SC quick win: \"{top['query']}\" — pos {top['position']} with "
                f"{top['impressions']:,} impressions. Optimise title/content to push into top 5."
            ),
        })
    if ctr_opps:
        top = ctr_opps[0]
        issues.append({
            "severity": "MEDIUM",
            "msg": (
                f"SC CTR fix: \"{top['query']}\" — pos {top['position']}, "
                f"{top['ctr_actual']}% CTR vs {top['ctr_bench']}% expected. "
                "Rewrite title tag and meta description."
            ),
        })
    if len(declining) >= 3:
        top3 = ", ".join(f'"{d["query"]}"' for d in declining[:3])
        issues.append({
            "severity": "LOW",
            "msg": f"SC decline watch: {len(declining)} queries losing position. Monitor: {top3}.",
        })
    return issues


# ── Main ──────────────────────────────────────────────────────────────────────

def run_sc_report(client_name=None, post_to_monday=True):
    clients_to_run = SC_CLIENTS
    if client_name:
        if client_name not in SC_CLIENTS:
            print(f"[ERROR] '{client_name}' not found in SC_CLIENTS. Check clients.py.")
            sys.exit(1)
        clients_to_run = {client_name: SC_CLIENTS[client_name]}

    for name, cfg in clients_to_run.items():
        site_url = cfg["site_url"]
        folder   = cfg["folder"]

        print("\n" + "=" * 65)
        print(f"  Search Console Weekly — {name}")
        print("=" * 65)

        print("  Authenticating with Search Console...")
        svc = get_sc_service()

        # Current period: 7 days ending 3 days ago (SC lag)
        cur_start, cur_end   = _date_range(10, 3)
        prev_start, prev_end = _date_range(17, 10)
        period_str = f"{cur_start} → {cur_end}"
        print(f"  Period : {cur_start} to {cur_end}")
        print(f"  Compare: {prev_start} to {prev_end}")

        print("  Fetching query data (current + previous)...")
        cur_queries  = rows_to_dict(fetch_analytics(svc, site_url, cur_start,  cur_end,  ["query"]))
        prev_queries = rows_to_dict(fetch_analytics(svc, site_url, prev_start, prev_end, ["query"]))

        print("  Fetching page data...")
        cur_pages = rows_to_dict(fetch_analytics(svc, site_url, cur_start, cur_end, ["page"]))

        if not cur_queries:
            print("  ⚠️  No data returned — verify site_url in clients.py and Search Console permissions.")
            continue

        print(f"  Queries: {len(cur_queries):,}  |  Pages: {len(cur_pages):,}")

        cur_t  = totals(cur_queries)
        prev_t = totals(prev_queries) if prev_queries else cur_t

        wins     = quick_wins(cur_queries)
        ctr_opps = ctr_opportunities(cur_queries)
        dec      = declining_queries(cur_queries, prev_queries)
        imp      = improving_queries(cur_queries, prev_queries)
        page_op  = page_ctr_opportunities(cur_pages)
        top_pgs  = top_pages(cur_pages)
        new_q    = new_in_top20(cur_queries, prev_queries)

        print(f"\n  Clicks: {cur_t['clicks']:,}  Impressions: {cur_t['impressions']:,}  "
              f"CTR: {cur_t['ctr']*100:.2f}%  Avg pos: {cur_t['position']:.1f}")
        print(f"  Quick wins: {len(wins)}  |  CTR opps: {len(ctr_opps)}  |  Declining: {len(dec)}")

        print("\n  Generating AI analysis...")
        ai_text = ai_analysis(name, cur_t, prev_t, wins, ctr_opps, dec, page_op, new_q)

        print("  Writing report to Obsidian...")
        path = write_report(
            name, folder, cur_t, prev_t,
            wins, ctr_opps, dec, imp, page_op, top_pgs, new_q,
            ai_text, period_str,
        )
        print(f"  ✅ Report: {path}")

        if post_to_monday:
            monday_issues = _monday_issues(wins, ctr_opps, dec)
            if monday_issues:
                print(f"\n  Posting {len(monday_issues)} item(s) to Monday.com...")
                post_audit_issues(name, monday_issues)

    print("\n" + "=" * 65 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Search Console weekly performance report")
    parser.add_argument("--client", help="Run for a specific client only")
    parser.add_argument("--no-monday", action="store_true", help="Skip Monday.com posting")
    args = parser.parse_args()
    run_sc_report(client_name=args.client, post_to_monday=not args.no_monday)


if __name__ == "__main__":
    main()
