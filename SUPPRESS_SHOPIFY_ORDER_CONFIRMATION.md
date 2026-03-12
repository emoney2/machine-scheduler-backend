# Suppress Shopify’s Default Order Confirmation for Product-Builder Orders

You want **only** your custom design confirmation email (from info@jrco.us) for orders that use the product builder, and **not** Shopify’s built-in order confirmation email.

Shopify does **not** let you turn off the order confirmation per product or per order type in the admin. You have to use one of the options below.

---

## Option 1: Turn off the default order confirmation (simplest)

If **all** your orders go through the product builder (or you’re okay with no Shopify confirmation for any order):

1. In **Shopify Admin** go to **Settings** → **Notifications**.
2. Under **Customer notifications**, find **Order confirmation**.
3. **Uncheck** or **disable** the order confirmation (exact wording depends on your Shopify version).
4. Save.

Result: Shopify will not send any order confirmation. Your backend will still send the custom design confirmation email for product-builder orders. Non–product-builder orders will not get an automatic confirmation unless you add another flow (e.g. Flow or app).

---

## Option 2: Use Shopify Flow (Shopify Plus or eligible plans)

If you have **Shopify Flow**:

1. Create a new flow.
2. **Trigger:** Order created.
3. **Condition:** Order has a line item with a property, e.g.:
   - Line item property `_productType` exists, or
   - Line item property `Material` exists.
4. **Action:** Use an action that **prevents** the default order confirmation from sending.  
   Flow does not have a built-in “do not send order confirmation” action, so you typically need an **app** that integrates with Flow and provides something like “Skip order confirmation” or “Don’t send default notification” when the flow runs.

So in practice, Option 2 usually means: **Flow + an app** that can suppress the confirmation when the flow triggers.

---

## Option 3: Use an app that can skip the confirmation

Use an app that can **conditionally** send or skip the order confirmation, for example:

- **Klaviyo** (or similar): Send your own “order confirmed” email from the app and turn off or replace Shopify’s in the app’s settings.
- **Order status / notification apps** that let you “disable default confirmation for orders matching X” (e.g. by tag, line item property, or product).

Steps are app-specific; install the app, then in its settings look for options like:

- “Replace Shopify order confirmation”
- “Don’t send Shopify confirmation when …”
- “Send custom confirmation only when …”

Trigger the condition on something only product-builder orders have (e.g. line item property `_productType` or `Material`, or a tag you add via Flow).

---

## Option 4: Tag at checkout and use an app (advanced)

Some apps let you “don’t send order confirmation if order has tag X”. In that case:

1. Add a tag to the order when it’s a product-builder order.  
   That usually requires:
   - **Shopify Flow** (Plus): “Order created” → condition “line item has property _productType” → action “Add tag …”, e.g. `product-builder`.
   - Or a **checkout script/app** that adds the tag when a line item has the product-builder property.
2. In the app, set: “Do not send default order confirmation when order has tag `product-builder`.”

Note: The default confirmation is often sent as soon as the order is created. So the tag must be added at or before order creation (e.g. by a checkout extension or by Flow running immediately on order creation). Your webhook runs after the order exists; by then the confirmation may already be sent, so tagging in the webhook is usually too late for suppressing that email.

---

## Summary

| Goal | Best approach |
|------|----------------|
| Only your custom email, and all orders are PB or you don’t need Shopify’s confirmation | **Option 1:** Turn off order confirmation in Settings → Notifications. |
| Only your custom email for PB orders, but keep Shopify’s confirmation for other orders | **Option 3:** App that skips/replaces confirmation when order has a PB line item (or tag). Option 2 (Flow + app) if you have Plus. |

Your backend **always** sends exactly one design confirmation email per order (with all design previews) for product-builder orders; it cannot stop Shopify from sending Shopify’s own email. Use one of the options above so that Shopify doesn’t send its confirmation for those orders.
