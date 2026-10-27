#!/usr/bin/env python3
"""
Seller Notes — monitor de ASIN para Amazon.es (The Blue Project).

Consulta cada pocos minutos las ofertas de los ASIN configurados vía SP-API
(Product Pricing API, getItemOffers), guarda cada consulta en SQLite y detecta:
  - vendedores que entran / salen
  - cambios de ganador de la Buy Box
  - cambios de precio (por vendedor y de la Buy Box)

Uso:
  python seller_notes.py run            # bucle continuo (cada INTERVAL_MINUTES)
  python seller_notes.py once           # una sola consulta
  python seller_notes.py listings       # estado de tus SKU (activo/inactivo, incidencias, stock)
  python seller_notes.py report [FECHA] # informe del día (AAAA-MM-DD, por defecto hoy)
  python seller_notes.py demo           # genera datos simulados para probar el informe
"""
import csv
import json
import os
import random
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE = Path(__file__).resolve().parent
TZ = ZoneInfo("Europe/Madrid")  # hora de Amazon.es


# ---------------------------------------------------------------- config
def load_env(path=BASE / ".env"):
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
ASINS = [a.strip() for a in os.getenv("ASINS", "B0G643D8ZG,B0CNCL35CH").split(",") if a.strip()]
MY_SELLER_ID = os.getenv("MY_SELLER_ID", "").strip()
MARKETPLACE_ID = os.getenv("MARKETPLACE_ID", "A1RKKUPIHCS9HS")  # Amazon.es
ENDPOINT = os.getenv("SPAPI_ENDPOINT", "https://sellingpartnerapi-eu.amazon.com")
INTERVAL_MINUTES = float(os.getenv("INTERVAL_MINUTES", "5"))
LISTINGS_MINUTES = float(os.getenv("LISTINGS_MINUTES", "60"))  # cada cuánto revisar tus listings en "run"
DB_PATH = BASE / os.getenv("DB_FILE", "seller_notes.db")
SELLERS_CSV = BASE / "sellers.csv"


