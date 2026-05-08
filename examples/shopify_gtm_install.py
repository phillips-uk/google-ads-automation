"""
Shopify GTM Install — Example Store
1. Patches theme.liquid: GTM <script> after <head>, <noscript> after <body>
2. Adds dataLayer pushes for view_item (product page), sign_up (newsletter form)
3. Installs the Customer Events web pixel for purchase + checkout sign_up

Does nothing if each change is already present (idempotent).
"""

import os
import re
import sys
import json
import requests

sys.path.insert(0, os.path.dirname(__file__))
from config import load_env

load_env()

SHOP          = "yourstore.myshopify.com"
ACCESS_TOKEN  = os.environ["SHOPIFY_ACCESS_TOKEN"]
API_VERSION   = "2024-01"
BASE_URL      = f"https://{SHOP}/admin/api/{API_VERSION}"
HEADERS       = {
    "X-Shopify-Access-Token": ACCESS_TOKEN,
    "Content-Type": "application/json",
}

GTM_ID = "GTM-YYYYYYY"
GA4_ID = "G-XXXXXXXXXX"


# ── Helpers ────────────────────────────────────────────────────────────────────

def get(path, **params):
    r = requests.get(f"{BASE_URL}{path}", headers=HEADERS, params=params)
    r.raise_for_status()
    return r.json()


def put(path, body):
    r = requests.put(f"{BASE_URL}{path}", headers=HEADERS, json=body)
    r.raise_for_status()
    return r.json()


def post(path, body):
    r = requests.post(f"{BASE_URL}{path}", headers=HEADERS, json=body)
    r.raise_for_status()
    return r.json()


def gql(query, variables=None):
    r = requests.post(
        f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json",
        headers={**HEADERS, "Content-Type": "application/json"},
        json={"query": query, "variables": variables or {}},
    )
    r.raise_for_status()
    return r.json()


# ── 1. theme.liquid patch ──────────────────────────────────────────────────────

GTM_HEAD = f"""<!-- Google Tag Manager -->
<script>(function(w,d,s,l,i){{w[l]=w[l]||[];w[l].push({{'gtm.start':
new Date().getTime(),event:'gtm.js'}});var f=d.getElementsByTagName(s)[0],
j=d.createElement(s),dl=l!='dataLayer'?'&l='+l:'';j.async=true;j.src=
'https://www.googletagmanager.com/gtm.js?id='+i+dl;f.parentNode.insertBefore(j,f);
}})(window,document,'script','dataLayer','{GTM_ID}');</script>
<!-- End Google Tag Manager -->"""

GTM_BODY = f"""<!-- Google Tag Manager (noscript) -->
<noscript><iframe src="https://www.googletagmanager.com/ns.html?id={GTM_ID}"
height="0" width="0" style="display:none;visibility:hidden"></iframe></noscript>
<!-- End Google Tag Manager (noscript) -->"""

# dataLayer pushes — injected before GTM snippet, inside <head>
DATALAYER_PUSHES = """<!-- GA4 dataLayer — ecommerce events -->
<script>
window.dataLayer = window.dataLayer || [];

// ── Product page: view_item ──────────────────────────────────
{% if template == 'product' %}
  {% assign variant = product.selected_or_first_available_variant %}
  dataLayer.push({ ecommerce: null });
  dataLayer.push({
    event: 'view_item',
    ecommerce: {
      currency: {{ shop.currency | json }},
      value: {{ variant.price | divided_by: 100.0 }},
      items: [{
        item_id:      {{ variant.id | json }},
        item_name:    {{ product.title | json }},
        item_variant: {{ variant.title | json }},
        item_brand:   'Your Brand',
        item_category:{{ product.type | json }},
        price:        {{ variant.price | divided_by: 100.0 }},
        quantity:     1
      }]
    }
  });
{% endif %}

// ── Cart page: begin_checkout on checkout button click ───────
{% if template == 'cart' %}
  document.addEventListener('DOMContentLoaded', function() {
    var btn = document.querySelector('[name="checkout"], [href="/checkout"], .cart__checkout-button');
    if (btn) {
      btn.addEventListener('click', function() {
        var items = [];
        {% for item in cart.items %}
          items.push({
            item_id:      {{ item.variant_id | json }},
            item_name:    {{ item.product.title | json }},
            item_variant: {{ item.variant.title | json }},
            item_brand:   'Your Brand',
            price:        {{ item.price | divided_by: 100.0 }},
            quantity:     {{ item.quantity }}
          });
        {% endfor %}
        dataLayer.push({ ecommerce: null });
        dataLayer.push({
          event: 'begin_checkout',
          ecommerce: {
            currency: {{ shop.currency | json }},
            value: {{ cart.total_price | divided_by: 100.0 }},
            items: items
          }
        });
      });
    }
  });
{% endif %}
</script>
<!-- End GA4 dataLayer -->"""

