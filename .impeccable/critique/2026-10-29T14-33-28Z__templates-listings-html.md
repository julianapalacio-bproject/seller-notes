---
target: Listings (/listings)
total_score: 27
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 2
target_identity: "file:/Users/julianapalacio/Documents/BLUE PROJECT AI/seller-notes/templates/listings.html"
target_fingerprint: "sha256:bc97c5dd224939eb5002d6a2cb58cd3d688591dc7e8663f095ee1f266357c4d9"
target_path: /Users/julianapalacio/Documents/BLUE PROJECT AI/seller-notes/templates/listings.html
timestamp: 2026-10-29T14-33-28Z
slug: templates-listings-html
---
Method: dual-agent (A: design review sub-agent · B: detector sub-agent)
Target: Listings (/listings) after steps 1-4 and two polish passes. The earlier 24/40 run scored this same file when it was the "Dashboard".

## Design Health Score
| # | Heuristic | Score | Key Issue |
|---|---|---|---|
| 1 | Visibility of System Status | 3 | Freshness by age, busy button, banners; fresh pill differs light vs dark; #count not aria-live |
| 2 | Match System / Real World | 3 | Plain Spanish next steps; "skus_ignorados.txt" filename exposed; truncated product names |
| 3 | User Control and Freedom | 3 | Tab/search in URL; no empty state or «Quitar filtro» when a search finds nothing |
| 4 | Consistency and Standards | 3 | Grey "Activo" vs emerald in DESIGN.md; tabs lack tab semantics; zero-count tabs clickable |
| 5 | Error Prevention | 2 | Only stock = 0 flagged; 6 uds looks as calm as 43 |
| 6 | Recognition Rather Than Recall | 3 | Inline next steps; no legend for the stock bar or state names |
| 7 | Flexibility and Efficiency | 2 | No column sort, no search shortcut, no export |
| 8 | Aesthetic and Minimalist Design | 3 | Clean summary; flags double-count one SKU; "Activo" pill repeated on every mobile card |
| 9 | Error Recovery | 3 | Clear messages + Reintentar; "otro" copy points to a detail that may not exist |
| 10 | Help and Documentation | 2 | No definitions for the states or ignored SKUs |
| **Total** | | **27/40** | **Good** |

## Design Specificity Verdict
LLM: mostly authored (header summary, flag chips, FBA/FBM tags, next-step copy); body remains a standard admin table; the stock column is the weak spot; page shows state, not change.
Deterministic: rendered scans in 4 states and 3 viewports: only overused-font (Inter, intentional). No horizontal overflow at 375 or 768; nothing below 11px. Template scan not used (Jinja hides CSS).

## Priority Issues
1. [P1] No "running low" signal; stock bar scaled to the 528-unit SKU. Fix: days of cover from sales (already computed on Dashboard), amber below 21 days, "N con stock bajo" flag; replace the bar. Cmd: clarify.
2. [P1] No "what changed since last look" on Listings. Fix: diff latest vs previous snapshot; "Cambios desde ayer" line in the summary; stock delta per row. Cmd: clarify.
3. [P2] Flags double-count one SKU ("1 sin stock" + "1 no activo" = same product); flags and tabs duplicate. Fix: lead with "N listings necesitan acción" + combined tab; hide or mute zero-count tabs. Cmd: distill.
4. [P2] Mobile header fills ~700 of 812 px; tabs scroll off-screen with no hint; 36 px targets. Fix: compact header, figures side by side, search before tabs, 44 px tabs, no "Activo" pill on healthy cards. Cmd: adapt.
5. [P3] Empty filter shows headers only; "otro" error copy refers to a missing detail; #count not aria-live. Cmd: harden.

## Persona Red Flags
Owner on phone: ~700 px of header before the first product; key tabs hidden in a scroller; search mid-screen.
Partner: skus_ignorados.txt and state names unexplained; can't judge whether 6 uds is low.
Desktop daily check: no deltas; no sorting by stock; the stock bar carries no information.

## Minor Observations
- Trim trailing "|" and "," in product names; add a title with the full name.
- Column widths shift between filters (table-layout: fixed).
- At 768 px the cards leave wide gaps; the table would fit from ~720 px.
- ?ok=1 banner could use the emerald tone.
- Dark warn-row tint reads grey.

## Questions to Consider
- Why units instead of days until stockout?
- Should the default tab be "Necesitan acción", with healthy listings collapsed?
- On the phone, should search own the prime spot instead of «Actualizar ahora»?
