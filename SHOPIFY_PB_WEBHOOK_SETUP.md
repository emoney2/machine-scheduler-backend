# Shopify Product Builder Webhook Setup

When a customer places an order in Shopify that includes product-builder line items (custom Material/Fur, etc.), the backend creates one production order row per line item in:

1. **Google Sheets** – tab **`Production Orders`** by default (override with `PRODUCTION_ORDERS_PB_SHEET_TAB`)
2. **Supabase** – table **`Production Orders TEST`** by default (override with `SUPABASE_PB_ORDERS_TABLE`)

Main scheduler reads use **`ORDERS_RANGE`** (default **`Production Orders!A1:AP`**). Manual order entry via `/submit` is unchanged.

---

## 1. Shopify webhook

1. In **Shopify Admin** → **Settings** → **Notifications** → **Webhooks**.
2. Click **Create webhook**.
3. **Event:** Order creation.
4. **Format:** JSON.
5. **URL:** `https://YOUR_BACKEND_URL/api/shopify/webhook/orders/create`
6. Save and copy the **Webhook signing secret** (starts with `whsec_` or similar).

---

## 2. Backend environment variables

Set on your backend (e.g. Render):

| Variable | Description |
|----------|-------------|
| `SHOPIFY_WEBHOOK_SECRET` | Webhook signing secret from Shopify (required for HMAC verification). |
| `ORDERS_RANGE` | (Optional) A1 range for Production Orders reads (default `Production Orders!A1:AP`). |
| `PRODUCTION_ORDERS_PB_SHEET_TAB` | (Optional) Sheet tab for Shopify webhook rows. Default: **`Production Orders`**. |
| `SUPABASE_PB_ORDERS_TABLE` | (Optional) Supabase table for PB webhook rows. Default: **`Production Orders TEST`**. |

`SPREADSHEET_ID` and Supabase credentials are already used by the app.

---

## 3. Supabase table (default name: Production Orders TEST)

Create a table with at least these columns (names and types that match the insert):

| Column        | Type    | Notes                    |
|---------------|---------|--------------------------|
| Order #       | integer | Primary key or unique    |
| Date          | text    | e.g. `1/11/2025 14:30:00` |
| Company Name  | text    | From billing/Shopify     |
| Design        | text    | Order name or line title |
| Quantity      | integer |                          |
| Product       | text    | Line item title          |
| Price         | numeric |                          |
| Due Date      | text    | Often empty from webhook |
| Stage         | text    | e.g. `ORDERED`           |
| Fur Color     | text    | From cart property `Fur` |
| Material 1    | text    | From cart property `Material` |

You can add more columns (e.g. Notes, Print) if you want; the code currently sends the above. If you omit `Fur Color` or `Material 1`, remove those keys from the insert in `server.py` (search for `SUPABASE_PB_ORDERS_TABLE`).

---

## 4. Google Sheet tab

- Ensure the workbook has a tab named **`Production Orders`** (or the value of `PRODUCTION_ORDERS_PB_SHEET_TAB`). Row width through column **AP** matches the default `ORDERS_RANGE`.
- Row 1 should be headers matching your main Production Orders sheet (Order #, Date, Preview, Company Name, Design, Quantity, … Material 1, … Fur Color, …).
- New rows are appended; no formulas are written for preview/stage/ship date (those cells are left blank).

---

## 5. Field mapping (product builder → sheet/Supabase)

- Cart property **Material** → **Material 1** (and Supabase `Material 1`).
- Cart property **Fur** → **Fur Color** (and Supabase `Fur Color`).
- **Print** property (Yes/true) → Print column `YES` in the sheet.
- Company comes from billing address or “Shopify”.
- Design = order name or line item title; Product = line item title.

---

## 6. Testing

1. Place a test order in Shopify with the product builder (choose Material and Fur).
2. Check the **Production Orders** tab (or your `PRODUCTION_ORDERS_PB_SHEET_TAB`) for a new row.
3. Check Supabase table **Production Orders Shopify** for the same order.
4. Delete the test row(s) from the sheet and Supabase if you don’t want them in real data.

When you’re ready for live PB orders on the main sheet, set `PRODUCTION_ORDERS_PB_SHEET_TAB=Production Orders` (and ensure the main sheet has the same column layout).

---

## Troubleshooting

### 404 or 500 when Shopify sends the webhook

- **Redeploy the backend** (e.g. on Render: push your latest code and let the service redeploy, or trigger a manual deploy). The route `/api/shopify/webhook/orders/create` must be in the deployed code.
- The route is set to accept both `/api/shopify/webhook/orders/create` and `/api/shopify/webhook/orders/create/` (trailing slash). After redeploying, use **Send test** again or place a real order.
