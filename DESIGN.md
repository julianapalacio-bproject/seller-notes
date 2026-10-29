---
name: Seller Notes
description: Private control center for The Blue Project's Amazon FBA business — Buy Box, real profit, sourcing and listing health.
colors:
  midnight-hull: "#0a162b"
  deep-navy: "#0f244a"
  bridge-blue: "#305f9e"
  signal-sky: "#81acf1"
  ice-blue: "#a8c8ff"
  lavender-mist: "#f9f9ff"
  lavender-soft: "#f2f3fd"
  lavender-line: "#ecedf7"
  lavender-well: "#e6e8f1"
  steel-edge: "#c1c6d5"
  ink: "#181c22"
  slate-ink: "#414753"
  fog-ink: "#646978"
  graphite: "#5c5c5c"
  profit-emerald: "#059669"
  loss-crimson: "#ba1a1a"
  caution-amber: "#92400e"
  caution-amber-num: "#d97706"
  card-white: "#ffffff"
typography:
  display:
    fontFamily: "Anton, Inter, sans-serif"
    fontSize: "30px"
    fontWeight: 400
    lineHeight: 1
    letterSpacing: "0.05em"
  headline:
    fontFamily: "Anton, Inter, sans-serif"
    fontSize: "20px"
    fontWeight: 400
    lineHeight: 1.3
    letterSpacing: "0.025em"
  figure:
    fontFamily: "Anton, Inter, sans-serif"
    fontSize: "30px"
    fontWeight: 400
    lineHeight: 1.2
    letterSpacing: "0.025em"
  title:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 700
    lineHeight: 1.4
  body:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.6
  table:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.45
    fontFeature: "\"tnum\""
  label:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "11px"
    fontWeight: 600
    lineHeight: 1.3
    letterSpacing: "0.05em"
rounded:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "20px"
  pill: "999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  card: "20px"
  gap: "18px"
  section: "24px"
components:
  button-primary:
    backgroundColor: "{colors.graphite}"
    textColor: "{colors.card-white}"
    rounded: "{rounded.md}"
    padding: "8px 16px"
    typography: "{typography.label}"
  button-primary-blue:
    backgroundColor: "{colors.bridge-blue}"
    textColor: "{colors.card-white}"
    rounded: "{rounded.md}"
    padding: "8px 16px"
  button-ghost:
    backgroundColor: "{colors.card-white}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "7px 14px"
  button-ghost-hover:
    backgroundColor: "{colors.lavender-soft}"
  card:
    backgroundColor: "{colors.card-white}"
    rounded: "{rounded.lg}"
    padding: "{spacing.card}"
  card-dark:
    backgroundColor: "{colors.deep-navy}"
    textColor: "{colors.card-white}"
    rounded: "{rounded.lg}"
    padding: "{spacing.card}"
  segmented:
    backgroundColor: "{colors.lavender-well}"
    rounded: "{rounded.md}"
    padding: "4px"
  segmented-active:
    backgroundColor: "{colors.card-white}"
    textColor: "{colors.graphite}"
    rounded: "{rounded.sm}"
  input-search:
    backgroundColor: "{colors.lavender-soft}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "6px 12px 6px 32px"
  pill-ok:
    backgroundColor: "#ecfdf5"
    textColor: "#065f46"
    rounded: "{rounded.pill}"
    padding: "2px 9px"
  pill-bad:
    backgroundColor: "#ffdad6"
    textColor: "#93000a"
    rounded: "{rounded.pill}"
    padding: "2px 9px"
  nav-item-active:
    backgroundColor: "rgba(255, 255, 255, 0.2)"
    textColor: "{colors.card-white}"
    rounded: "{rounded.pill}"
    padding: "6px 14px"
---

# Design System: Seller Notes

## Overview

**Creative North Star: "El puente de mando"** (the command bridge)

Seller Notes is the bridge of a small ship. A deep navy band runs across the top like the bridge window. Light, floating instrument panels sit below it on a pale lavender deck. The numbers are the instruments. Each panel shows one reading, and you read it in a glance without hunting. The mood is calm control, not alarm. Color is held back so that green (on course), crimson (losing money or the Buy Box) and amber (check this) mean something when they appear.

The surfaces are tactile. Cards float on soft, wide shadows with a bright inner top highlight, like a lit glass edge. Segmented controls sit *into* the surface with an inset shadow, and the active segment rises out of it as a white chip. Buttons press down when clicked. Anton is the only expressive voice: uppercase, condensed, used for page titles, section heads and headline figures. Everything else is quiet Inter.

