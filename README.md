# Seller Notes

**A private control center for a small Amazon FBA business (Amazon.es), built on Amazon's Selling Partner API.**

Seller Notes replaces checking Seller Central, Keepa-style and Sellerboard-style tools separately. It has three jobs:

1. **Defend the Buy Box**: polls watched ASINs every 5 minutes and records who holds the Buy Box, at what price, which sellers enter or leave, how often prices change, and each seller's share of the day.
2. **Know the real profit**: profit, margin and ROI per SKU for any date range, built from Amazon's money movements (sales, fees, FBA fees, refunds, returns) plus the seller's own product costs (with date-effective cost history).
3. **Decide what to buy**: upload a supplier catalog (EAN + cost) and get the ASIN, BSR, number of offers, whether Amazon itself sells it, Buy Box price, Amazon + FBA fees, profit per unit, ROI and margin, exportable to Excel.

It also tracks **listing health** for every SKU: active, visible but not buyable, inactive or suppressed, with issues, price and stock.

> UI language is Spanish. 🇪🇸 Spanish README: [README.es.md](README.es.md)

## Tech stack

- **Python 3.9+**: `requests`, `sqlite3`
- **Flask** web panel with Jinja templates, a custom CSS design system, light/dark theme and an installable PWA (manifest + service worker)
- **Amazon SP-API** with LWA OAuth: Product Pricing, Catalog Items, Product Fees, Listings, Orders, Finances and Reports APIs
- **SQLite** for Buy Box history, listings, finances and catalog jobs
- Runs locally or on a small cloud server (Oracle Cloud Free Tier)

## Project structure

| File | What it does |
|---|---|
| `seller_notes.py` | Monitor: polls Buy Box offers, syncs listings and orders, writes daily reports (CLI: `demo`, `once`, `run`, `report`, `listings`) |
| `rentabilidad.py` | Profit engine: imports product costs, downloads Finances API events, computes profit per SKU and month |
| `analizador.py` | Supplier catalog analyzer: EAN → ASIN matching, pricing and fee estimates in batches |
| `panel.py` | Flask web panel with login: Dashboard, Buy Box analysis, Profit, Catalogs |
| `templates/`, `static/` | HTML templates, CSS, icons and PWA files |

## Getting started

```bash
pip install requests flask openpyxl
python seller_notes.py demo      # simulated day of data, no API keys needed
```

To use real data:

1. Create a private SP-API app in Amazon's Solution Provider Portal (roles: Pricing, Product Listing, Finance and Accounting, Order Tracking).
2. Copy `.env.example` to `.env` and fill in your LWA credentials and seller ID. **`.env` is git-ignored and must never be committed.**
3. Start the monitor and the panel:

```bash
python seller_notes.py run       # polls every 5 minutes; daily report at midnight
python panel.py                  # opens http://127.0.0.1:5050
```

## Design decisions

- **Real numbers only.** Estimates (e.g., fees for unsettled orders) are always labeled as estimates.
- **Sales count on the order date**, like Sellerboard. Unsettled orders get fees estimated from each SKU's history.
- **A refunded unit only recovers its cost if Amazon receives it back as sellable** (FBA returns report).
- **Data limits are shown, not hidden:** 5-minute polling can miss faster price changes, Amazon returns at most the top 20 offers, and missing FBA fees (no product dimensions) are flagged because they would inflate profit.

## Privacy

API keys, the SQLite database, daily reports and supplier catalogs are excluded through `.gitignore`. The repository contains code only.

## How it was built

Designed and product-managed by me, the business owner, and developed with an AI coding assistant (Claude). I defined the requirements and business rules, tested every feature against real Amazon data, and iterated on the UI designs I created in Google Stitch.
