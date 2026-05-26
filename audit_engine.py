"""
Audit Engine — Campaign-Type-Aware Google Ads Account Auditor
=============================================================
Replaces the search-only audit approach with per-type auditors, spend-weighted
scoring, and three scale modes so it works for both small boutique accounts
and enterprise accounts with 100+ campaigns.

Architecture:
  - Batch GAQL: all data fetched in minimal API round-trips
  - Campaign type routing: SEARCH / PERFORMANCE_MAX / SHOPPING / DISPLAY / VIDEO
  - Per-type auditors: type-specific checks and benchmarks
  - Spend-weighted health scoring: 100-point scale
  - Scale modes:
      simple     (≤10 campaigns)  — full detail, per-campaign
      mid_tier   (11–50)          — per-type summary + outliers
      enterprise (51+)            — aggregate distribution + top outliers only

Usage:
  python3 audit_engine.py                                    # all accounts
  python3 audit_engine.py --account "Lee Renee Jewellery"   # single account
  python3 audit_engine.py --dry-run                         # no Obsidian write

Reports saved to:
  <OBSIDIAN_BASE>/<Account>/Performance Reports/account_audit_YYYY-MM-DD.md
"""

import os
import sys
import json
import argparse
from dataclasses import dataclass, field
from datetime import date
from typing import Optional
from collections import defaultdict

import anthropic
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

from config import OBSIDIAN_BASE, load_env
from monday_helper import post_audit_issues

load_env()

YAML_FILE = os.path.join(os.path.dirname(__file__), "google-ads.yaml")

try:
    from clients import AUDIT_ACCOUNTS
except ImportError:
    print("[audit_engine] ERROR: clients.py not found.")
    sys.exit(1)


# ── Scale mode thresholds ─────────────────────────────────────────────────────

SCALE_SIMPLE     = 10
SCALE_MID_TIER   = 50
# enterprise = anything above SCALE_MID_TIER

def get_scale_mode(campaign_count: int) -> str:
    if campaign_count <= SCALE_SIMPLE:
        return "simple"
    elif campaign_count <= SCALE_MID_TIER:
        return "mid_tier"
    else:
        return "enterprise"


# ── Per-type benchmarks ───────────────────────────────────────────────────────

BENCHMARKS = {
    "SEARCH": {
        "ctr_min":          0.05,    # 5% — flag below this
        "ctr_warn":         0.03,    # 3% — warning zone
        "qs_min":           7.0,     # average QS below this = issue
        "qs_warn":          5.0,     # QS below this = critical per keyword
        "impression_share_min": 0.40,
        "wasted_spend_pct_max": 0.10, # flag if >10% spend on non-converting terms
    },
    "PERFORMANCE_MAX": {
        # Asset group strength: EXCELLENT > GOOD > AVERAGE > POOR > NO_ASSETS
        "min_strength":     "GOOD",
        "poor_pct_max":     0.25,   # flag if >25% of asset groups are POOR
        # No CTR benchmark — PMAX blends all placements
    },
    "SHOPPING": {
        "ctr_min":          0.01,    # 1%
        "impression_share_min": 0.30,
    },
    "DISPLAY": {
        "ctr_min":          0.002,   # 0.2%
    },
    "VIDEO": {
        "view_rate_min":    0.25,    # 25% view rate
    },
}

STRENGTH_ORDER = ["NO_ASSETS", "POOR", "AVERAGE", "GOOD", "EXCELLENT", "PENDING"]
STRENGTH_RANK  = {s: i for i, s in enumerate(STRENGTH_ORDER)}


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class Finding:
    severity:   str        # CRITICAL | HIGH | MEDIUM | LOW | INFO
    category:   str        # PMAX | SEARCH | SHOPPING | DISPLAY | VIDEO | ACCOUNT
    msg:        str
    detail:     str = ""   # optional machine-readable detail (JSON string)
    campaign_id: Optional[int] = None

    def icon(self) -> str:
        return {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡",
                "LOW": "🔵", "INFO": "⚪"}.get(self.severity, "⚪")

    def to_monday_dict(self) -> dict:
        return {"severity": self.severity, "msg": f"[{self.category}] {self.msg}"}


@dataclass
class CampaignRow:
    id:           int
    name:         str
    status:       str
    channel_type: str           # SEARCH | PERFORMANCE_MAX | SHOPPING | DISPLAY | VIDEO
    bidding:      str
    clicks:       int   = 0
    impressions:  int   = 0
    cost_micros:  int   = 0
    conversions:  float = 0.0
    conv_value:   float = 0.0
    search_is:    float = 0.0   # impression share (Search/Shopping only)
    budget_lost_is: float = 0.0
    rank_lost_is:   float = 0.0

    @property
    def cost(self) -> float:
        return self.cost_micros / 1_000_000

    @property
    def ctr(self) -> float:
        return self.clicks / self.impressions if self.impressions else 0

    @property
    def cpa(self) -> float:
        return self.cost / self.conversions if self.conversions else 0

    @property
    def roas(self) -> float:
        return self.conv_value / self.cost if self.cost else 0