# ---------------------------------------------------------------- base de datos
def db():
    con = sqlite3.connect(DB_PATH, timeout=30)  # el monitor, el panel y el analizador escriben a la vez
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY, ts TEXT, asin TEXT,
            total_offers INTEGER, bb_seller TEXT, bb_price REAL, raw TEXT);
        CREATE TABLE IF NOT EXISTS offers (
            snapshot_id INTEGER, asin TEXT, ts TEXT, seller_id TEXT,
            price REAL, shipping REAL, landed REAL, fba INTEGER, is_bb INTEGER);
        CREATE TABLE IF NOT EXISTS events (
            ts TEXT, asin TEXT, type TEXT, seller_id TEXT,
            old_value TEXT, new_value TEXT);
        CREATE INDEX IF NOT EXISTS ix_snap ON snapshots(asin, ts);
        CREATE INDEX IF NOT EXISTS ix_ev ON events(asin, ts);
        """
    )
    cols = [r[1] for r in con.execute("PRAGMA table_info(snapshots)")]
    if "my_stock" not in cols:
        con.execute("ALTER TABLE snapshots ADD COLUMN my_stock INTEGER")
    return con


def seller_names():
    names = {}
    if SELLERS_CSV.exists():
        raw = SELLERS_CSV.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("mac_roman")  # guardado desde TextEdit/Numbers en Mac
        for row in csv.DictReader(text.splitlines()):
            sid, nom = (row.get("seller_id") or "").strip(), (row.get("nombre") or "").strip()
            if sid and nom:  # ignora filas incompletas en vez de fallar
                names[sid] = nom
    if MY_SELLER_ID:
        names.setdefault(MY_SELLER_ID, "YO (The Blue Project)")
    return names


def name(sid, names):
    if not sid:
        return "(nadie / suprimida)"
    return f"{names[sid]} [{sid}]" if sid in names else sid


# ---------------------------------------------------------------- SP-API
_token = {"value": None, "exp": 0}


def access_token():
    if _token["value"] and time.time() < _token["exp"] - 60:
        return _token["value"]
    r = requests.post(
        "https://api.amazon.com/auth/o2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": os.environ["LWA_REFRESH_TOKEN"],
            "client_id": os.environ["LWA_CLIENT_ID"],
            "client_secret": os.environ["LWA_CLIENT_SECRET"],
        },
        timeout=30,
    )
    r.raise_for_status()
    j = r.json()
    _token.update(value=j["access_token"], exp=time.time() + j.get("expires_in", 3600))
    return _token["value"]


def get_item_offers(asin):
    url = f"{ENDPOINT}/products/pricing/v0/items/{asin}/offers"
    params = {"MarketplaceId": MARKETPLACE_ID, "ItemCondition": "New", "CustomerType": "Consumer"}
    for attempt in range(5):
        r = requests.get(url, params=params, headers={"x-amz-access-token": access_token()}, timeout=30)
        if r.status_code == 429:  # límite de peticiones: esperar y reintentar
            time.sleep(2 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json().get("payload", {})
    raise RuntimeError(f"Demasiados 429 para {asin}")


def get_featured_spain(asins):
    """Quién tiene la Buy Box para un cliente en España (competitiveSummary).
    Amazon la reparte por tipo de cliente y código postal; gana el vendedor con más peso en visitas.
    Devuelve {asin: seller_id}; {} si falla (entonces se usa getItemOffers)."""
    url = f"{ENDPOINT}/batches/products/pricing/2022-05-01/items/competitiveSummary"
    out = {}
    for i in range(0, len(asins), 20):
        reqs = [{"asin": a, "marketplaceId": MARKETPLACE_ID, "includedData": ["featuredBuyingOptions"],
                 "method": "GET", "uri": "/products/pricing/2022-05-01/items/competitiveSummary"} for a in asins[i:i + 20]]
        try:
            for attempt in range(4):
                r = requests.post(url, json={"requests": reqs}, timeout=30,
                                  headers={"x-amz-access-token": access_token(), "content-type": "application/json"})
                if r.status_code != 429:  # admite 1 petición cada 30 s
                    break
                time.sleep(30)
            r.raise_for_status()
        except Exception as e:
            print(f"    (Buy Box en España no disponible: {e})", flush=True)
            continue
        for resp in r.json().get("responses", []):
            body = resp.get("body") or {}
            weight = {}
            for opt in body.get("featuredBuyingOptions", []):
                if opt.get("buyingOptionType") != "New":
                    continue
                for fo in opt.get("segmentedFeaturedOffers", []):
                    w = sum((seg.get("segmentDetails") or {}).get("glanceViewWeightPercentage", 0)
                            for seg in fo.get("featuredOfferSegments", []))
                    weight[fo.get("sellerId")] = weight.get(fo.get("sellerId"), 0) + w
            if body.get("asin"):
                out[body["asin"]] = max(weight, key=weight.get) if weight else None
    return out


_stock_api = {"enabled": True}


def get_my_stock():
    """Tu stock FBA disponible por ASIN (requiere el rol 'Logística de Amazon' en la app).
    Devuelve {} si el rol no está activo, sin detener el monitor."""
    if not _stock_api["enabled"]:
        return {}
    url = f"{ENDPOINT}/fba/inventory/v1/summaries"
    params = {"granularityType": "Marketplace", "granularityId": MARKETPLACE_ID,
              "marketplaceIds": MARKETPLACE_ID, "details": "true"}
    stock = {}
    try:
        while True:
            r = requests.get(url, params=params, headers={"x-amz-access-token": access_token()}, timeout=30)
            if r.status_code in (401, 403):
                _stock_api["enabled"] = False
                print("    (Tu stock no disponible: activa el rol 'Logística de Amazon' en la app y vuelve a autorizarla)", flush=True)
                return {}
            r.raise_for_status()
            j = r.json()
            for it in j.get("payload", {}).get("inventorySummaries", []):
                a = it.get("asin")
                q = (it.get("inventoryDetails") or {}).get("fulfillableQuantity")
                if q is None:
                    q = it.get("totalQuantity", 0)
                if a:
                    stock[a] = stock.get(a, 0) + int(q or 0)
            nxt = (j.get("pagination") or {}).get("nextToken")
            if not nxt:
                return stock
            params = {**params, "nextToken": nxt}
            time.sleep(0.6)
    except Exception as e:
        print(f"    (Error leyendo tu stock: {e})", flush=True)
        return {}


def parse_payload(payload):
    """Convierte la respuesta de getItemOffers en (total_ofertas, lista_ofertas)."""
    offers = []
    for o in payload.get("Offers", []):
        price = float(o.get("ListingPrice", {}).get("Amount", 0) or 0)
        ship = float(o.get("Shipping", {}).get("Amount", 0) or 0)
        offers.append(
            dict(
                seller_id=o.get("SellerId", ""),
                price=price,
                shipping=ship,
                landed=round(price + ship, 2),
                fba=1 if o.get("IsFulfilledByAmazon") else 0,
                is_bb=1 if o.get("IsBuyBoxWinner") else 0,
            )
        )
    total = 0
    for n in payload.get("Summary", {}).get("NumberOfOffers", []) or []:
        if str(n.get("condition", "")).lower() == "new":
            total += int(n.get("OfferCount", 0))
    return total or len(offers), offers


# ---------------------------------------------------------------- guardar + eventos
def last_snapshot(con, asin):
    row = con.execute(
        "SELECT id, bb_seller, bb_price FROM snapshots WHERE asin=? ORDER BY ts DESC, id DESC LIMIT 1",
        (asin,),
    ).fetchone()
    if not row:
        return None
    offers = {
        r[0]: r[1]
        for r in con.execute("SELECT seller_id, landed FROM offers WHERE snapshot_id=?", (row[0],))
    }
    return {"bb_seller": row[1], "bb_price": row[2], "offers": offers}


def store(con, asin, ts, total, offers, raw=None, my_stock=None):
    prev = last_snapshot(con, asin)
    bb = next((o for o in offers if o["is_bb"]), None)
    bb_seller = bb["seller_id"] if bb else None
    bb_price = bb["landed"] if bb else None
    if raw is not None:  # la respuesta completa ocupa mucho: solo se guarda la primera de cada día
        day_start = datetime.fromisoformat(ts).astimezone(TZ).replace(hour=0, minute=0, second=0)
        if con.execute("SELECT 1 FROM snapshots WHERE asin=? AND ts>=? AND raw IS NOT NULL LIMIT 1",
                       (asin, day_start.astimezone(timezone.utc).isoformat())).fetchone():
            raw = None
    cur = con.execute(
        "INSERT INTO snapshots(ts, asin, total_offers, bb_seller, bb_price, raw, my_stock) VALUES (?,?,?,?,?,?,?)",
        (ts, asin, total, bb_seller, bb_price, json.dumps(raw) if raw else None, my_stock),
    )
    sid = cur.lastrowid
    con.executemany(
        "INSERT INTO offers VALUES (?,?,?,?,?,?,?,?,?)",
        [(sid, asin, ts, o["seller_id"], o["price"], o["shipping"], o["landed"], o["fba"], o["is_bb"]) for o in offers],
    )

    events = []
    if prev is not None:
        now = {o["seller_id"]: o["landed"] for o in offers}
        for s in now.keys() - prev["offers"].keys():
            events.append(("vendedor_entra", s, None, now[s]))
        for s in prev["offers"].keys() - now.keys():
            events.append(("vendedor_sale", s, prev["offers"][s], None))
        for s in now.keys() & prev["offers"].keys():
            if abs(now[s] - prev["offers"][s]) >= 0.005:
                events.append(("cambio_precio", s, prev["offers"][s], now[s]))
        if bb_seller != prev["bb_seller"]:
            events.append(("cambio_buybox", bb_seller, prev["bb_seller"], bb_seller))
        if bb_price is not None and prev["bb_price"] is not None and abs(bb_price - prev["bb_price"]) >= 0.005:
            events.append(("cambio_precio_buybox", bb_seller, prev["bb_price"], bb_price))
    con.executemany(
        "INSERT INTO events VALUES (?,?,?,?,?,?)",
        [(ts, asin, t, s, None if a is None else str(a), None if b is None else str(b)) for t, s, a, b in events],
    )
    con.commit()
    return bb_seller, bb_price, total, events


def poll_once(con):
    names = seller_names()
    my_stock = get_my_stock()
    spain = get_featured_spain(ASINS)
    for i, asin in enumerate(ASINS):
        if i:
            time.sleep(2.5)  # getItemOffers admite ~0,5 peticiones/segundo
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            payload = get_item_offers(asin)
        except Exception as e:  # no detener el bucle por un fallo puntual
            print(f"[{ts}] {asin} ERROR: {e}", flush=True)
            continue
        total, offers = parse_payload(payload)
        if spain.get(asin) in {o["seller_id"] for o in offers}:  # getItemOffers puede marcar varias ganadoras: manda la de un cliente en España
            for o in offers:
                o["is_bb"] = 1 if o["seller_id"] == spain[asin] else 0
        stock = my_stock.get(asin) if my_stock else None
        bb_seller, bb_price, total, events = store(con, asin, ts, total, offers, raw=payload, my_stock=stock)
        local = datetime.fromisoformat(ts).astimezone(TZ).strftime("%H:%M")
        extra = f" | Mi stock: {stock} uds" if stock is not None else ""
        print(f"[{local}] {asin}: {total} ofertas | Buy Box: {name(bb_seller, names)} a {bb_price} €{extra}", flush=True)
        for t, s, a, b in events:
            print(f"    -> {t}: {name(s, names)} {a} → {b}", flush=True)


# ---------------------------------------------------------------- informe diario
def report(day=None):
    con = db()
    names = seller_names()
    day = datetime.strptime(day, "%Y-%m-%d").date() if day else datetime.now(TZ).date()
    start = datetime(day.year, day.month, day.day, tzinfo=TZ)
    end = start + timedelta(days=1)
    s_utc, e_utc = start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()
    lines = [f"SELLER NOTES · INFORME {day.isoformat()} (hora de España)", "=" * 60]
    csv_rows = []

    for asin in ASINS:
        snaps = con.execute(
            "SELECT ts, total_offers, bb_seller, bb_price FROM snapshots WHERE asin=? AND ts>=? AND ts<? ORDER BY ts",
            (asin, s_utc, e_utc),
        ).fetchall()
        lines.append(f"\nASIN {asin}  —  {len(snaps)} consultas")
        if not snaps:
            lines.append("  Sin datos ese día.")
            continue

        # % de Buy Box ponderado por tiempo
        share = defaultdict(float)
        for (ts, _, bb, _), nxt in zip(snaps, snaps[1:] + [None]):
            t0 = datetime.fromisoformat(ts)
            t1 = datetime.fromisoformat(nxt[0]) if nxt else min(end, datetime.now(TZ))
            share[bb] += max((t1 - t0).total_seconds(), 0)
        tot = sum(share.values()) or 1
        prices = [p for *_, p in snaps if p is not None]
        offers_n = [n for _, n, _, _ in snaps]

        ev = con.execute(
            "SELECT ts, type, seller_id, old_value, new_value FROM events WHERE asin=? AND ts>=? AND ts<? ORDER BY ts",
            (asin, s_utc, e_utc),
        ).fetchall()
        count = defaultdict(int)
        per_seller_changes = defaultdict(int)
        for _, t, s, *_ in ev:
            count[t] += 1
            if t == "cambio_precio":
                per_seller_changes[s] += 1

        lines.append(f"  Precio Buy Box: mín {min(prices):.2f} € | máx {max(prices):.2f} € | último {prices[-1]:.2f} €" if prices else "  Buy Box sin precio")
        lines.append(f"  Nº de ofertas: mín {min(offers_n)} | máx {max(offers_n)} | último {offers_n[-1]}")
        ms = [r[0] for r in con.execute(
            "SELECT my_stock FROM snapshots WHERE asin=? AND ts>=? AND ts<? AND my_stock IS NOT NULL ORDER BY ts",
            (asin, s_utc, e_utc))]
        if ms:
            lines.append(f"  Mi stock FBA: inicio {ms[0]} uds | último {ms[-1]} uds")
        lines.append(f"  Cambios de precio de la Buy Box: {count['cambio_precio_buybox']}")
        lines.append(f"  Cambios de ganador de la Buy Box: {count['cambio_buybox']}")
        lines.append(f"  Vendedores que entran: {count['vendedor_entra']} | que salen: {count['vendedor_sale']}")
        lines.append("  Reparto de la Buy Box:")
        for s, secs in sorted(share.items(), key=lambda x: -x[1]):
            lines.append(f"    {100 * secs / tot:5.1f}%  {name(s, names)}")
        if per_seller_changes:
            lines.append("  Cambios de precio por vendedor:")
            for s, n in sorted(per_seller_changes.items(), key=lambda x: -x[1]):
                lines.append(f"    {n:4d}  {name(s, names)}")
        notable = [e for e in ev if e[1] in ("vendedor_entra", "vendedor_sale", "cambio_buybox")]
        if notable:
            lines.append("  Movimientos:")
            for ts, t, s, a, b in notable:
                h = datetime.fromisoformat(ts).astimezone(TZ).strftime("%H:%M")
                label = {"vendedor_entra": "ENTRA", "vendedor_sale": "SALE", "cambio_buybox": "BUY BOX →"}[t]
                extra = f" ({b} €)" if t == "vendedor_entra" else ""
                lines.append(f"    {h}  {label} {name(s, names)}{extra}")
        for ts, t, s, a, b in ev:
            csv_rows.append([datetime.fromisoformat(ts).astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S"), asin, t, s or "", names.get(s, ""), a or "", b or ""])

    text = "\n".join(lines)
    out_dir = BASE / "informes"
    out_dir.mkdir(exist_ok=True)
    (out_dir / f"informe_{day}.txt").write_text(text, encoding="utf-8")
    with (out_dir / f"eventos_{day}.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["hora", "asin", "evento", "seller_id", "nombre", "antes", "despues"])
        w.writerows(csv_rows)
    print(text)
    print(f"\nGuardado en {out_dir}/informe_{day}.txt y eventos_{day}.csv")


# ---------------------------------------------------------------- nombres pendientes
def pending_names():
    """Añade a sellers.csv los IDs vistos sin nombre y abre sus fichas en Amazon."""
    con = db()
    known = seller_names()
    listed = set()
    if SELLERS_CSV.exists():
        raw = SELLERS_CSV.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("mac_roman")
        listed = {(r.get("seller_id") or "").strip() for r in csv.DictReader(text.splitlines())}
        SELLERS_CSV.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    else:
        SELLERS_CSV.write_text("seller_id,nombre\n", encoding="utf-8")
    seen = [r[0] for r in con.execute(
        "SELECT seller_id, MAX(ts) m FROM offers WHERE seller_id<>'' GROUP BY seller_id ORDER BY m DESC")]
    missing = [sid for sid in seen if sid not in known]
    new = [sid for sid in missing if sid not in listed]
    if new:
        with SELLERS_CSV.open("a", encoding="utf-8") as f:
            for sid in new:
                f.write(f"{sid},\n")
    if not missing:
        print("Todos los vendedores vistos ya tienen nombre.")
        return
    print(f"{len(missing)} vendedores sin nombre. Escribe el nombre después de la coma en sellers.csv:\n")
    for sid in missing:
        url = f"https://www.amazon.es/sp?seller={sid}"
        print(f"  {sid}  →  {url}")
    if sys.platform == "darwin" and "--abrir" in sys.argv:
        import subprocess
        for sid in missing[:15]:
            subprocess.run(["open", f"https://www.amazon.es/sp?seller={sid}"])
        subprocess.run(["open", "-e", str(SELLERS_CSV)])


# ---------------------------------------------------------------- estado de mis listings
SUPPRESSION_ACTIONS = {"LISTING_SUPPRESSED", "SEARCH_SUPPRESSED", "ATTRIBUTE_SUPPRESSED", "CATALOG_ITEM_REMOVED"}
IGNORED_SKUS_FILE = BASE / "skus_ignorados.txt"
_listings_api = {"enabled": True}


def ignored_skus():
    """SKU que no se analizan: uno por línea en skus_ignorados.txt (las líneas con # se ignoran)."""
    if not IGNORED_SKUS_FILE.exists():
        IGNORED_SKUS_FILE.write_text(
            "# SKU que Seller Notes NO debe analizar: uno por línea.\n"
            "# Para volver a analizar uno, borra su línea o ponle # delante.\n", encoding="utf-8")
    return {l.split("#", 1)[0].strip() for l in IGNORED_SKUS_FILE.read_text(encoding="utf-8").splitlines()} - {""}


def search_my_listings():
    """Todos tus SKU en Amazon.es con estado, incidencias, precio y stock (Listings Items API,
    requiere el rol 'Publicación de productos'). Devuelve None si el rol no está activo."""
    if not _listings_api["enabled"]:
        return None
    url = f"{ENDPOINT}/listings/2021-08-01/items/{MY_SELLER_ID}"
    params = {"marketplaceIds": MARKETPLACE_ID, "issueLocale": "es_ES", "pageSize": 20,
              "includedData": "summaries,issues,offers,fulfillmentAvailability"}
    items = []
    while True:
        for attempt in range(5):
            r = requests.get(url, params=params, headers={"x-amz-access-token": access_token()}, timeout=30)
            if r.status_code != 429:
                break
            time.sleep(2 * (attempt + 1))
        if r.status_code in (401, 403):
            _listings_api["enabled"] = False
            print("    (Estado de listings no disponible: activa el rol 'Publicación de productos' / "
                  "'Product Listing' en la app y vuelve a autorizarla)", flush=True)
            return None
        r.raise_for_status()
        j = r.json()
        items += j.get("items", [])
        nxt = (j.get("pagination") or {}).get("nextToken")
        if not nxt:
            return items
        params = {**params, "pageToken": nxt}
        time.sleep(0.3)  # searchListingsItems admite ~5 peticiones/segundo


def parse_listing(it, fba_stock):
    """Resume un item de searchListingsItems en una fila con estado legible."""
    s = next((x for x in it.get("summaries", []) if x.get("marketplaceId") == MARKETPLACE_ID),
             (it.get("summaries") or [{}])[0])
    flags = s.get("status") or []
    issues = it.get("issues", [])
    errors = [i for i in issues if i.get("severity") == "ERROR"]
    warnings = [i for i in issues if i.get("severity") == "WARNING"]
    suppressed = any(a.get("action") in SUPPRESSION_ACTIONS
                     for i in issues for a in (i.get("enforcements") or {}).get("actions", []))
    if suppressed:
        status = "SUPRIMIDO"
    elif "BUYABLE" in flags:
        status = "ACTIVO"
    elif "DISCOVERABLE" in flags:
        status = "VISIBLE, NO COMPRABLE"
    else:
        status = "INACTIVO"
    offer = next((o for o in it.get("offers", []) if o.get("marketplaceId") == MARKETPLACE_ID
                  and o.get("offerType", "B2C") == "B2C"), None)
    price = float(offer["price"]["amount"]) if offer and offer.get("price") else None
    fbm = None
    fba = False
    for f in it.get("fulfillmentAvailability", []):
        if f.get("fulfillmentChannelCode") == "DEFAULT":
            fbm = (fbm or 0) + int(f.get("quantity") or 0)
        else:  # AMAZON_EU, AMAZON_NA...: la cantidad real viene de la API de inventario FBA
            fba = True
    asin = s.get("asin", "")
    stock = fba_stock.get(asin) if fba else fbm
    return dict(
        sku=it.get("sku", ""), asin=asin, nombre=(s.get("itemName") or "")[:80],
        estado=status, precio=price, canal="FBA" if fba else "FBM", stock=stock,
        errores=len(errors), avisos=len(warnings),
        incidencias=" | ".join(f"[{i.get('severity')}] {i.get('code')}: {i.get('message')}" for i in errors + warnings),
    )


def listings_status(con, quiet=False):
    """Consulta tus listings, los guarda, avisa de cambios de estado y escribe el CSV del día."""
    if not MY_SELLER_ID:
        print("Falta MY_SELLER_ID en .env (Seller Central > Configuración > Información de la cuenta > Token de comerciante)")
        return
    items = search_my_listings()
    if items is None:
        return
    con.execute("""CREATE TABLE IF NOT EXISTS listings (
        ts TEXT, sku TEXT, asin TEXT, nombre TEXT, estado TEXT, precio REAL, canal TEXT,
        stock INTEGER, errores INTEGER, avisos INTEGER, incidencias TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_list ON listings(sku, ts)")
    prev = {r[0]: r[1:] for r in con.execute(
        "SELECT sku, estado, errores FROM listings l WHERE ts=(SELECT MAX(ts) FROM listings WHERE sku=l.sku)")}
    skip = ignored_skus()
    items = [it for it in items if it.get("sku") not in skip]
    fba_stock = get_my_stock()
    rows = [parse_listing(it, fba_stock) for it in items]
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    con.executemany("INSERT INTO listings VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [(ts, r["sku"], r["asin"], r["nombre"], r["estado"], r["precio"], r["canal"],
                      r["stock"], r["errores"], r["avisos"], r["incidencias"]) for r in rows])
    con.commit()

    changes = []
    for r in rows:
        if r["sku"] in prev:
            old_status, old_err = prev[r["sku"]]
            if old_status != r["estado"]:
                changes.append(f"{r['sku']}: {old_status} → {r['estado']}")
            elif r["errores"] > old_err:
                changes.append(f"{r['sku']}: nuevos errores ({old_err} → {r['errores']})")
    local = datetime.now(TZ).strftime("%H:%M")
    if not quiet:
        ignored = f" ({len(skip)} ignorados en {IGNORED_SKUS_FILE.name})" if skip else ""
        print(f"\nMIS LISTINGS EN AMAZON.ES · {len(rows)} SKU{ignored} · {local}")
        print("=" * 60)
        order = {"SUPRIMIDO": 0, "INACTIVO": 1, "VISIBLE, NO COMPRABLE": 2, "ACTIVO": 3}
        for r in sorted(rows, key=lambda r: (order.get(r["estado"], 9), r["sku"])):
            precio = f"{r['precio']:.2f} €" if r["precio"] is not None else "sin precio"
            stock = f"{r['stock']} uds" if r["stock"] is not None else "stock ?"
            print(f"  {r['estado']:<22} {r['sku']:<20} {r['asin']:<11} {precio:>10} | {r['canal']} {stock}")
            if r["nombre"]:
                print(f"      {r['nombre']}")
            if r["incidencias"]:
                for inc in r["incidencias"].split(" | "):
                    print(f"      ! {inc}")
        resumen = defaultdict(int)
        for r in rows:
            resumen[r["estado"]] += 1
        print("\n  " + " | ".join(f"{k}: {v}" for k, v in sorted(resumen.items(), key=lambda x: order.get(x[0], 9))))
    for c in changes:
        print(f"[{local}] LISTING -> {c}", flush=True)

    out_dir = BASE / "informes"
    out_dir.mkdir(exist_ok=True)
    day = datetime.now(TZ).date()
    path = out_dir / f"listings_{day}.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["sku", "asin", "nombre", "estado", "precio", "canal", "stock", "errores", "avisos", "incidencias"])
        w.writerows([[r[k] if r[k] is not None else "" for k in
                      ("sku", "asin", "nombre", "estado", "precio", "canal", "stock", "errores", "avisos", "incidencias")]
                     for r in rows])
    if not quiet:
        print(f"\nGuardado en {path}")


# ---------------------------------------------------------------- demo
def demo():
    """Simula un día de datos (sin SP-API) para ver cómo queda el informe."""
    global DB_PATH
    DB_PATH = BASE / "demo.db"
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = db()
    rnd = random.Random(7)
    me = MY_SELLER_ID or "A1F1XT54LFOPE9"
    pool = {"A2SELLERXYZ01": 18.90, "A3SELLERXYZ02": 19.40, "A4SELLERXYZ03": 18.75}
    day = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    for asin in ASINS:
        sellers = {me: 19.20, "A2SELLERXYZ01": 18.90} if asin == ASINS[0] else {"A3SELLERXYZ02": 24.50}
        t = day
        while t < day + timedelta(hours=24) and t < datetime.now(TZ):
            if rnd.random() < 0.02 and len(sellers) < 4:
                s = rnd.choice([k for k in pool if k not in sellers] or [None])
                if s:
                    sellers[s] = pool[s]
            if rnd.random() < 0.015 and len(sellers) > 1:
                sellers.pop(rnd.choice([k for k in sellers if k != me]))
            for s in sellers:
                if rnd.random() < 0.12:
                    sellers[s] = round(max(sellers[s] + rnd.choice([-0.10, -0.05, 0.05, 0.10]), 10), 2)
            bb = min(sellers, key=lambda s: sellers[s] - (0.08 if s == me else 0))
            offers = [dict(seller_id=s, price=p, shipping=0.0, landed=p, fba=1, is_bb=int(s == bb)) for s, p in sellers.items()]
            store(con, asin, t.astimezone(timezone.utc).isoformat(timespec="seconds"), len(offers), offers)
            t += timedelta(minutes=INTERVAL_MINUTES)
    print(f"Datos simulados creados en {DB_PATH.name}\n")
    report(day.date().isoformat())


# ---------------------------------------------------------------- main
def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "demo":
        return demo()
    if cmd == "nombres":
        return pending_names()
    if cmd == "report":
        return report(sys.argv[2] if len(sys.argv) > 2 else None)
    missing = [k for k in ("LWA_CLIENT_ID", "LWA_CLIENT_SECRET", "LWA_REFRESH_TOKEN") if not os.getenv(k)]
    if missing:
        sys.exit(f"Faltan claves en .env: {', '.join(missing)}  (puedes probar con: python seller_notes.py demo)")
    con = db()
    if cmd == "once":
        return poll_once(con)
    if cmd == "listings":
        return listings_status(con)
    import fcntl
    lock = open(BASE / ".monitor.lock", "w")
    try:  # un solo monitor a la vez: dos duplicarían las consultas y los datos
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("Ya hay un monitor en marcha en otra ventana. Ciérralo (Ctrl+C) antes de abrir otro.")
    print(f"Seller Notes · vigilando {', '.join(ASINS)} cada {INTERVAL_MINUTES:g} min. Ctrl+C para parar.", flush=True)
    last_report_day = datetime.now(TZ).date()
    last_listings = 0.0
    last_finances = 0.0
    while True:
        started = time.time()
        poll_once(con)
        if MY_SELLER_ID and started - last_listings >= LISTINGS_MINUTES * 60:
            try:
                listings_status(con, quiet=True)  # en el bucle solo avisa de cambios de estado
            except Exception as e:
                print(f"    (Error leyendo tus listings: {e})", flush=True)
            last_listings = started
        if started - last_finances >= 24 * 3600:  # movimientos de dinero y devoluciones, una vez al día
            try:
                import rentabilidad
                rentabilidad.sync(progress=lambda m: None)
                print(f"[{datetime.now(TZ):%H:%M}] Rentabilidad: movimientos de Amazon actualizados", flush=True)
            except Exception as e:
                print(f"    (Error actualizando la rentabilidad: {e})", flush=True)
            last_finances = started
        today = datetime.now(TZ).date()
        if today != last_report_day:  # al cambiar de día, genera el informe del día anterior
            report(last_report_day.isoformat())
            last_report_day = today
        time.sleep(max(INTERVAL_MINUTES * 60 - (time.time() - started), 5))


if __name__ == "__main__":
    main()
