/**
 * Shopify Customer Events Pixel — Example Store
 * Fires GA4 purchase + GA4 sign_up (newsletter opt-in) from Shopify checkout.
 *
 * Install: Shopify Admin → Settings → Customer events → Add custom pixel
 *
 * GA4 Measurement ID: G-XXXXXXXXXX
 * GTM Container:      GTM-YYYYYYY  (theme.liquid pages only — can't fire in checkout)
 */

// ── Purchase ──────────────────────────────────────────────────────────────────

analytics.subscribe('checkout_completed', (event) => {
  const order     = event.data.checkout;
  const lineItems = order.lineItems || [];

  const items = lineItems.map((li, idx) => ({
    item_id:      li.variant?.id || li.id,
    item_name:    li.title,
    item_variant: li.variant?.title || '',
    item_brand:   'Your Brand',
    price:        parseFloat(li.variant?.price?.amount || 0),
    quantity:     li.quantity,
    index:        idx,
  }));

  // GA4 purchase event
  gtag('event', 'purchase', {
    send_to:        'G-XXXXXXXXXX',
    transaction_id: order.order?.id || order.token,
    value:          parseFloat(order.totalPrice?.amount || 0),
    tax:            parseFloat(order.totalTax?.amount   || 0),
    shipping:       parseFloat(order.shippingLine?.price?.amount || 0),
    currency:       order.currencyCode || 'GBP',
    items,
  });
});

// ── Newsletter opt-in captured at checkout ────────────────────────────────────

analytics.subscribe('checkout_completed', (event) => {
  const order = event.data.checkout;
  if (order.buyerAcceptsEmailMarketing) {
    gtag('event', 'sign_up', {
      send_to: 'G-XXXXXXXXXX',
      method:  'checkout_email',
    });
  }
});