@dataclass
class AuditResult:
    account_name:  str
    scale_mode:    str
    campaigns:     list[CampaignRow] = field(default_factory=list)
    findings:      list[Finding]     = field(default_factory=list)
    health_score:  float             = 0.0
    type_summary:  dict              = field(default_factory=dict)   # channel_type → count
    total_spend:   float             = 0.0
    total_conversions: float         = 0.0
    ai_plan:       str               = ""

    def add(self, finding: Finding):
        self.findings.append(finding)

    def critical_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "CRITICAL")

    def high_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "HIGH")


# ── Batch GAQL Fetcher ────────────────────────────────────────────────────────

class BatchFetcher:
    """
    Fetches all required data in the minimum number of GAQL queries.
    All queries are read-only, no mutations.
    """

    def __init__(self, ads_client: GoogleAdsClient, customer_id: str):
        self.svc          = ads_client.get_service("GoogleAdsService")
        self.customer_id  = customer_id

    def _search(self, query: str) -> list:
        try:
            return list(self.svc.search(customer_id=self.customer_id, query=query))
        except GoogleAdsException as ex:
            msg = ex.failure.errors[0].message if ex.failure.errors else str(ex)
            print(f"    [WARN] GAQL failed: {msg[:120]}")
            return []

    # ── Campaigns (always fetched) ────────────────────────────────────────────

    def fetch_campaigns(self) -> list[CampaignRow]:
        rows = self._search("""
            SELECT
                campaign.id,
                campaign.name,
                campaign.status,
                campaign.advertising_channel_type,
                campaign.bidding_strategy_type,
                metrics.clicks,
                metrics.impressions,
                metrics.cost_micros,
                metrics.conversions,
                metrics.conversions_value,
                metrics.search_impression_share,
                metrics.search_budget_lost_impression_share,
                metrics.search_rank_lost_impression_share
            FROM campaign
            WHERE campaign.status = 'ENABLED'
            AND segments.date DURING LAST_30_DAYS
        """)
        out = []
        for r in rows:
            c = r.campaign
            m = r.metrics
            # Impression share: GA returns string "-" when not applicable
            def _is(val):
                try:
                    return float(val) if val and str(val) not in ("", "--") else 0.0
                except (ValueError, TypeError):
                    return 0.0
            out.append(CampaignRow(
                id=c.id, name=c.name,
                status=c.status.name,
                channel_type=c.advertising_channel_type.name,
                bidding=c.bidding_strategy_type.name,
                clicks=m.clicks,
                impressions=m.impressions,
                cost_micros=m.cost_micros,
                conversions=m.conversions,
                conv_value=m.conversions_value,
                search_is=_is(m.search_impression_share),
                budget_lost_is=_is(m.search_budget_lost_impression_share),
                rank_lost_is=_is(m.search_rank_lost_impression_share),
            ))
        return out

    # ── PMAX asset groups ─────────────────────────────────────────────────────

    def fetch_asset_groups(self) -> list[dict]:
        rows = self._search("""
            SELECT
                asset_group.id,
                asset_group.name,
                asset_group.ad_strength,
                asset_group.status,
                campaign.id,
                campaign.name
            FROM asset_group
            WHERE campaign.advertising_channel_type = 'PERFORMANCE_MAX'
            AND campaign.status = 'ENABLED'
        """)
        return [
            {
                "id":         r.asset_group.id,
                "name":       r.asset_group.name,
                "strength":   r.asset_group.ad_strength.name,
                "status":     r.asset_group.status.name,
                "campaign_id":   r.campaign.id,
                "campaign_name": r.campaign.name,
            }
            for r in rows
        ]

    # ── Search: keyword QS ────────────────────────────────────────────────────

    def fetch_keyword_qs(self, enterprise_mode: bool = False) -> list[dict]:
        """
        Fetch keyword quality scores for SEARCH campaigns.
        In enterprise mode, only fetch keywords with > 100 impressions to cap row count.
        """
        impression_floor = 100 if enterprise_mode else 0
        filter_clause = f"AND metrics.impressions > {impression_floor}" if enterprise_mode else ""
        rows = self._search(f"""
            SELECT
                ad_group_criterion.keyword.text,
                ad_group_criterion.quality_info.quality_score,
                campaign.id,
                campaign.name,
                ad_group.name,
                metrics.impressions,
                metrics.cost_micros
            FROM keyword_view
            WHERE campaign.advertising_channel_type = 'SEARCH'
            AND campaign.status = 'ENABLED'
            AND ad_group_criterion.status != 'REMOVED'
            AND ad_group_criterion.quality_info.quality_score > 0
            {filter_clause}
            AND segments.date DURING LAST_30_DAYS
        """)
        return [
            {
                "keyword":     r.ad_group_criterion.keyword.text,
                "qs":          r.ad_group_criterion.quality_info.quality_score,
                "campaign_id": r.campaign.id,
                "campaign":    r.campaign.name,
                "ad_group":    r.ad_group.name,
                "impressions": r.metrics.impressions,
                "cost":        r.metrics.cost_micros / 1_000_000,
            }
            for r in rows
        ]

    # ── Search: wasted spend (unmatched search terms) ─────────────────────────

    def fetch_search_term_waste(self, limit: int = 50) -> list[dict]:
        """
        Search terms with status NONE (no negative match) that spent money but
        didn't convert — these are wasted spend candidates.
        """
        rows = self._search(f"""
            SELECT
                search_term_view.search_term,
                campaign.id,
                campaign.name,
                metrics.clicks,
                metrics.cost_micros,
                metrics.conversions
            FROM search_term_view
            WHERE campaign.advertising_channel_type = 'SEARCH'
            AND campaign.status = 'ENABLED'
            AND search_term_view.status = 'NONE'
            AND metrics.cost_micros > 0
            AND segments.date DURING LAST_30_DAYS
            ORDER BY metrics.cost_micros DESC
            LIMIT {limit}
        """)
        return [
            {
                "term":        r.search_term_view.search_term,
                "campaign_id": r.campaign.id,
                "campaign":    r.campaign.name,
                "clicks":      r.metrics.clicks,
                "cost":        r.metrics.cost_micros / 1_000_000,
                "conversions": r.metrics.conversions,
            }
            for r in rows
        ]

    # ── Campaign-level assets (sitelinks, callouts, etc.) ─────────────────────

    def fetch_campaign_assets(self) -> dict[int, set[str]]:
        """Return {campaign_id: set of asset_types} for enabled campaign assets."""
        rows = self._search("""
            SELECT
                campaign.id,
                campaign_asset.asset_type,
                campaign_asset.status
            FROM campaign_asset
            WHERE campaign.status = 'ENABLED'
            AND campaign_asset.status = 'ENABLED'
        """)
        out: dict[int, set[str]] = defaultdict(set)
        for r in rows:
            out[r.campaign.id].add(r.campaign_asset.asset_type.name)
        return dict(out)

    # ── Ad group counts per campaign ──────────────────────────────────────────

    def fetch_ad_group_counts(self) -> dict[int, int]:
        """Return {campaign_id: active_ad_group_count}."""
        rows = self._search("""
            SELECT
                campaign.id,
                ad_group.id
            FROM ad_group
            WHERE campaign.status = 'ENABLED'
            AND ad_group.status = 'ENABLED'
        """)
        counts: dict[int, int] = defaultdict(int)
        for r in rows:
            counts[r.campaign.id] += 1
        return dict(counts)


