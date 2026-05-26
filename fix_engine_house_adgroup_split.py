"""
Engine House — Complete ad group split
Step 1: Create RSAs for Coworking Space (196217996146) + Office Rental (198241935762)
Step 2: Pause moved keywords in Office Space (190190485563)
"""
import warnings
warnings.filterwarnings("ignore")
from config import load_env
load_env()

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

CUSTOMER_ID   = "2563537401"
CAMPAIGN_ID   = "14700059864"

AG_COWORKING  = "196217996146"
AG_RENTAL     = "198241935762"
AG_OFFICE     = "190190485563"   # original "Office Space" group

client     = GoogleAdsClient.load_from_storage("google-ads.yaml")
ga_service = client.get_service("GoogleAdsService")
ad_service = client.get_service("AdService")
ag_ad_svc  = client.get_service("AdGroupAdService")

# ── RSA definitions ────────────────────────────────────────────────────────────

COWORKING_RSA = {
    "headlines": [
        "Coworking Space Bexley",
        "Flexible Hot Desks",
        "Dedicated Desk Rentals",
        "Modern Coworking Hub",
        "Join Our Creative Community",
        "Spaces Available Now",
        "From £500 Per Month",
        "All-Inclusive Amenities",
        "Flexible Coworking Terms",
        "Coworking in Bexley",
        "Hot Desks Available",
        "Creative Office Community",
        "Short Term Desk Rental",
        "Bexley Coworking Space",
        "Book a Desk Today",
    ],
    "descriptions": [
        "Modern coworking space in Bexley. Hot desks and dedicated desks. Flexible terms.",
        "Coworking memberships in Bexley. Creative community. All-inclusive amenities.",
        "Hot desks and dedicated desks in Bexley. Flexible monthly membership available.",
        "Join a thriving creative community in Bexley. Coworking space from £500/month.",
    ],
}

RENTAL_RSA = {
    "headlines": [
        "Office Space for Rent Bexley",
        "Private Office Rentals",
        "Rent an Office in Bexley",
        "Flexible Office Leases",
        "Spaces Available Now",
        "From £500 Per Month",
        "Short Term Office Rentals",
        "All-Inclusive Office Space",
        "Bexley Office for Rent",
        "Serviced Offices Bexley",
        "Private Office Available",
        "Flexible Lease Terms",
        "Managed Office Space",
        "Office Rental Bexley",
        "Book a Viewing Today",
    ],
    "descriptions": [
        "Private offices for rent in Bexley. Flexible lease terms. All bills included.",
        "Serviced office rentals in Bexley. Short and long term leases available.",
        "Rent a private office in Bexley from £500/month. All-inclusive, flexible terms.",
        "Managed office space in Bexley. Flexible rentals with no long-term commitment.",
    ],
}


def create_rsa(ag_id: str, rsa_config: dict, label: str) -> None:
    """Create an RSA in the given ad group."""
    print(f"\n--- Creating RSA: {label} (AG: {ag_id}) ---")

    ag_resource = f"customers/{CUSTOMER_ID}/adGroups/{ag_id}"

    # Build ad
    ad_op = client.get_type("AdGroupAdOperation")
    aga   = ad_op.create
    aga.ad_group = ag_resource
    aga.status   = client.enums.AdGroupAdStatusEnum.ENABLED

    rsa = aga.ad.responsive_search_ad
    for h in rsa_config["headlines"]:
        asset = client.get_type("AdTextAsset")
        asset.text = h
        rsa.headlines.append(asset)
    for d in rsa_config["descriptions"]:
        if len(d) > 90:
            print(f"  ⚠️  Description too long ({len(d)} chars): {d!r}")
            continue
        asset = client.get_type("AdTextAsset")
        asset.text = d
        rsa.descriptions.append(asset)

    aga.ad.final_urls.append("https://enginehousebexley.co.uk/")

    try:
        resp = ag_ad_svc.mutate_ad_group_ads(
            customer_id=CUSTOMER_ID, operations=[ad_op]
        )
        rn = resp.results[0].resource_name
        print(f"  ✅ RSA created: {rn}")
    except GoogleAdsException as ex:
        print(f"  ❌ Error creating RSA:")
        for e in ex.failure.errors:
            print(f"    [{e.error_code}] {e.message}")
            if e.location:
                for fp in e.location.field_path_elements:
                    print(f"      field: {fp.field_name} idx: {fp.index}")


