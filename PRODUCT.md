# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

- **Primary:** the owner of The Blue Project, a Portuguese sole-trader company selling wholesale haircare, skincare and wellness brands through Amazon FBA (Amazon.es first). Not a programmer; runs the app locally by double-clicking `.command` files.
- **Secondary:** a partner or team member who also reads the panel to make the same business decisions.
- Both use it for a **daily check on the computer** and for **quick lookups on the phone**.

## Product Purpose

Seller Notes is a private control center for a small FBA wholesale business. It replaces checking Seller Central, Keepa-style tools and Sellerboard-style profit tools separately. All three jobs carry equal weight; no one page is "the" product:

1. **Defend the Buy Box:** who holds it on watched ASINs, at what price, and which sellers enter, leave or change price (polled every 5 minutes).
2. **Know whether the business really makes money:** real profit per SKU over any date range, built from Amazon's money movements plus the user's own product costs. Account-level costs (ads, subscription, storage, inbound shipping, removals) are shown separately, along with returns and refunds.
3. **Decide what to buy and from whom:** upload a supplier catalog (EAN + cost) and get BSR, offer count, whether Amazon sells it, Buy Box price, fees, profit per unit, ROI and margin.

Plus listing health: every SKU's status (active, visible but not buyable, inactive, suppressed), issues, price and stock.

Success means the user and partner see what changed and what needs action in one glance each day, without opening Seller Central.

## Positioning

It is built on the seller's own SP-API data and their own cost sheet. Profit counts sales on the order date, like Sellerboard. Unsettled orders get fees estimated from the SKU's history, and a refunded unit recovers its cost only if Amazon receives it back as sellable. It watches only the ASINs and SKUs this business cares about, not a generic market.

## Operating Context

- A local Flask app (`panel.py`, port 5050) reads the SQLite database that the monitor (`seller_notes.py run`) fills. The panel only shows what the monitor has stored, so the monitor must be running for Buy Box data to update.
- Pages (top navigation): **Dashboard** (listings), **Análisis Productos** (Buy Box), **Profit** (rentabilidad, plus Costes por SKU), **Catálogos** (supplier catalog analysis and its columns view).
- Daily reports and CSV exports go to `informes/`; catalog results export to Excel.
- Visual direction is designed in Google Stitch and ported into `templates/*.html` and `static/panel.css` by hand. Stitch mockups use fake numbers; the panel shows only real data.

## Capabilities and Constraints

- UI language is **Spanish**. Money is in euros, and catalog analysis is without VAT.
- Data limits that the UI must not hide or overstate: polling every 5 minutes misses price changes within that window; Amazon returns at most the top 20 new offers; FBA fees can be missing when product dimensions are missing, which inflates profit (flagged); a supplier brand that doesn't match Amazon's brand is flagged for manual review.
- Required SP-API roles: Pricing, Product Listing, Finance and Accounting, Order Tracking. The inventory ledger report currently returns 403.
- Light/dark theme is the user's choice through a header toggle, stored in localStorage. It does not follow `prefers-color-scheme`.
- **Open decision:** the panel binds to `127.0.0.1`, so the partner and the phone can't reach it yet. How it should be shared (LAN, tunnel, hosting) has not been decided.

## Brand Commitments

- Name: **Seller Notes**, for **The Blue Project**. Logo at `static/logo.svg`.
- Stitch is the visual source of truth for screens the user designed there. When porting, copy palette, fonts, shadows and buttons faithfully. Visual specifics belong in DESIGN.md, not here.

## Evidence on Hand

- Real operating data in `seller_notes.db`: Buy Box history, listings, finances, and catalog jobs.
- Supplier catalogs in `catalogos/` and daily reports in `informes/`.
- No testimonials, customers or public claims exist. This is an internal tool; don't invent them.

## Product Principles

1. **Real numbers only.** Never show placeholder or estimated figures without labeling them as estimates.
2. **What changed, and what to do.** Each view leads with what moved since the last look and what needs action.
3. **Equal citizens.** Buy Box, profit, sourcing and listing health are peers; none is buried.
4. **Honest about data gaps.** Surface API limits, missing fees and stale data rather than smoothing them over.
5. **Works for a non-programmer.** No terminal is needed for daily use; actions are buttons with plain Spanish labels.