# ── Per-type Auditors ─────────────────────────────────────────────────────────

class SearchAuditor:
    """Audits SEARCH campaigns: QS, CTR, IS, SQR waste, sitelink coverage."""

    REQUIRED_ASSETS = {"SITELINK", "CALLOUT", "STRUCTURED_SNIPPET"}

    def run(
        self,
        campaigns: list[CampaignRow],
        keywords:  list[dict],
        waste_terms: list[dict],
        campaign_assets: dict[int, set[str]],
        scale_mode: str,
    ) -> list[Finding]:
        findings = []
        b = BENCHMARKS["SEARCH"]
        search_campaigns = [c for c in campaigns if c.channel_type == "SEARCH"]

        if not search_campaigns:
            return findings

        # ── Per-campaign CTR + IS (simple / mid_tier: per campaign; enterprise: aggregate) ──
        if scale_mode == "enterprise":
            findings += self._enterprise_checks(search_campaigns, b)
        else:
            for camp in search_campaigns:
                findings += self._campaign_checks(camp, campaign_assets, b)

        # ── Quality Score analysis ────────────────────────────────────────────
        if keywords:
            findings += self._qs_checks(keywords, scale_mode, b)

        # ── Wasted spend analysis ─────────────────────────────────────────────
        if waste_terms:
            findings += self._waste_checks(waste_terms, search_campaigns, b)

        return findings

    def _campaign_checks(
        self,
        c: CampaignRow,
        campaign_assets: dict[int, set[str]],
        b: dict,
    ) -> list[Finding]:
        findings = []
        b_ctr_min  = b["ctr_min"]
        b_ctr_warn = b["ctr_warn"]
        b_is_min   = b["impression_share_min"]

        # CTR
        if c.impressions > 500:
            if c.ctr < b_ctr_warn:
                findings.append(Finding(
                    severity="HIGH", category="SEARCH",
                    msg=f"'{c.name}' CTR {c.ctr:.1%} is critically low (benchmark ≥{b_ctr_min:.0%}). "
                        "Check ad copy relevance, keyword match types, and Quality Scores.",
                    campaign_id=c.id,
                ))
            elif c.ctr < b_ctr_min:
                findings.append(Finding(
                    severity="MEDIUM", category="SEARCH",
                    msg=f"'{c.name}' CTR {c.ctr:.1%} is below benchmark ≥{b_ctr_min:.0%}. "
                        "Review ad copy and consider RSA asset pinning.",
                    campaign_id=c.id,
                ))

        # Impression share
        if c.impressions > 100 and c.search_is > 0:
            if c.search_is < b_is_min:
                lost_budget = c.budget_lost_is
                lost_rank   = c.rank_lost_is
                cause = "budget" if lost_budget > lost_rank else "ad rank"
                findings.append(Finding(
                    severity="MEDIUM", category="SEARCH",
                    msg=f"'{c.name}' Search IS {c.search_is:.0%} (lost to {cause}: "
                        f"budget {lost_budget:.0%}, rank {lost_rank:.0%}). "
                        f"{'Increase budget.' if cause == 'budget' else 'Improve QS or bid.'}",
                    campaign_id=c.id,
                ))

        # Asset coverage (sitelinks/callouts/structured snippets)
        assets = campaign_assets.get(c.id, set())
        missing = self.REQUIRED_ASSETS - assets
        if missing:
            findings.append(Finding(
                severity="MEDIUM", category="SEARCH",
                msg=f"'{c.name}' missing assets: {', '.join(sorted(missing))}. "
                    "These extensions increase CTR by 10–15% on average.",
                campaign_id=c.id,
            ))

        return findings

    def _enterprise_checks(self, campaigns: list[CampaignRow], b: dict) -> list[Finding]:
        """Aggregate checks for enterprise-scale Search campaigns."""
        findings = []
        enabled = [c for c in campaigns if c.status == "ENABLED" and c.impressions > 500]

        low_ctr = [c for c in enabled if c.ctr < b["ctr_warn"]]
        low_is  = [c for c in enabled if 0 < c.search_is < b["impression_share_min"]]

        if low_ctr:
            total_cost = sum(c.cost for c in low_ctr)
            findings.append(Finding(
                severity="HIGH", category="SEARCH",
                msg=f"{len(low_ctr)} SEARCH campaigns have CTR below {b['ctr_warn']:.0%} "
                    f"(£{total_cost:,.0f} combined spend). Top offenders: "
                    + ", ".join(f"'{c.name}' ({c.ctr:.1%})" for c in
                                sorted(low_ctr, key=lambda x: x.cost, reverse=True)[:5]),
            ))

        if low_is:
            findings.append(Finding(
                severity="MEDIUM", category="SEARCH",
                msg=f"{len(low_is)} SEARCH campaigns below {b['impression_share_min']:.0%} IS. "
                    "Likely budget-constrained. Review shared budgets or campaign priorities.",
            ))

        return findings

    def _qs_checks(self, keywords: list[dict], scale_mode: str, b: dict) -> list[Finding]:
        findings = []

        if not keywords:
            return findings

        qs_values  = [k["qs"] for k in keywords if k["qs"] > 0]
        avg_qs     = sum(qs_values) / len(qs_values) if qs_values else 0
        poor_qs    = [k for k in keywords if k["qs"] <= b["qs_warn"]]
        spend_poor = sum(k["cost"] for k in poor_qs)

        if avg_qs < b["qs_min"]:
            findings.append(Finding(
                severity="HIGH", category="SEARCH",
                msg=f"Average Quality Score {avg_qs:.1f}/10 is below target {b['qs_min']:.0f}. "
                    f"{len(poor_qs)} keywords score ≤{b['qs_warn']:.0f} (£{spend_poor:.0f} spend). "
                    "Low QS inflates CPCs by 20–50% — review landing page relevance and ad copy.",
            ))
        elif poor_qs:
            findings.append(Finding(
                severity="MEDIUM", category="SEARCH",
                msg=f"{len(poor_qs)} keywords score ≤{b['qs_warn']:.0f}/10 (£{spend_poor:.0f} spend). "
                    "Pause or improve the worst performers to reduce wasted CPC premium.",
            ))

        return findings

    def _waste_checks(self, terms: list[dict], campaigns: list[CampaignRow], b: dict) -> list[Finding]:
        findings = []
        wasted = [t for t in terms if t["conversions"] == 0 and t["cost"] > 0]
        total_wasted = sum(t["cost"] for t in wasted)
        total_spend  = sum(c.cost for c in campaigns)

        if total_spend > 0 and total_wasted / total_spend > b["wasted_spend_pct_max"]:
            top5 = sorted(wasted, key=lambda x: x["cost"], reverse=True)[:5]
            findings.append(Finding(
                severity="HIGH", category="SEARCH",
                msg=f"£{total_wasted:.0f} ({total_wasted/total_spend:.0%} of Search spend) on "
                    f"zero-conversion terms not yet negated. "
                    "Top wasted terms: " + ", ".join(f"'{t['term']}' (£{t['cost']:.0f})" for t in top5),
            ))
        elif total_wasted > 10:
            findings.append(Finding(
                severity="LOW", category="SEARCH",
                msg=f"£{total_wasted:.0f} on zero-conversion unmatched search terms. "
                    "Review and add negatives for the top spenders.",
            ))

        return findings


