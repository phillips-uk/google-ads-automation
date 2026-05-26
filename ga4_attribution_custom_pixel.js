/**
 * Shopify Custom Pixel: GA4 Purchase via Measurement Protocol
 * ============================================================
 * Fixes 100% Direct attribution on Lee Renée Jewellery purchases.
 *
 * How to deploy:
 *   Shopify Admin → Settings → Customer events → Add custom pixel
 *   Name: "GA4 Purchase — Attribution Fix"
 *   Paste this entire file as the pixel code.
 *
 * Prerequisites (must be done BEFORE deploying this pixel):
 *   1. GTM tag "GA4 Session Context → Cart" deployed and published
 *      (ga4_attribution_gtm_tag.html) — this writes _ga4_client_id and
 *      _ga4_session_id to cart attributes on every storefront page.
 *   2. Google & YouTube app GA4 connection DISCONNECTED to avoid duplicate
 *      purchase events in GA4. Google Ads conversion tracking (Google Shopping
 *      App Purchase) is unaffected — do NOT disconnect that.
 *
 * What this does:
 *   Subscribes to checkout_completed. Reads client_id and session_id from
 *   cart customAttributes (written by the GTM tag). Fires a GA4 purchase
 *   event via the Measurement Protocol with full ecommerce data and correct
 *   session context, so GA4 attributes the conversion to the original
 *   acquisition channel instead of Direct.
 *
 * Client: Lee Renée Jewellery — leereneejewellery.myshopify.com
 * GA4 Measurement ID: G-BTVMM11L4Q
 * MP API Secret: akbQC-uFQuiRe_Wl4cexBw
 * Created: 2026-05-11
 */

const GA4_MEASUREMENT_ID = 'G-BTVMM11L4Q';
const GA4_API_SECRET = 'akbQC-uFQuiRe_Wl4cexBw';
const GA4_MP_ENDPOINT = `https://www.google-analytics.com/mp/collect?measurement_id=${GA4_MEASUREMENT_ID}&api_secret=${GA4_API_SECRET}`;

// ── Helper: extract value from customAttributes array ──────────────────────
function getAttr(attributes, key) {
  if (!Array.isArray(attributes)) return null;
  const attr = attributes.find(a => a.key === key);
  return attr ? attr.value : null;
}

// ── Helper: generate a fallback client_id if GTM tag didn't fire ───────────
// This covers edge cases (ad blockers, very fast checkout) — attribution will
// show as Direct but the event will still register.
function fallbackClientId() {
  return `${Math.floor(Math.random() * 2147483647)}.${Math.floor(Date.now() / 1000)}`;
}

// ── Subscribe to checkout_completed ────────────────────────────────────────
analytics.subscribe('checkout_completed', (event) => {
  try {
    const checkout = event.data.checkout;
    if (!checkout) return;

    // ── 1. Get session context from cart attributes (set by GTM tag) ────────
    const customAttributes = checkout.customAttributes || [];
    let clientId = getAttr(customAttributes, '_ga4_client_id');
    const sessionId = getAttr(customAttributes, '_ga4_session_id');

    if (!clientId) {
      // GTM tag didn't fire (e.g. ad blocker, empty cart at checkout start)
      // Use fallback — purchase will show as Direct but won't be lost
      clientId = fallbackClientId();
    }

    // ── 2. Build items array ────────────────────────────────────────────────
    const items = (checkout.lineItems || []).map((item, index) => {
      const itemObj = {
        item_id: item.variant?.sku || item.variant?.id || item.id,
        item_name: item.title,
        quantity: item.quantity,
        price: parseFloat(item.variant?.price?.amount || item.finalLinePrice?.amount || 0),
        index: index
      };
      if (item.variant?.product?.vendor) {
        itemObj.item_brand = item.variant.product.vendor;
      }
      if (item.variant?.product?.type) {
        itemObj.item_category = item.variant.product.type;
      }
      if (item.variant?.title && item.variant.title !== 'Default Title') {
        itemObj.item_variant = item.variant.title;
      }
      return itemObj;
    });

    // ── 3. Build the MP payload ─────────────────────────────────────────────
    const payload = {
      client_id: clientId,
      events: [{
        name: 'purchase',
        params: {
          transaction_id: checkout.order?.id || checkout.token,
          value: parseFloat(checkout.totalPrice?.amount || 0),
          currency: checkout.currencyCode || 'GBP',
          tax: parseFloat(checkout.totalTax?.amount || 0),
          shipping: parseFloat(checkout.shippingLine?.price?.amount || 0),
          coupon: (checkout.discountApplications || [])
            .filter(d => d.type === 'DISCOUNT_CODE')
            .map(d => d.title)
            .join(',') || undefined,
          items: items
        }
      }]
    };

    // Add session_id if available — allows GA4 to stitch to the session
    if (sessionId) {
      payload.events[0].params.session_id = sessionId;
      // GA4 engagement_time_msec is required for session stitching
      payload.events[0].params.engagement_time_msec = 1;
    }

    // ── 4. Fire the MP request ──────────────────────────────────────────────
    fetch(GA4_MP_ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).catch(() => {
      // Silently swallow — best effort
    });

  } catch (err) {
    // Never throw from a pixel — Shopify may disable it on repeated errors
  }
});
