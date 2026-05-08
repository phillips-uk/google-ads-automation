"""
GTM Setup — Example Store
Creates a GTM container under the agency account and builds:
  - DLV variables (ecommerce.items / .value / .currency / .transaction_id)
  - Custom event triggers (view_item, add_to_cart, begin_checkout, sign_up)
  - GA4 Configuration tag (All Pages)
  - GA4 Event tags for all ecommerce + sign_up events
Prints the GTM snippet for Shopify theme.liquid and a Customer Events pixel
for purchase tracking (checkout is a separate Shopify domain — GTM can't fire there).
"""

import json
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from tracking_audit import build_gtm_service

# ── Config ─────────────────────────────────────────────────────────────────────
GTM_ACCOUNT_ID     = "0000000000"
ACCOUNT_PATH       = f"accounts/{GTM_ACCOUNT_ID}"
CONTAINER_NAME     = "Example Store"
GA4_MEASUREMENT_ID = "G-XXXXXXXXXX"

# Shopify domain used for Customer Events pixel context
SHOPIFY_STORE      = "yourstore.myshopify.com"

# Built-in GTM trigger
ALL_PAGES_TRIGGER_ID = "2147479553"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _ok(label, obj=None):
    detail = f"  ({obj.get('publicId') or obj.get('triggerId') or obj.get('tagId') or obj.get('variableId') or ''})" if obj else ""
    print(f"  ✅  {label}{detail}")


def create_variable(svc, ws_path, name, var_type, parameters):
    body = {"name": name, "type": var_type, "parameter": parameters}
    v = svc.accounts().containers().workspaces().variables().create(
        parent=ws_path, body=body
    ).execute()
    _ok(f"Variable: {name}", v)
    return v


def create_trigger(svc, ws_path, name, event_name):
    body = {
        "name": name,
        "type": "CUSTOM_EVENT",
        "customEventFilter": [{
            "type": "equals",
            "parameter": [
                {"type": "template", "key": "arg0", "value": "{{_event}}"},
                {"type": "template", "key": "arg1", "value": event_name},
            ],
        }],
    }
    t = svc.accounts().containers().workspaces().triggers().create(
        parent=ws_path, body=body
    ).execute()
    _ok(f"Trigger: {name}", t)
    return t


def create_tag(svc, ws_path, name, tag_type, firing_trigger_ids, parameters):
    body = {
        "name":             name,
        "type":             tag_type,
        "firingTriggerId":  firing_trigger_ids,
        "parameter":        parameters,
    }
    tag = svc.accounts().containers().workspaces().tags().create(
        parent=ws_path, body=body
    ).execute()
    _ok(f"Tag: {name}", tag)
    return tag


def _dlv(path):
    """Build a dataLayer Variable parameter list for the given key path."""
    return [
        {"type": "integer",  "key": "dataLayerVersion", "value": "2"},
        {"type": "boolean",  "key": "setDefaultValue",  "value": "false"},
        {"type": "template", "key": "name",              "value": path},
    ]