class PMaxAuditor:
    """Audits PERFORMANCE_MAX campaigns: asset group strength, URL expansion."""

    def run(
        self,
        campaigns:    list[CampaignRow],
        asset_groups: list[dict],
        scale_mode:   str,
    ) -> list[Finding]:
        findings = []
        b = BENCHMARKS["PERFORMANCE_MAX"]
        pmax_campaigns = [c for c in campaigns if c.channel_type == "PERFORMANCE_MAX"]

        if not pmax_campaigns:
            return findings

        # ── Asset group strength ──────────────────────────────────────────────
        if asset_groups:
            findings += self._asset_strength_checks(asset_groups, pmax_campaigns, b, scale_mode)

        # ── Zero-spend PMAX campaigns ─────────────────────────────────────────
        for c in pmax_campaigns:
            if c.impressions == 0 and c.cost == 0:
                findings.append(Finding(
                    severity="MEDIUM", category="PMAX",
                    msg=f"PMAX campaign '{c.name}' has zero impressions in 30 days. "
                        "Check asset groups, audience signals, and product feed eligibility.",
                    campaign_id=c.id,
                ))

        # ── PMAX alongside Shopping — cannibalisation risk ────────────────────
        pmax_ids     = {c.id for c in pmax_campaigns}
        has_shopping = any(c.channel_type == "SHOPPING" for c in campaigns)
        if has_shopping and pmax_ids:
            findings.append(Finding(
                severity="MEDIUM", category="PMAX",
                msg=f"PMAX and Standard Shopping campaigns coexist. PMAX takes priority on "
                    "identical product queries — verify Shopping campaign IS hasn't dropped. "
                    "If PMAX handles all Shopping volume, consider consolidating.",
            ))

        # ── No audience signals ───────────────────────────────────────────────
        # We can't query audience signals via GAQL (they're on asset groups in the UI),
        # so flag as a check-item if any PMAX campaign is new (low spend).
        low_conv_pmax = [c for c in pmax_campaigns if c.conversions < 50 and c.cost > 100]
        if low_conv_pmax:
            findings.append(Finding(
                severity="LOW", category="PMAX",
                msg=f"{len(low_conv_pmax)} PMAX campaign(s) have <50 conversions (last 30 days) "
                    "but meaningful spend. Verify audience signals are set — they guide PMAX "
                    "learning before sufficient conversion history is available.",
            ))

        return findings

    def _asset_strength_checks(
        self,
        asset_groups: list[dict],
        campaigns: list[CampaignRow],
        b: dict,
        scale_mode: str,
    ) -> list[Finding]:
        findings = []
        min_strength_rank = STRENGTH_RANK.get(b["min_strength"], 3)

        poor = [ag for ag in asset_groups
                if ag["status"] == "ENABLED"
                and STRENGTH_RANK.get(ag["strength"], 0) < min_strength_rank
                and ag["strength"] not in ("PENDING", "NO_ASSETS")]

        good = [ag for ag in asset_groups
                if ag["status"] == "ENABLED"
                and STRENGTH_RANK.get(ag["strength"], 0) >= min_strength_rank]

        total_enabled = len([ag for ag in asset_groups if ag["status"] == "ENABLED"])

        if not total_enabled:
            return findings

        poor_pct = len(poor) / total_enabled

        if poor_pct > b["poor_pct_max"]:
            if scale_mode == "simple":
                poor_detail = ", ".join(
                    f"'{ag['campaign_name']} → {ag['name']}' ({ag['strength']})"
                    for ag in poor[:5]
                )
                findings.append(Finding(
                    severity="HIGH", category="PMAX",
                    msg=f"{len(poor)}/{total_enabled} asset groups are below GOOD strength. "
                        "PMAX serves fewer auction types with weak assets — "
                        f"fix: {poor_detail}" + ("..." if len(poor) > 5 else ""),
                ))
            else:
                by_campaign: dict[str, list] = defaultdict(list)
                for ag in poor:
                    by_campaign[ag["campaign_name"]].append(ag["strength"])
                summary = ", ".join(
                    f"'{camp}' ({len(ags)} poor)"
                    for camp, ags in list(by_campaign.items())[:5]
                )
                findings.append(Finding(
                    severity="HIGH", category="PMAX",
                    msg=f"{len(poor)}/{total_enabled} PMAX asset groups are POOR/AVERAGE. "
                        f"Campaigns affected: {summary}. "
                        "Add missing headlines/descriptions/images to reach GOOD+.",
                ))
        elif poor:
            findings.append(Finding(
                severity="LOW", category="PMAX",
                msg=f"{len(poor)} asset group(s) are POOR/AVERAGE. "
                    "All others are GOOD or EXCELLENT. "
                    "Prioritise fixing: " + ", ".join(f"'{ag['name']}'" for ag in poor[:3]),
            ))

        return findings


