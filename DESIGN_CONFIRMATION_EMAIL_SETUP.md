# Design Confirmation Email (info@jrco.us)

When a customer places an order that includes product-builder line items, the backend sends a **separate design confirmation email** to the customer. This email includes their order details and the **design preview image** (hosted on Google Drive), so they have a record of the design even though Shopify’s order confirmation email cannot show it.

The email is sent **from** `info@jrco.us` (configurable) and uses your SMTP server.

---

## What the customer receives

- **From:** info@jrco.us (or the address you set)
- **Subject:** Your JR & Co. Order #&lt;number&gt; — Design Confirmation
- **Content:** Thank-you message, order number, date, customer name, product, quantity, **embedded design preview image**, production timeline, and a link to jrcogolf.com.

---

## Backend environment variables

Set these on your backend (e.g. Render) so the app can send the design confirmation email:

| Variable | Description |
|----------|-------------|
| `DESIGN_CONFIRMATION_FROM_EMAIL` | **From** address. Default: `info@jrco.us`. |
| `SMTP_HOST` | SMTP server hostname (e.g. `smtp.gmail.com`, `smtp.office365.com`, or your provider’s host). |
| `SMTP_PORT` | SMTP port. Default: `587` (TLS). Use `465` for SSL if your provider requires it. |
| `SMTP_USER` | SMTP username (often the full email, e.g. `info@jrco.us`). |
| `SMTP_PASSWORD` | SMTP password or app password for `SMTP_USER`. |

If `SMTP_HOST`, `SMTP_USER`, or `SMTP_PASSWORD` are not set, the design confirmation email is **skipped** (order processing and sheet writes still run; a log line will say SMTP not configured).

---

## Using info@jrco.us

To send **from** `info@jrco.us`:

1. **Use an SMTP provider that allows sending from that address.**  
   Examples:
   - **Google Workspace** for `@jrco.us`: use Gmail SMTP with an app password for `info@jrco.us`.
   - **Microsoft 365** for `@jrco.us`: use Office 365 SMTP for `info@jrco.us`.
   - **Your domain host** (e.g. cPanel, MX/email for jrco.us): use the SMTP host and credentials they provide for that mailbox.

2. **Set the env vars** (e.g. on Render):
   - `DESIGN_CONFIRMATION_FROM_EMAIL=info@jrco.us`
   - `SMTP_HOST=` your provider’s SMTP host
   - `SMTP_PORT=587` (or 465 if required)
   - `SMTP_USER=info@jrco.us`
   - `SMTP_PASSWORD=` the password or app password for that mailbox

3. **Security:** Prefer an **app password** or **API key** over the main account password. For Gmail/Google Workspace, enable 2FA and create an app password for “Mail.”

---

## Sending only your custom email (no Shopify order confirmation)

The backend **cannot** stop Shopify from sending its built-in order confirmation email. To have **only** your custom design confirmation for product-builder orders, use one of the options in **[SUPPRESS_SHOPIFY_ORDER_CONFIRMATION.md](./SUPPRESS_SHOPIFY_ORDER_CONFIRMATION.md)** (step-by-step: turn off in Settings, use Shopify Flow + app, or use an app that skips by product/tag).

---

## When the email is sent

The email is sent **once per order**, **after**:

- All product-builder line items are written to the Production Orders (PB) sheet (one or two rows per line item for front/back)
- Design preview images are uploaded to Google Drive

For orders with **multiple custom products**, the single email includes **all** design preview images and a product summary (e.g. “Driver Front, Driver Back, Blade”).

The recipient is the **customer email** on the Shopify order (or billing email). The design image in the email uses the **first** uploaded preview image (direct Google Drive view link).

---

## Troubleshooting

- **No email received:** Check that `SMTP_HOST`, `SMTP_USER`, and `SMTP_PASSWORD` are set and that the backend logs do not show “SMTP not configured” or “Failed to send email.”
- **Image not showing in email:** Some clients block external images. The link is a direct Google Drive view URL; ensure the Drive file is shared so “Anyone with the link” can view (the webhook already sets this when uploading).
- **From address rejected:** Confirm with your SMTP provider that you are allowed to send from `info@jrco.us` and that the account is verified.
