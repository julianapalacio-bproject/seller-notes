---
target: Dashboard (/listings)
total_score: 24
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 3
target_identity: "file:/Users/julianapalacio/Documents/BLUE PROJECT AI/seller-notes/templates/listings.html"
target_fingerprint: "sha256:68fbbf8f4607eff10ece573fd00d9a04f05b72bbb8c2241f3c33455113b7469f"
target_path: /Users/julianapalacio/Documents/BLUE PROJECT AI/seller-notes/templates/listings.html
timestamp: 2026-10-29T13-52-00Z
slug: templates-listings-html
closed: true
---
Method: dual-agent (A: design review sub-agent · B: detector sub-agent)

## Design Health Score
| # | Heuristic | Score | Key Issue |
|---|---|---|---|
| 1 | Visibility of System Status | 2 | "Última consulta" pill is always green, whatever its age; refresh has no progress or confirmation |
| 2 | Match System / Real World | 3 | Good Spanish domain language; "Dashboard"/"Profit" English mix; skus_ignorados.txt leaks implementation |
| 3 | User Control and Freedom | 3 | Instant, reversible filters; tab and search lost on reload or theme toggle |
| 4 | Consistency and Standards | 2 | "Good" shown three ways (green dot, grey pill, grey .pos); same counts repeated in KPI cards and tabs |
| 5 | Error Prevention | 3 | Refresh disabled without a seller ID; few destructive actions |
| 6 | Recognition Rather Than Recall | 3 | Counts on tabs; "Revisar" doesn't say what to review |
| 7 | Flexibility and Efficiency | 2 | No column sort, no shortcuts, no URL state for a filtered view |
| 8 | Aesthetic and Minimalist Design | 2 | Decorative icon tiles and repeated KPIs push problem rows below the fold |
| 9 | Error Recovery | 2 | Raw exception goes into ?error=; a zero-stock row offers no next step |
| 10 | Help and Documentation | 2 | Data age and status meanings not explained |
| **Total** | | **24/40** | **Acceptable** |

## Design Specificity Verdict
LLM: content specific (FBA/FBM, amazon.es links, Spanish statuses, problem-first sort); composition is a generic SaaS admin shell (H1 card → 3 KPI cards with icon tiles → segmented filter + search → table). "Dashboard" covers only listing health, with no Buy Box or profit and no "what changed".
Deterministic: template scan was a false clean (Jinja url_for hid panel.css). URL scan: 19 findings at desktop width (16 undersized-ui-text = two rules: 9px FBA pill panel.css:957, 10px status pill panel.css:970; 2 low-contrast: placeholder 4.1:1 panel.css:891, kpi3-sub muted 4.4:1; 1 overused-font Inter, intentional). Mobile: 12 findings, plus nested-cards on .g-seg.tabs (false positive).

## Priority Issues
1. [P1] The page doesn't answer "what changed / what to do". Fix: diff the latest listings snapshot against the previous one and add an "Atención hoy" strip above the KPIs ("Todo en orden" when empty), plus one-line Buy Box and profit summaries. Cmd: shape / distill.
2. [P1] Mobile hides the decision columns: the table is 773px wide in a 301px space, so Precio/Stock/Estado are off-screen, and the table starts at about 1032px. Fix: stacked row cards below 680px; compact KPI strip without icon tiles; a visible hint that the nav scrolls. Cmd: adapt.
3. [P1] Staleness is never surfaced: the "Última consulta" pill is always green. Fix: age-based ok/warn/bad (<6h / >6h / >24h) plus "hace 3 h". Cmd: harden.
4. [P2] KPI cards repeat each other and decorate: "7 activos" and "1 sin stock" appear 3×; the 52px icon tiles carry nothing; "Revisar" has no target; .pos renders graphite. Fix: merge cards, drop the icons, link "Revisar" to its tab. Cmd: distill / clarify.
5. [P2] Text below the 11px floor and contrast misses: 9px FBA pill, 10px status pill, fog-ink placeholder 4.1:1. Fix: 11px minimum; placeholder → slate ink. Cmd: typeset / polish.

## Persona Red Flags
Owner on phone: about 1000px of header and KPIs before any product; long names wrap 4-5 lines; stock and status need a hidden sideways scroll; can't trust the green freshness pill; the panel binds to 127.0.0.1, so the phone can't reach it yet.
Partner: no context for skus_ignorados.txt or "Revisar"; no "since you last looked"; can't share a filtered view.
Daily desktop check: no column sort, no search shortcut, no export; refresh gives no completion confirmation.

## Minor Observations
- The Anton "DASHBOARD" H1 is the loudest element and says the least.
- The stock bar is scaled to the largest SKU (528 uds), which flattens the others; days of cover would be the actionable number.
- In dark mode the warn-row tint is nearly invisible.
- The theme toggle reloads the page and loses the filter.
- The footer repeats the header.

## Questions to Consider
- If the owner reads one line a day, what is it, and why isn't it the H1?
- Should Dashboard become a cross-pillar overview, with this table moving to "Listings"?
- Is stock without sales velocity ever actionable for an FBA wholesaler?