class ShoppingAuditor:
    """Audits SHOPPING campaigns: IS, CTR."""

    def run(self, campaigns: list[CampaignRow], scale_mode: str) -> list[Finding]:
        findings = []
        b = BENCHMARKS["SHOPPING"]
        shopping = [c for c in campaigns if c.channel_type == "SHOPPING"]

        if not shopping:
            return findings

        for c in shopping:
            if c.impressions < 100:
                continue
            if 0 < c.search_is < b["impression_share_min"]:
                cause = "budget" if c.budget_lost_is > c.rank_lost_is else "ad rank/feed quality"
                findings.append(Finding(
                    severity="MEDIUM", category="SHOPPING",
                    msg=f"Shopping '{c.name}' IS {c.search_is:.0%} — lost to {cause}. "
                        f"{'Increase campaign budget.' if 'budget' in cause else 'Improve feed quality and bidding.'}",
                    campaign_id=c.id,
                ))
            if c.ctr < b["ctr_min"] and c.impressions > 1000:
                findings.append(Finding(
                    severity="MEDIUM", category="SHOPPING",
                    msg=f"Shopping '{c.name}' CTR {c.ctr:.2%} is low. "
                        "Review product titles, images, and pricing competitiveness.",
                    campaign_id=c.id,
                ))

        return findings