# Newsletter sign_up push — injected at end of </body>
SIGNUP_PUSH = """<!-- GA4 sign_up — newsletter form -->
<script>
document.addEventListener('DOMContentLoaded', function() {
  // Catches Klaviyo, Shopify native newsletter forms, and common theme patterns
  var forms = document.querySelectorAll(
    'form[action*="/contact#newsletter"], form[action*="/contact"][id*="newsletter"], ' +
    'form.newsletter-form, form[id*="newsletter"], .klaviyo-form form'
  );
  forms.forEach(function(form) {
    form.addEventListener('submit', function() {
      dataLayer.push({ event: 'sign_up' });
    });
  });
});
</script>
<!-- End GA4 sign_up -->"""


def patch_theme_liquid():
    print("\n── 1. Patching theme.liquid ─────────────────────────────────")

    # Find published theme
    themes = get("/themes.json")["themes"]
    pub = next((t for t in themes if t["role"] == "main"), None)
    if not pub:
        print("  ❌  No published theme found")
        return False
    print(f"  Theme: {pub['name']}  (id={pub['id']})")

    # Fetch theme.liquid
    asset = get(f"/themes/{pub['id']}/assets.json", **{"asset[key]": "layout/theme.liquid"})["asset"]
    src = asset["value"]
    orig_len = len(src)
    changed = False

    # Check already patched
    if GTM_ID in src:
        print(f"  ℹ️   GTM snippet ({GTM_ID}) already present — skipping head/body injection")
    else:
        # dataLayer pushes — insert before </head>
        if "window.dataLayer" not in src:
            src = src.replace("</head>", DATALAYER_PUSHES + "\n</head>", 1)
            print("  ✅  Injected dataLayer pushes (view_item, begin_checkout)")
        else:
            print("  ℹ️   dataLayer already initialised — skipping push injection")

        # GTM <script> — insert after <head>
        src = re.sub(r'(<head[^>]*>)', r'\1\n' + GTM_HEAD, src, count=1)
        print("  ✅  Injected GTM <script> after <head>")

        # GTM <noscript> — insert after <body>
        src = re.sub(r'(<body[^>]*>)', r'\1\n' + GTM_BODY, src, count=1)
        print("  ✅  Injected GTM <noscript> after <body>")

        changed = True

    # Newsletter sign_up push
    if "GA4 sign_up" in src:
        print("  ℹ️   sign_up push already present — skipping")
    else:
        src = src.replace("</body>", SIGNUP_PUSH + "\n</body>", 1)
        print("  ✅  Injected newsletter sign_up push before </body>")
        changed = True

    if not changed:
        print("  ℹ️   theme.liquid already up to date — no changes written")
        return True

    # Write back
    put(f"/themes/{pub['id']}/assets.json", {
        "asset": {"key": "layout/theme.liquid", "value": src}
    })
    print(f"  ✅  theme.liquid saved ({orig_len} → {len(src)} chars)")
    return True