The system is one family with two variants. **Apex** is the base (Dashboard, Análisis Productos, Catálogos): Anton titles, lavender deck, graphite or blue primary per page. **Navy** is the Profit variant: Inter-only headings, zinc greys and the bridge blue as primary. Both share the navy top bar, the type family and the blue accents. Every page now belongs to the family: Costes uses Navy (it lives under Profit), and the catalog result and column check use Apex. The old emerald "glass" style survives only as base rules and must not be used for new pages.

**Key Characteristics:**
- Navy gradient top bar (midnight hull → deep navy → bridge blue), sticky, 56px.
- White floating cards (16px radius) on a lavender gradient deck.
- Anton uppercase for titles and headline figures; Inter for everything else, with tabular numbers in data.
- Inset wells for controls, raised white chips for the active state.
- Semantic color only for state: emerald, crimson, amber.
- Light by default. Dark is the user's choice through the header toggle, never `prefers-color-scheme`.

## Colors

A cool, blue-led palette: navies for structure, lavender for the deck, and three semantic colors that only ever mean state.

### Primary
- **Bridge Blue** (#305f9e): the system's accent. Blue primary buttons (Análisis Productos uses a gradient to #2563eb), active Buy Box menu item, links to a product, chart series 0, and the end stop of the top bar gradient.
- **Graphite** (#5c5c5c): the Dashboard's primary button and active segment text. It is a neutral primary on purpose, so the page's only color comes from state.

### Secondary
- **Signal Sky** (#81acf1) and **Ice Blue** (#a8c8ff): secondary chart series, glows on dark cards, the Buy Box ring, labels on navy surfaces, and the dark-theme accents.

### Tertiary
- **Deep Navy** (#0f244a) and **Midnight Hull** (#0a162b): the top bar, the dark "hero" cards (`.g-dark`), and Catálogos' navy-gradient button.

### Neutral
- **Lavender Mist** (#f9f9ff): page background, which runs as a gradient to #e8edff.
- **Card White** (#ffffff at 90% opacity): floating panels.
- **Lavender Soft / Line / Well** (#f2f3fd / #ecedf7 / #e6e8f1): table headers, row dividers, segmented-control wells, field backgrounds and track bars.
- **Steel Edge** (#c1c6d5): dashed empty-state borders and middot separators.
- **Ink** (#181c22): primary text. **Slate Ink** (#414753): secondary text. **Fog Ink**: tertiary and placeholder text. Darkened from #717785 so it passes AA on every lavender surface (4.5–5.4:1).

### Semantic
- **Profit Emerald** (#059669; pill #ecfdf5 / #065f46): profit, active, "you hold the Buy Box".
- **Loss Crimson** (#ba1a1a; pill #ffdad6 / #93000a): loss, out of stock, suppressed, the Buy Box lost to another seller.
- **Caution Amber** (#92400e text, #d97706 figures, #fef3c7 fill): price changes, "visible but not buyable", things to check.

### Named Rules
**The State-Only Color Rule.** Emerald, crimson and amber never decorate. If a green, red or amber element doesn't mean on course, losing or check, it is a bug.

**The One Blue Rule.** Each page has a single primary (graphite on Dashboard, blue on Análisis Productos and Profit, navy gradient on Catálogos). Never put two primaries in one view.

## Typography

**Display Font:** Anton (with Inter, sans-serif)
**Body Font:** Inter (with system-ui, -apple-system, Segoe UI, Roboto)

**Character:** Anton is the stencilled lettering on the bridge: condensed, uppercase, loud, used sparingly. Inter is the instrument readout: neutral, dense and legible at 11–14px, with tabular figures so columns of euros line up.

### Hierarchy
- **Display** (Anton 400, 30px, line-height 1, +0.05em, uppercase): page titles in the header card. Catálogos uses 36px; mobile drops to 26px.
- **Headline** (Anton 400, 20px, 1.3, +0.025em): section titles inside cards (`h3.sec`).
- **Figure** (Anton 400, 28–30px, +0.025em): KPI figures, the Buy Box ring value and donut centers. Hero figures on dark cards are 48px.
- **Title** (Inter 700, 14px): emphasized row names, menu items. Profit's page title is Inter 700 at 20px, −0.025em, and it is the Navy variant's display.
- **Body** (Inter 400, 14px, 1.6): descriptions under titles, notes.
- **Table** (Inter 400, 12px, tabular numbers): all data tables and lists.
- **Label** (Inter 600, 11px, +0.05em, uppercase): table headers, KPI labels, card eyebrows.

### Named Rules
**The Anton Ration Rule.** Anton appears only in titles, section heads and headline figures, never in body, labels or buttons. Navy pages (Profit) don't use it at all.

**The 11px Floor Rule.** No readable text below 11px, including channel tags and table status pills.

## Layout

Single centered column, max-width 1180px, 16px side gutter. Pages stack in this order: a header card (title + description + primary action, plus a summary row where the page has one), filters, then a flush table card. Cards sit in CSS grid rows (`.g-row`) with 18px gaps (20px on Análisis Productos, 24px on Catálogos). Card padding is 20px (24px on Catálogos). Rows use asymmetric fractional columns (for example 1 : 1.25 : 1.5) rather than equal thirds.

Breakpoints: 1150px and 1050px, where three-column rows become two with the lead card spanning both. 900px and 820px, where everything becomes one column, the Buy Box menu turns into a horizontal scroller, and the top bar wraps with the nav scrolling sideways. 680px, where hero figures shrink.

Density is medium-high. Tables use 14px vertical cell padding with 12px type.

## Elevation & Depth

A layered system. Depth comes from three shadow roles plus tonal wells, and every lifted surface carries a bright inner top highlight (`inset 0 1px 0 rgba(255,255,255,.95)`) that reads as a lit edge.

### Shadow Vocabulary
- **Floating panel** (`0 20px 25px -5px rgba(15,23,42,.05), 0 8px 10px -6px rgba(15,23,42,.03), inset 0 1px 0 rgba(255,255,255,.95)`): every card. Análisis Productos tints it blue (`rgba(48,95,158,.08)`).
- **Raised control** (`0 1px 2px rgba(15,36,74,.05), inset 0 1px 0 rgba(255,255,255,.9)`): ghost buttons and fields.
- **Primary lift** (`0 4px 6px -1px rgba(0,0,0,.1), 0 2px 4px -2px rgba(0,0,0,.1)`; blue variant `0 4px 14px rgba(48,95,158,.35)`): primary buttons.
- **Well** (`inset 0 2px 4px rgba(0,0,0,.05)`): segmented-control tracks, search fields, drop zones.
- **Glow** (blurred radial blobs of bridge blue and sky at 30% inside `.g-dark`): dark hero cards only.

### Named Rules
**The Lit Edge Rule.** Anything that floats gets the white inner top highlight. Anything you put something *into* (wells, fields) gets an inset shadow instead.

**The No Card-in-Card Rule.** Inside a card, sub-panels (diagnostic tiles, event rows, margin boxes, gauges, tables) are flat tinted wells: a soft lavender or tone fill with no border and no shadow. Only top-level cards float.

**The Still Card Rule.** Cards don't lift or move on hover. They aren't clickable, so motion would be a false promise.

## Shapes

Soft rectangles. Cards use 16px (20px on Análisis Productos). Buttons, fields, segmented tracks and inner tiles use 12px. Segments inside a track use 8px. Status pills and nav items are full pills (999px). The small channel tag (FBA) is a 4px chip. Hairline borders are white at 80–90% opacity on cards, so a card's edge reads as light rather than a line. Lavender lines (#ecedf7) divide rows. Dashed steel borders mark empty states and the upload drop zone.

## Components

### Buttons
Tactile and confident: they sit on a lift shadow and press in when clicked.
- **Shape:** gently rounded (12px).
- **Primary:** page primary background (graphite #5c5c5c on Dashboard; bridge-blue gradient on Análisis Productos; deep-navy → bridge-blue gradient on Catálogos), white 12px/600 text, 8px 16px padding, a white 20% hairline border and the primary lift shadow. Header refresh buttons carry a 17px masked refresh icon that spins while Amazon is being queried (`.is-busy`).
- **Hover / Active:** hover darkens the background (#474746 or a deeper gradient). Active scales to 0.97 over 160ms `cubic-bezier(.23,1,.32,1)`. Disabled is 55% opacity.
- **Ghost:** white card background, a lavender 80% hairline, ink text, raised-control shadow. Hover fills lavender-soft. A `.small` variant has 5px 10px padding and 11px text.

### Chips / Pills
- **Style:** full-pill, 11px/700, 2px 9px, a 1px tinted border. Emerald, crimson, amber or blue sets by meaning. A 6px dot precedes live states. The "Última consulta" dot breathes slowly (3s opacity) when fresh.
- **Channel tag:** a 4px-radius solid chip (FBA in primary; FBM in lavender-well).

### Cards / Containers
- **Corner Style:** 16px.
- **Background:** white at 90% on the lavender deck. **Dark hero card** (`.g-dark`): vertical navy gradient (#0a162b → #0f1c38 → #071120), a sky-tinted border and glow blobs, ice-blue labels, white Anton figures.
- **Shadow Strategy:** floating panel (see Elevation).
- **Border:** white 90% hairline.
- **Internal Padding:** 20px (24px on Catálogos). Flush table cards pad 20px around the table.

### Inputs / Fields
- **Style:** lavender-soft fill, a white or lavender hairline, 12px radius, well shadow. Search has an inline magnifier icon at 10px from the left, 256px wide.
- **Focus:** the fill turns white and the border becomes bridge blue at 40%, with no outline ring. Profit uses a 2px ink outline.
- **Placeholder:** fog ink, which passes AA on the field fill.

### Navigation
- **Top bar:** sticky navy gradient band, 56px, with the logo (32px, 8px radius) and Anton wordmark at 16px. Nav sits after a 15%-white divider: pill links at 12px/500 in pale blue (#dbeafe at 90%). Hover gets a 10% white fill; active gets a 20% white fill, a hairline and 600 weight. The theme toggle is a 32px round ghost icon. On mobile the bar wraps and the nav scrolls sideways.
- **Segmented filter** (`.g-seg`): a lavender-well track with an inset shadow. Segments are 12px/600 fog text; the active one is a raised white chip with primary-colored text. Tabs carry live counts, for example "Activos (7)".

### Signature: Summary row (Listings)
Page summaries live in the header card, below a hairline, not in a row of separate KPI cards. The row has a 26px Anton figure with its label («7 de 8 activos») over an 8px segmented status bar, a second figure («713 uds en stock»), and right-aligned problem chips: tone-filled pills with a dot, each linking to the matching table filter. When nothing needs attention, a single emerald «Todo activo, con stock y sin incidencias» chip replaces them. No decorative icon tiles, and no count repeated across cards.

### Signature: Bridge readout (Dashboard)
The page title is the day's verdict, set in Anton at 40px: «Todo en orden», «2 urgentes · 3 por revisar». The date and three freshness chips (Buy Box, Listings, Dinero) follow it, and each chip's dot turns amber or crimson when that data is stale. Below it, «Atención hoy» lists one row per action, urgent first. Each row has a tone label, an area tag, a one-line title, a two-line detail, «desde …» and a ghost button to the page that resolves it. Urgent rows are always visible; only the first three «Revisar» rows show, and the rest fold under «Mostrar N avisos más». Row tint comes from the tone's soft fill plus a 1px tone border, never a side stripe.

### Signature: Buy Box ring
A 112px SVG ring on a dark hero card: a 3px track at 8% white, and a fill in a bridge-blue → sky gradient with a soft glow (crimson when the Buy Box is lost). It shows an Anton percentage in the center and an ice-blue caption.

## Do's and Don'ts

### Do:
- **Do** keep the navy top bar, Inter body and bridge blue (#305f9e) on every page. They are what makes Apex and Navy one family.
- **Do** give each page exactly one primary color for its main action.
- **Do** use tabular figures for every number column and right-align money.
- **Do** put the white inner top highlight on anything that floats, and an inset shadow on anything that is a well.
- **Do** port Stitch screens faithfully (palette, fonts, shadows, buttons) and replace their mock numbers with real data.
- **Do** keep motion to press feedback (scale 0.97, 160ms) and state changes, and respect `prefers-reduced-motion`.

### Don't:
- **Don't** use emerald, crimson or amber for decoration. They only mean on course, losing or check.
- **Don't** set Anton for body text, labels, buttons or table content, or anywhere on Navy (Profit) pages.
- **Don't** set readable text below 11px.
- **Don't** add hover lift or motion to non-clickable cards.
- **Don't** follow `prefers-color-scheme`. Theme is the user's toggle, stored as `tema`.
- **Don't** build new pages in the old emerald glass style (Hanken Grotesk, #059669 primary). New pages take `glass apex` (or `glass navy` under Profit).
- **Don't** render thousands of rows at once: long tables page on the server (100 rows) with filters and sorting in the URL.