class DisplayAuditor:
    """Audits DISPLAY campaigns: basic health checks."""

    def run(self, campaigns: list[CampaignRow]) -> list[Finding]:
        findings = []
        b = BENCHMARKS["DISPLAY"]
        display = [c for c in campaigns if c.channel_type == "DISPLAY"]

        for c in display:
            if c.impressions < 5000:
                continue
            if c.ctr < b["ctr_min"]:
                findings.append(Finding(
                    severity="LOW", category="DISPLAY",
                    msg=f"Display '{c.name}' CTR {c.ctr:.3%} is below {b['ctr_min']:.1%} benchmark. "
                        "Review creative assets and audience targeting.",
                    campaign_id=c.id,
                ))

        return findings


class VideoAuditor:
    """Audits VIDEO campaigns: view rate."""

    def run(self, campaigns: list[CampaignRow]) -> list[Finding]:
        # Video campaigns don't expose view_rate in standard metrics via GAQL easily
        # without joining video_performance_view — flag a reminder instead
        video = [c for c in campaigns if c.channel_type == "VIDEO"]
        findings = []

        for c in video:
            if c.impressions > 0 and c.cost > 50:
                findings.append(Finding(
                    severity="INFO", category="VIDEO",
                    msg=f"Video campaign '{c.name}' active (£{c.cost:.0f} spend). "
                        "Check view rate and audience overlap in Google Ads UI — "
                        "GAQL does not expose view rate without video_performance_view.",
                    campaign_id=c.id,
                ))

        return findings


# ── Spend-Weighted Health Scorer ──────────────────────────────────────────────

class AccountScorer:
    """
    Produces a 0–100 health score weighted by campaign spend.
    Penalties applied per finding severity:
      CRITICAL  -20 pts   HIGH      -10 pts
      MEDIUM    -4 pts    LOW       -1 pt
    Score floors at 0 and ceilings at 100.
    """

    PENALTIES = {"CRITICAL": 20, "HIGH": 10, "MEDIUM": 4, "LOW": 1, "INFO": 0}

    def score(self, findings: list[Finding]) -> float:
        score = 100.0
        for f in findings:
            score -= self.PENALTIES.get(f.severity, 0)
        return max(0.0, min(100.0, score))

    def grade(self, score: float) -> str:
        if score >= 85:  return "✅ Healthy"
        if score >= 65:  return "🟡 Needs Attention"
        if score >= 40:  return "🟠 Action Required"
        return "🔴 Critical Issues"


# ── AI Recommendations ────────────────────────────────────────────────────────

def generate_ai_plan(result: AuditResult) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return "*(ANTHROPIC_API_KEY not set — AI plan skipped)*"

    try:
        issues_text = "\n".join(
            f"  [{f.severity}] [{f.category}] {f.msg}"
            for f in result.findings
        )

        type_summary = ", ".join(
            f"{count} {ctype}" for ctype, count in result.type_summary.items()
        )

        prompt = (
            f"You are a senior Google Ads specialist auditing **{result.account_name}**.\n\n"
            f"Account: {type_summary} campaigns | Scale: {result.scale_mode} | "
            f"Health score: {result.health_score:.0f}/100 | "
            f"Total spend (30d): £{result.total_spend:,.0f} | "
            f"Total conversions: {result.total_conversions:.0f}\n\n"
            f"FINDINGS ({len(result.findings)} total — {result.critical_count()} critical, "
            f"{result.high_count()} high):\n{issues_text or '  None.'}\n\n"
            "Write a structured implementation plan:\n\n"
            "## Immediate Actions (Critical + High — fix this week)\n"
            "Numbered steps. Be specific about which campaign, what to change, what target to hit.\n\n"
            "## Optimisation Backlog (Medium — next 30 days)\n"
            "Numbered steps.\n\n"
            "## Quick Wins\n"
            "3–5 bullet points: actions that take <30 min and have high impact.\n\n"
            "## Expected Impact\n"
            "1 paragraph: what these changes should do to CPA, ROAS, or conversion volume.\n\n"
            "Keep it under 600 words. Be direct — no filler."
        )

        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=(
                "You are a senior Google Ads specialist. Write precise, actionable instructions. "
                "Reference specific campaign names from the findings. No filler. Markdown only."
            ),
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()

    except Exception as e:
        return f"*(AI plan generation failed: {e})*"