# ── 2. Customer Events pixel ───────────────────────────────────────────────────

PIXEL_NAME = "GA4 + Purchase Tracking"

PIXEL_CODE = f"""/**
 * Example Store — Shopify Customer Events Pixel
 * Fires GA4 purchase + newsletter sign_up from Shopify checkout.
 * GA4 Measurement ID: {GA4_ID}
 */

analytics.subscribe('checkout_completed', (event) => {{
  const order     = event.data.checkout;
  const lineItems = order.lineItems || [];

  const items = lineItems.map((li, idx) => ({{
    item_id:      String(li.variant?.id || li.id || ''),
    item_name:    li.title || '',
    item_variant: li.variant?.title || '',
    item_brand:   'Your Brand',
    price:        parseFloat(li.variant?.price?.amount || 0),
    quantity:     li.quantity || 1,
    index:        idx,
  }}));

  gtag('event', 'purchase', {{
    send_to:        '{GA4_ID}',
    transaction_id: String(order.order?.id || order.token || ''),
    value:          parseFloat(order.totalPrice?.amount || 0),
    tax:            parseFloat(order.totalTax?.amount   || 0),
    shipping:       parseFloat(order.shippingLine?.price?.amount || 0),
    currency:       order.currencyCode || 'GBP',
    items,
  }});

  // Newsletter opt-in captured at checkout
  if (order.buyerAcceptsEmailMarketing) {{
    gtag('event', 'sign_up', {{
      send_to: '{GA4_ID}',
      method:  'checkout_email',
    }});
  }}
}});
"""


def install_pixel():
    print("\n── 2. Installing Customer Events pixel ─────────────────────")

    # Check if pixel already exists (GraphQL — REST doesn't have a list endpoint)
    result = gql("""
    {
      webPixels(first: 20) {
        edges {
          node { id title }
        }
      }
    }
    """)
    existing = result.get("data", {}).get("webPixels", {}).get("edges", [])
    for edge in existing:
        if edge["node"]["title"] == PIXEL_NAME:
            print(f"  ℹ️   Pixel '{PIXEL_NAME}' already exists — skipping")
            return True

    # Create via GraphQL
    mutation = """
    mutation webPixelCreate($webPixel: WebPixelInput!) {
      webPixelCreate(webPixel: $webPixel) {
        webPixel { id title }
        userErrors { field message }
      }
    }
    """
    variables = {
        "webPixel": {
            "title":      PIXEL_NAME,
            "sourceCode": PIXEL_CODE,
        }
    }
    result = gql(mutation, variables)
    errors = result.get("data", {}).get("webPixelCreate", {}).get("userErrors", [])
    if errors:
        for e in errors:
            print(f"  ❌  {e['field']}: {e['message']}")
        return False

    pixel = result["data"]["webPixelCreate"]["webPixel"]
    print(f"  ✅  Pixel created: '{pixel['title']}'  id={pixel['id']}")
    return True


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("="*60)
    print("  Shopify GTM Install — Example Store")
    print("="*60)

    ok1 = patch_theme_liquid()
    ok2 = install_pixel()

    print("\n" + "="*60)
    if ok1 and ok2:
        print("  ✅  All done.")
        print()
        print("  Next steps:")
        print("  1. Publish the GTM workspace (GTM UI → Submit → Publish)")
        print("  2. Test: open your store in Tag Assistant → browse a")
        print("     product page → check view_item fires in the GA4 tag")
        print("  3. Test purchase pixel: place a test order (or use Shopify")
        print("     Order → More actions → View order status page) and check")
        print("     GA4 Realtime → Events for 'purchase'")
        print("  4. Run /tracking-audit to verify GTM-YYYYYYY is wired up")
    else:
        print("  ⚠️   Some steps failed — check errors above")
    print("="*60)


if __name__ == "__main__":
    main()