def _event_params(pairs):
    """Build the gaawe eventParameters list from [(name, value), ...]."""
    return {
        "type": "list",
        "key":  "eventParameters",
        "list": [
            {
                "type": "map",
                "map": [
                    {"type": "template", "key": "name",  "value": n},
                    {"type": "template", "key": "value", "value": v},
                ],
            }
            for n, v in pairs
        ],
    }


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    svc = build_gtm_service()

    # 1 — Create container
    print(f"\n{'='*60}")
    print(f"  GTM Setup — {CONTAINER_NAME}")
    print(f"{'='*60}\n")

    print("Creating container...")
    container = svc.accounts().containers().create(
        parent=ACCOUNT_PATH,
        body={
            "name":          CONTAINER_NAME,
            "usageContext":  ["web"],
            "notes":         "Shopify store — Example Store. GA4 + Google Ads tracking.",
        },
    ).execute()
    container_id = container["containerId"]
    container_path = container["path"]
    public_id = container["publicId"]
    _ok(f"Container: {CONTAINER_NAME}", container)
    print(f"  Public ID: {public_id}")
    print(f"  Container path: {container_path}\n")

    # 2 — Create default workspace
    ws = svc.accounts().containers().workspaces().create(
        parent=container_path,
        body={"name": "GA4 + Ecommerce Setup", "description": "Initial GA4 configuration and ecommerce event tags"},
    ).execute()
    ws_path = ws["path"]
    _ok(f"Workspace: {ws['name']}", ws)
    print()

    # 3 — Variables
    print("Creating variables...")
    v_items   = create_variable(svc, ws_path, "DLV - ecommerce.items",          "v", _dlv("ecommerce.items"))
    v_value   = create_variable(svc, ws_path, "DLV - ecommerce.value",          "v", _dlv("ecommerce.value"))
    v_curr    = create_variable(svc, ws_path, "DLV - ecommerce.currency",       "v", _dlv("ecommerce.currency"))
    v_txn     = create_variable(svc, ws_path, "DLV - ecommerce.transaction_id", "v", _dlv("ecommerce.transaction_id"))
    v_ga4_id  = create_variable(svc, ws_path, "CONST - GA4 Measurement ID",     "c",
                                [{"type": "template", "key": "value", "value": GA4_MEASUREMENT_ID}])
    print()

    var = lambda n: f"{{{{{n}}}}}"   # {{var_name}} helper

    # 4 — Triggers
    print("Creating triggers...")
    t_view      = create_trigger(svc, ws_path, "CE - view_item",       "view_item")
    t_cart      = create_trigger(svc, ws_path, "CE - add_to_cart",     "add_to_cart")
    t_checkout  = create_trigger(svc, ws_path, "CE - begin_checkout",  "begin_checkout")
    t_signup    = create_trigger(svc, ws_path, "CE - sign_up",         "sign_up")
    print()

    # 5 — Tags
    print("Creating tags...")

    # GA4 Configuration — All Pages
    create_tag(svc, ws_path,
        name="GA4 - Configuration",
        tag_type="gaawc",
        firing_trigger_ids=[ALL_PAGES_TRIGGER_ID],
        parameters=[
            {"type": "template", "key": "measurementId", "value": var("CONST - GA4 Measurement ID")},
            {"type": "boolean",  "key": "sendPageView",  "value": "true"},
        ],
    )

    # GA4 - view_item
    create_tag(svc, ws_path,
        name="GA4 - view_item",
        tag_type="gaawe",
        firing_trigger_ids=[t_view["triggerId"]],
        parameters=[
            {"type": "template", "key": "eventName", "value": "view_item"},
            _event_params([
                ("currency", var("DLV - ecommerce.currency")),
                ("value",    var("DLV - ecommerce.value")),
                ("items",    var("DLV - ecommerce.items")),
            ]),
        ],
    )

    # GA4 - add_to_cart
    create_tag(svc, ws_path,
        name="GA4 - add_to_cart",
        tag_type="gaawe",
        firing_trigger_ids=[t_cart["triggerId"]],
        parameters=[
            {"type": "template", "key": "eventName", "value": "add_to_cart"},
            _event_params([
                ("currency", var("DLV - ecommerce.currency")),
                ("value",    var("DLV - ecommerce.value")),
                ("items",    var("DLV - ecommerce.items")),
            ]),
        ],
    )

    # GA4 - begin_checkout
    create_tag(svc, ws_path,
        name="GA4 - begin_checkout",
        tag_type="gaawe",
        firing_trigger_ids=[t_checkout["triggerId"]],
        parameters=[
            {"type": "template", "key": "eventName", "value": "begin_checkout"},
            _event_params([
                ("currency", var("DLV - ecommerce.currency")),
                ("value",    var("DLV - ecommerce.value")),
                ("items",    var("DLV - ecommerce.items")),
            ]),
        ],
    )

    # GA4 - sign_up (newsletter)
    create_tag(svc, ws_path,
        name="GA4 - sign_up",
        tag_type="gaawe",
        firing_trigger_ids=[t_signup["triggerId"]],
        parameters=[
            {"type": "template", "key": "eventName", "value": "sign_up"},
            _event_params([("method", "newsletter")]),
        ],
    )

    print()
    print("="*60)
    print(f"  ✅  GTM container ready: {public_id}")
    print(f"      Review workspace in GTM UI before publishing.")
    print("="*60)

    # ── Print install instructions ─────────────────────────────────────────────
    print(f"""
{'='*60}
  SHOPIFY INSTALL INSTRUCTIONS
{'='*60}

─── 1. GTM snippet → Shopify theme.liquid ───────────────────

Add the <script> block immediately after <head>:

<!-- Google Tag Manager -->
<script>(function(w,d,s,l,i){{w[l]=w[l]||[];w[l].push({{'gtm.start':
new Date().getTime(),event:'gtm.js'}})}})(window,document,'script','dataLayer','{public_id}');</script>
<!-- End Google Tag Manager -->

Add the <noscript> block immediately after <body>:

<!-- Google Tag Manager (noscript) -->
<noscript><iframe src="https://www.googletagmanager.com/ns.html?id={public_id}"
height="0" width="0" style="display:none;visibility:hidden"></iframe></noscript>
<!-- End Google Tag Manager (noscript) -->


─── 2. dataLayer pushes → theme.liquid (before GTM snippet) ──

Add product-page push inside the {{%- if template == 'product' -%}} block:

<script>
  window.dataLayer = window.dataLayer || [];
  dataLayer.push({{ ecommerce: null }});
  dataLayer.push({{
    event: 'view_item',
    ecommerce: {{
      currency: '{{{{ product.selected_or_first_available_variant.price | money_without_currency | replace: ',', '' }}}}' === '' ? 'GBP' : 'GBP',
      value: {{{{ product.selected_or_first_available_variant.price | divided_by: 100.0 }}}},
      items: [{{
        item_id:   '{{{{ product.selected_or_first_available_variant.id }}}}',
        item_name: {{{{ product.title | json }}}},
        item_brand:'Your Brand',
        price:      {{{{ product.selected_or_first_available_variant.price | divided_by: 100.0 }}}},
        quantity:   1
      }}]
    }}
  }});
</script>

For add_to_cart: fire on cart/add AJAX response — use theme's cart JS hook or
Shopify's product form submit event. Push same structure with event:'add_to_cart'.

For begin_checkout: fire when user clicks checkout button.
  dataLayer.push({{ event: 'begin_checkout', ecommerce: {{ ... }} }});

For sign_up: fire on newsletter form submit success.
  dataLayer.push({{ event: 'sign_up' }});


─── 3. Purchase — Shopify Customer Events pixel ──────────────

Checkout runs on Shopify's domain (can't use GTM). Use a Customer Events pixel:
Shopify Admin → Settings → Customer events → Add custom pixel

Pixel name: GA4 + Google Ads Purchase

--- PIXEL CODE (see gtm_shopify_purchase_pixel.js) ---


─── 4. Update tracking_audit.py ──────────────────────────────

Add Example Store to AUDIT_ACCOUNTS:
  "gtm_container_public_id": "{public_id}",
  "ads_customer_id":         "YOUR_ADS_CUSTOMER_ID",

""")

    # Write the Customer Events purchase pixel to a file
    pixel_path = os.path.join(os.path.dirname(__file__), "gtm_shopify_purchase_pixel.js")
    pixel_code = f"""/**
 * Shopify Customer Events Pixel — Example Store
 * Fires GA4 purchase + GA4 sign_up (newsletter opt-in) from checkout.
 * Install: Shopify Admin → Settings → Customer events → Add custom pixel
 *
 * GA4 Measurement ID: {GA4_MEASUREMENT_ID}
 */

// ── Purchase ──────────────────────────────────────────────────────────────────

analytics.subscribe('checkout_completed', (event) => {{
  const order      = event.data.checkout;
  const lineItems  = order.lineItems || [];

  const items = lineItems.map((li, idx) => ({{
    item_id:       li.variant?.id          || li.id,
    item_name:     li.title,
    item_variant:  li.variant?.title       || '',
    item_brand:    'Your Brand',
    price:         parseFloat(li.variant?.price?.amount || li.finalLinePrice?.amount / li.quantity) || 0,
    quantity:      li.quantity,
    index:         idx,
  }}));

  // GA4 purchase
  gtag('event', 'purchase', {{
    transaction_id: order.order?.id || order.token,
    value:          parseFloat(order.totalPrice?.amount || 0),
    tax:            parseFloat(order.totalTax?.amount   || 0),
    shipping:       parseFloat(order.shippingLine?.price?.amount || 0),
    currency:       order.currencyCode || 'GBP',
    items,
  }});
}});

// ── Newsletter opt-in at checkout ─────────────────────────────────────────────

analytics.subscribe('checkout_completed', (event) => {{
  const order = event.data.checkout;
  if (order.buyerAcceptsEmailMarketing) {{
    gtag('event', 'sign_up', {{ method: 'checkout_email' }});
  }}
}});
"""
    with open(pixel_path, "w") as f:
        f.write(pixel_code)
    print(f"  Purchase pixel written → {pixel_path}\n")

    return public_id, container_id


if __name__ == "__main__":
    main()