# ── Obsidian Report Writer ────────────────────────────────────────────────────

def write_report(result: AuditResult, dry_run: bool = False) -> str:
    folder    = os.path.join(OBSIDIAN_BASE, result.account_name, "Performance Reports")
    date_slug = date.today().strftime("%Y-%m-%d")
    filepath  = os.path.join(folder, f"account_audit_{date_slug}.md")

    scorer = AccountScorer()
    grade  = scorer.grade(result.health_score)

    type_rows = "\n".join(
        f"| {ctype} | {count} |"
        for ctype, count in sorted(result.type_summary.items())
    )

    sev_order   = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
    sev_icons   = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🔵", "INFO": "⚪"}
    by_severity: dict[str, list[Finding]] = defaultdict(list)
    for f in result.findings:
        by_severity[f.severity].append(f)

    issues_md = ""
    for sev in sev_order:
        for f in by_severity.get(sev, []):
            issues_md += f"{sev_icons[sev]} **{sev}** `{f.category}` — {f.msg}\n\n"
    if not issues_md:
        issues_md = "*No issues detected.*\n"

    # Campaign table (capped at 30 rows for readability)
    campaign_rows = sorted(result.campaigns, key=lambda c: c.cost, reverse=True)[:30]
    camp_table = "| Campaign | Type | Spend | Conversions | CPA | CTR | IS | Status |\n"
    camp_table += "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
    for c in campaign_rows:
        is_str  = f"{c.search_is:.0%}" if c.search_is else "—"
        cpa_str = f"£{c.cpa:.2f}" if c.cpa else "—"
        camp_table += (
            f"| {c.name} | {c.channel_type} | £{c.cost:,.0f} "
            f"| {c.conversions:.0f} | {cpa_str} | {c.ctr:.2%} | {is_str} | {c.status} |\n"
        )

    lines = [
        "---",
        f"tags: [google-ads, account-audit, {result.account_name.lower().replace(' ', '-')}]",
        f"date: {date.today().isoformat()}",
        f"account: {result.account_name}",
        f"scale_mode: {result.scale_mode}",
        f"health_score: {result.health_score:.0f}",
        "---",
        "",
        f"# {result.account_name} — Account Audit",
        f"**Date:** {date.today().strftime('%d %b %Y')}  ",
        f"**Health Score:** {result.health_score:.0f}/100 — {grade}  ",
        f"**Scale Mode:** {result.scale_mode}  ",
        f"**Findings:** {result.critical_count()} critical · {result.high_count()} high · "
        f"{len([f for f in result.findings if f.severity == 'MEDIUM'])} medium",
        "",
        "---",
        "",
        "## Account Overview",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Total spend (30d) | £{result.total_spend:,.0f} |",
        f"| Total conversions (30d) | {result.total_conversions:.0f} |",
        f"| Account CPA | {'£' + f'{result.total_spend / result.total_conversions:.2f}' if result.total_conversions else '—'} |",
        f"| Campaign count | {len(result.campaigns)} |",
        "",
        "### Campaign Type Breakdown",
        "",
        "| Type | Count |",
        "| --- | --- |",
        type_rows,
        "",
        "---",
        "",
        "## Issues Found",
        "",
        issues_md,
        "---",
        "",
        "## Campaign Performance (Top 30 by Spend)",
        "",
        camp_table,
        "",
        "---",
        "",
        "## Implementation Plan",
        "",
        result.ai_plan,
        "",
    ]

    content = "\n".join(lines)

    if dry_run:
        print(f"\n    [DRY RUN] Would write to: {filepath}")
        return filepath

    os.makedirs(folder, exist_ok=True)
    with open(filepath, "w") as fh:
        fh.write(content)

    return filepath


# ── Orchestrator ──────────────────────────────────────────────────────────────