# ── Keyword IDs to pause in Office Space ──────────────────────────────────────

MOVED_TO_COWORKING = [
    6827596575, 10512015097, 141491995013, 297280537862, 368434259947,
    464677636056, 297280537822, 10512014017, 32775528402, 555928725295,
    626947826091, 2384597110319, 2384581238799, 404245858226,
    1597377611, 496123938, 304752166228,
]

MOVED_TO_RENTAL = [
    40715250, 252163541, 296972043530, 1012846361472, 80436567891,
    5457871119, 464677636976, 4687845869, 863870678547, 7162254374,
    310423043732, 313427883772, 944968501633, 812104974169, 835122680854,
    16559611087, 147699309173,
]

IRRELEVANT = [
    1298115756, 365548048680, 2384581239039, 2969832188,
]

ALL_TO_PAUSE = MOVED_TO_COWORKING + MOVED_TO_RENTAL + IRRELEVANT


def pause_keywords(criterion_ids: list, ag_id: str) -> None:
    """Pause keywords by criterion ID using correct resource name format."""
    print(f"\n--- Pausing {len(criterion_ids)} keywords in AG {ag_id} ---")

    ag_criterion_svc = client.get_service("AdGroupCriterionService")
    ops = []
    for cid in criterion_ids:
        op   = client.get_type("AdGroupCriterionOperation")
        agc  = op.update
        # Correct format: customers/{CID}/adGroupCriteria/{AG_ID}~{criterion_id}
        agc.resource_name = f"customers/{CUSTOMER_ID}/adGroupCriteria/{ag_id}~{cid}"
        agc.status        = client.enums.AdGroupCriterionStatusEnum.PAUSED
        op.update_mask.paths.append("status")
        ops.append(op)

    # Send in batches of 100
    batch_size = 100
    for i in range(0, len(ops), batch_size):
        batch = ops[i:i + batch_size]
        try:
            resp = ag_criterion_svc.mutate_ad_group_criteria(
                customer_id=CUSTOMER_ID, operations=batch
            )
            for r in resp.results:
                print(f"  ✅ Paused: {r.resource_name}")
        except GoogleAdsException as ex:
            print(f"  ❌ Error pausing batch:")
            for e in ex.failure.errors:
                print(f"    [{e.error_code}] {e.message}")
                if e.location:
                    for fp in e.location.field_path_elements:
                        print(f"      field: {fp.field_name} idx: {fp.index}")


# ── Verification ───────────────────────────────────────────────────────────────

def verify_keyword_counts():
    print("\n── Verification: keyword counts per ad group")
    resp = ga_service.search(customer_id=CUSTOMER_ID, query=f"""
        SELECT ad_group.name, ad_group.id,
               metrics.impressions
        FROM   ad_group_criterion
        WHERE  campaign.id = {CAMPAIGN_ID}
          AND  ad_group_criterion.type = KEYWORD
          AND  ad_group_criterion.status = ENABLED
    """)
    counts = {}
    for row in resp:
        name = row.ad_group.name
        counts[name] = counts.get(name, 0) + 1
    for name, cnt in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {cnt:>3} active keywords  {name}")


def verify_ads():
    print("\n── Verification: active ads per ad group")
    resp = ga_service.search(customer_id=CUSTOMER_ID, query=f"""
        SELECT ad_group.name, ad_group.id,
               ad_group_ad.status, ad_group_ad.ad.type
        FROM   ad_group_ad
        WHERE  campaign.id = {CAMPAIGN_ID}
          AND  ad_group_ad.status != REMOVED
    """)
    ads = {}
    for row in resp:
        name = row.ad_group.name
        ads[name] = ads.get(name, 0) + 1
    for name, cnt in sorted(ads.items()):
        print(f"  {cnt} ad(s)  {name}")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Step 1 — Create RSA: Coworking Space")
    create_rsa(AG_COWORKING, COWORKING_RSA, "Coworking Space")

    print("\nStep 2 — Create RSA: Office Rental")
    create_rsa(AG_RENTAL, RENTAL_RSA, "Office Rental")

    print("\nStep 3 — Pause moved/irrelevant keywords in Office Space")
    pause_keywords(ALL_TO_PAUSE, AG_OFFICE)

    verify_keyword_counts()
    verify_ads()

    print("\n✅ Done")