class AuditOrchestrator:
    """
    Ties everything together for a single account:
    1. Batch-fetch all data
    2. Detect scale mode
    3. Run per-type auditors
    4. Score the account
    5. Generate AI plan
    6. Write Obsidian report
    """

    def __init__(self, ads_client: GoogleAdsClient):
        self.ads_client = ads_client
        self.scorer     = AccountScorer()

    def run(self, account_name: str, customer_id: str, dry_run: bool = False) -> AuditResult:
        print(f"\n  [{account_name}]")
        fetcher = BatchFetcher(self.ads_client, customer_id)

        # ── 1. Campaigns (always) ──────────────────────────────────────────────
        print("    Fetching campaigns...")
        campaigns = fetcher.fetch_campaigns()
        enabled   = [c for c in campaigns if c.status != "REMOVED"]

        if not enabled:
            print("    No enabled campaigns found — skipping.")
            return AuditResult(account_name=account_name, scale_mode="simple")

        scale_mode   = get_scale_mode(len(enabled))
        total_spend  = sum(c.cost for c in enabled)
        total_convs  = sum(c.conversions for c in enabled)
        type_summary = dict(
            sorted(
                {ct: sum(1 for c in enabled if c.channel_type == ct)
                 for ct in set(c.channel_type for c in enabled)}.items()
            )
        )

        print(f"    {len(enabled)} campaigns | Scale: {scale_mode} | "
              f"£{total_spend:,.0f} spend | {total_convs:.0f} conversions")
        print(f"    Types: {', '.join(f'{v}x{k}' for k, v in type_summary.items())}")

        result = AuditResult(
            account_name=account_name,
            scale_mode=scale_mode,
            campaigns=enabled,
            type_summary=type_summary,
            total_spend=total_spend,
            total_conversions=total_convs,
        )

        # ── 2. Supplemental data ───────────────────────────────────────────────
        has_search   = "SEARCH"          in type_summary
        has_pmax     = "PERFORMANCE_MAX" in type_summary
        has_shopping = "SHOPPING"        in type_summary

        asset_groups    = []
        keywords        = []
        waste_terms     = []
        campaign_assets = {}

        if has_pmax:
            print("    Fetching PMAX asset groups...")
            asset_groups = fetcher.fetch_asset_groups()
            print(f"    Asset groups: {len(asset_groups)}")

        if has_search:
            print("    Fetching keyword QS...")
            keywords = fetcher.fetch_keyword_qs(enterprise_mode=(scale_mode == "enterprise"))
            print(f"    Keywords (with QS): {len(keywords)}")

            print("    Fetching search term waste...")
            limit       = 100 if scale_mode == "enterprise" else 50
            waste_terms = fetcher.fetch_search_term_waste(limit=limit)

            print("    Fetching campaign assets...")
            campaign_assets = fetcher.fetch_campaign_assets()

        # ── 3. Run auditors ────────────────────────────────────────────────────
        print("    Running auditors...")

        if has_search:
            result.findings += SearchAuditor().run(
                campaigns, keywords, waste_terms, campaign_assets, scale_mode
            )

        if has_pmax:
            result.findings += PMaxAuditor().run(campaigns, asset_groups, scale_mode)

        if has_shopping:
            result.findings += ShoppingAuditor().run(campaigns, scale_mode)

        result.findings += DisplayAuditor().run(campaigns)
        result.findings += VideoAuditor().run(campaigns)

        # ── 4. Score ───────────────────────────────────────────────────────────
        result.health_score = self.scorer.score(result.findings)
        grade               = self.scorer.grade(result.health_score)
        print(f"    Health score: {result.health_score:.0f}/100 — {grade}")
        print(f"    Findings: {result.critical_count()} critical, {result.high_count()} high, "
              f"{len([f for f in result.findings if f.severity == 'MEDIUM'])} medium")

        # ── 5. AI plan ─────────────────────────────────────────────────────────
        print("    Generating AI implementation plan...")
        result.ai_plan = generate_ai_plan(result)

        # ── 6. Write report ────────────────────────────────────────────────────
        path = write_report(result, dry_run=dry_run)
        if not dry_run:
            print(f"    → {path}")

        # ── 7. Post to Monday.com ──────────────────────────────────────────────
        if result.findings and not dry_run:
            monday_findings = [f.to_monday_dict() for f in result.findings
                               if f.severity in ("CRITICAL", "HIGH", "MEDIUM")]
            if monday_findings:
                print("    Posting to Monday.com...")
                post_audit_issues(account_name, monday_findings)

        return result


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Campaign-type-aware Google Ads account audit")
    parser.add_argument("--account", help="Audit a single account by name")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to Obsidian or Monday.com")
    args = parser.parse_args()

    ads_client  = GoogleAdsClient.load_from_storage(YAML_FILE)
    orchestrator = AuditOrchestrator(ads_client)

    accounts = AUDIT_ACCOUNTS
    if args.account:
        if args.account not in accounts:
            print(f"[ERROR] Account '{args.account}' not found in AUDIT_ACCOUNTS.")
            print(f"Available: {', '.join(accounts.keys())}")
            sys.exit(1)
        accounts = {args.account: accounts[args.account]}

    print("\nGoogle Ads Account Audit — Campaign-Type-Aware")
    print("=" * 70)

    results = []
    for account_name, cfg in accounts.items():
        result = orchestrator.run(
            account_name=account_name,
            customer_id=cfg["ads_customer_id"],
            dry_run=args.dry_run,
        )
        results.append(result)

    # ── Final summary ──────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("AUDIT SUMMARY")
    print("=" * 70)
    scorer = AccountScorer()
    for r in results:
        grade = scorer.grade(r.health_score)
        print(f"  {r.account_name:<30} {r.health_score:>5.0f}/100  {grade}")
        print(f"    {r.critical_count()} critical  {r.high_count()} high  "
              f"{len([f for f in r.findings if f.severity == 'MEDIUM'])} medium")

    total_critical = sum(r.critical_count() for r in results)
    total_high     = sum(r.high_count()     for r in results)
    print(f"\n  {len(results)} account(s) audited. "
          f"{total_critical} critical issues, {total_high} high-priority items.")
    print("=" * 70)


if __name__ == "__main__":
    main()
