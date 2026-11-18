"""Repricer de Seller Notes: ajusta el precio de tus ofertas según la Buy Box.

Cada producto tiene una estrategia:
  igualar  Iguala el precio de la Buy Box actual.
  debajo   Compite con la Buy Box: se pone 0,02 € por debajo de su precio actual.
Si ya tienes la Buy Box, el precio se mantiene.
Solo frena en lo que tú pongas (todo opcional): un mínimo y un máximo a mano, y un margen mínimo % (el precio que,
con el coste, las comisiones y la tarifa FBA, te deja ese margen). También respeta los precios mínimo y máximo de la
oferta en Seller Central, porque fuera de ellos Amazon desactiva la oferta.

Un producto solo cambia de precio en Amazon si está ACTIVADO; si no, el repricer solo anota qué haría
(simulación). Se ejecuta en el monitor justo después de cada consulta de la Buy Box (cada 5 min).
"""
import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

import rentabilidad as rt
import seller_notes as sn

STRATEGIES = {"igualar": "Igualar Buy Box", "debajo": "Competir con la Buy Box (−0,02 €)"}
DEFAULT_MARGIN = 10.0     # % mínimo de beneficio sobre el precio sin IVA para el mínimo automático
UNDERCUT = 0.02           # € por debajo de la Buy Box en la estrategia «debajo»
WAIT_AFTER_CHANGE = 15    # min: Amazon tarda en reflejar un cambio; mientras, no se vuelve a tocar


def ensure_tables(con):
    con.executescript("""
        CREATE TABLE IF NOT EXISTS repricer (
            sku TEXT PRIMARY KEY, asin TEXT, estrategia TEXT DEFAULT 'igualar', activo INTEGER DEFAULT 0,
            minimo REAL, maximo REAL, margen REAL, rebaja REAL,
            ultimo_precio REAL, ultimo_cambio TEXT);
        CREATE TABLE IF NOT EXISTS repricer_log (
            ts TEXT, sku TEXT, asin TEXT, antes REAL, nuevo REAL, bb_precio REAL, bb_vendedor TEXT,
            motivo TEXT, aplicado INTEGER, error TEXT);
        CREATE INDEX IF NOT EXISTS ix_replog ON repricer_log(sku, ts);
    """)


def settings(con):
    """Ajustes por SKU (los que aún no tienen fila salen con los valores por defecto, desactivados)."""
    ensure_tables(con)
    cols = ["sku", "asin", "estrategia", "activo", "minimo", "maximo", "margen", "rebaja", "ultimo_precio", "ultimo_cambio"]
    return {r[0]: dict(zip(cols, r)) for r in con.execute(f"SELECT {', '.join(cols)} FROM repricer")}


def save(con, sku, asin, **values):
    ensure_tables(con)
    con.execute("INSERT OR IGNORE INTO repricer(sku, asin) VALUES (?, ?)", (sku, asin))
    for k, v in values.items():
        if k in ("estrategia", "activo", "minimo", "maximo", "margen", "rebaja"):
            con.execute(f"UPDATE repricer SET {k}=?, asin=? WHERE sku=?", (v, asin, sku))
    con.commit()


def auto_minimum(con, sku, margin=DEFAULT_MARGIN):
    """Precio (con IVA) por debajo del cual perderías el margen mínimo. None si falta el coste."""
    cost, iva = rt.cost_on(con, sku, datetime.now(sn.TZ).date().isoformat())
    if cost is None:
        return None
    rates, default = rt.fee_rates(con)
    comm, fba = rates.get(sku, default)  # comisión: fracción (negativa) de la venta con IVA; FBA: € por unidad (negativo)
    v = (21.0 if iva is None else iva) / 100
    k = (1 - margin / 100) / (1 + v) + comm
    if k <= 0:
        return None
    return round((cost - fba) / k + 0.005, 2)


def market(con, asin):
    """Última consulta de la Buy Box: (hora, ofertas, ganadora) de ese ASIN."""
    row = con.execute("SELECT id, ts FROM snapshots WHERE asin=? ORDER BY ts DESC, id DESC LIMIT 1", (asin,)).fetchone()
    if not row:
        return None, [], None
    offers = [dict(seller_id=s, price=p, landed=l, fba=f, is_bb=b) for s, p, l, f, b in con.execute(
        "SELECT seller_id, price, landed, fba, is_bb FROM offers WHERE snapshot_id=?", (row[0],))]
    return row[1], offers, next((o for o in offers if o["is_bb"]), None)


def decide(con, sku, asin, conf, my_price=None):
    """Qué precio pondría ahora el repricer y por qué. Devuelve un dict con objetivo (None = no cambiar)."""
    conf = conf or {}
    strategy = conf.get("estrategia") if conf.get("estrategia") in STRATEGIES else "igualar"
    margin = conf.get("margen")
    by_margin = auto_minimum(con, sku, margin) if margin is not None else None
    lows = [x for x in (conf.get("minimo"), by_margin) if x is not None]
    low = max(lows) if lows else None  # con mínimo y margen a la vez manda el más alto
    high = conf.get("maximo")
    ts, offers, bb = market(con, asin)
    mine = next((o for o in offers if o["seller_id"] == sn.MY_SELLER_ID), None)
    current = mine["price"] if mine else my_price
    out = dict(sku=sku, asin=asin, estrategia=strategy, minimo=low, minimo_margen=by_margin, margen=margin, maximo=high,
               actual=current, bb=bb, consulta=ts, objetivo=None, motivo="")

    def done(target, why):
        if target is not None:
            target = round(target, 2)
            if high is not None and target > high:
                target, why = high, why + f" Tope en tu máximo ({high:.2f} €)."
            if low is not None and target < low and not (current is not None and target > current):
                if current is not None and current <= low + 0.005:  # el mínimo frena bajadas, nunca sube tu precio
                    target, why = None, why + f" Pero no baja de tu mínimo ({low:.2f} €): se mantiene."
                else:
                    target, why = low, why + f" No baja de tu mínimo ({low:.2f} €)."
            if target is not None and current is not None and abs(target - current) < 0.005:
                target = None
        out.update(objetivo=target, motivo=re.sub(r"(\d)\.(\d\d) €", r"\1,\2 €", why.strip()))  # 0.05 € → 0,05 €
        return out

    if high is not None and low is not None and high < low:
        return done(None, "El máximo es menor que el mínimo: corrígelos.")
    if margin is not None and by_margin is None:
        return done(None, f"Para usar el margen del {margin:g} % falta el coste de este producto.")
    if not offers:
        return done(None, "Aún no hay datos de la Buy Box de este producto.")
    if current is None:
        return done(None, "No encuentro tu oferta en este ASIN.")
    i_win = bool(mine and mine["is_bb"])
    if bb is None:
        return done(None, "Nadie tiene la Buy Box ahora: no se cambia.")

    if i_win:
        return done(None, "Tienes la Buy Box: se mantiene.")
    if strategy == "debajo":
        return done(bb["landed"] - UNDERCUT, f"Compite: {UNDERCUT:.2f} € por debajo de la Buy Box ({bb['landed']:.2f} €).")
    return done(bb["landed"], f"Iguala la Buy Box ({bb['landed']:.2f} €).")


# ---------------------------------------------------------------- cambiar el precio en Amazon
def _listing(sku):
    url = f"{sn.ENDPOINT}/listings/2021-08-01/items/{sn.MY_SELLER_ID}/{quote(sku, safe='')}"
    r = requests.get(url, params={"marketplaceIds": sn.MARKETPLACE_ID, "includedData": "summaries,attributes"},
                     headers={"x-amz-access-token": sn.access_token()}, timeout=30)
    r.raise_for_status()
    return url, r.json()


def set_price(sku, price):
    """Cambia tu precio (con IVA) en Amazon.es con la Listings Items API. Devuelve (ok, mensaje)."""
    url, item = _listing(sku)
    ptype = next((s.get("productType") for s in item.get("summaries", []) if s.get("marketplaceId") == sn.MARKETPLACE_ID),
                 None) or "PRODUCT"
    offers = (item.get("attributes") or {}).get("purchasable_offer") or []
    offer = next((o for o in offers if o.get("marketplace_id") == sn.MARKETPLACE_ID
                  and o.get("audience", "ALL") == "ALL"), None)
    now = next((x.get("value_with_tax") for x in ((offer or {}).get("our_price") or [{}])[0].get("schedule", [])), None)
    if now is not None and abs(float(now) - price) < 0.005:  # getItemOffers a veces devuelve tu precio antiguo un momento
        return True, "Ya estaba a ese precio en Amazon"
    def limit(key):
        v = next((x.get("value_with_tax") for x in ((offer or {}).get(key) or [{}])[0].get("schedule", [])), None)
        return None if v is None else float(v)
    amz_min, amz_max = limit("minimum_seller_allowed_price"), limit("maximum_seller_allowed_price")
    if amz_min is not None and price < amz_min - 0.005:
        return False, (f"No se cambia: {price:.2f} € está por debajo de tu precio mínimo en Seller Central "
                       f"({amz_min:.2f} €) y Amazon desactivaría la oferta.")
    if amz_max is not None and price > amz_max + 0.005:
        return False, f"No se cambia: {price:.2f} € supera tu precio máximo en Seller Central ({amz_max:.2f} €)."
    value = json.loads(json.dumps(offer)) if offer else {"marketplace_id": sn.MARKETPLACE_ID, "currency": "EUR"}
    value["our_price"] = [{"schedule": [{"value_with_tax": round(price, 2)}]}]  # mantiene el resto de la oferta
    body = {"productType": ptype, "patches": [{"op": "replace", "path": "/attributes/purchasable_offer", "value": [value]}]}
    r = requests.patch(url, params={"marketplaceIds": sn.MARKETPLACE_ID, "issueLocale": "es_ES"}, json=body,
                       headers={"x-amz-access-token": sn.access_token(), "content-type": "application/json"}, timeout=30)
    if r.status_code in (401, 403):
        return False, "Amazon no deja cambiar precios: falta el rol «Publicación de productos» en la app de SP-API."
    j = r.json() if r.content else {}
    if r.status_code >= 300 or j.get("status") != "ACCEPTED":
        issues = "; ".join(i.get("message", "") for i in j.get("issues", []) if i.get("severity") == "ERROR")
        return False, issues or j.get("errors", [{}])[0].get("message", f"HTTP {r.status_code}")
    return True, "Aceptado por Amazon"


def run(con, quiet=False):
    """Una ronda: decide cada producto; los activados cambian de precio, el resto solo se anota."""
    ensure_tables(con)
    conf = settings(con)
    now = datetime.now(timezone.utc)
    ts = now.isoformat(timespec="seconds")
    listings = {}
    try:
        listings = {sku: (asin, precio) for sku, asin, precio in con.execute(
            "SELECT sku, asin, precio FROM listings WHERE ts=(SELECT MAX(ts) FROM listings)")}
    except Exception:
        pass
    for sku, c in conf.items():
        asin = c["asin"] or listings.get(sku, (None, None))[0]
        if not asin:
            continue
        d = decide(con, sku, asin, c, listings.get(sku, (None, None))[1])
        if d["objetivo"] is None:
            continue
        if c["ultimo_cambio"] and c["ultimo_precio"] is not None:
            since = now - datetime.fromisoformat(c["ultimo_cambio"])
            if since < timedelta(minutes=WAIT_AFTER_CHANGE) and abs((d["actual"] or 0) - c["ultimo_precio"]) >= 0.005:
                continue  # Amazon aún no muestra el último cambio
        last = con.execute("SELECT nuevo, aplicado FROM repricer_log WHERE sku=? ORDER BY ts DESC LIMIT 1", (sku,)).fetchone()
        if not c["activo"]:
            if not (last and last[1] == 0 and last[0] == d["objetivo"]):  # simulación: anota solo si cambia la decisión
                con.execute("INSERT INTO repricer_log VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (ts, sku, asin, d["actual"], d["objetivo"], (d["bb"] or {}).get("landed"),
                             (d["bb"] or {}).get("seller_id"), d["motivo"], 0, None))
                con.commit()
            continue
        try:
            ok, msg = set_price(sku, d["objetivo"])
        except Exception as e:
            ok, msg = False, str(e)[:300]
        if ok and msg.startswith("Ya estaba"):
            continue  # nada que cambiar: no se anota
        if not ok and last and last[1] == -1 and last[0] == d["objetivo"]:
            continue  # mismo error que en la ronda anterior: no se repite en el historial
        con.execute("INSERT INTO repricer_log VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (ts, sku, asin, d["actual"], d["objetivo"], (d["bb"] or {}).get("landed"),
                     (d["bb"] or {}).get("seller_id"), d["motivo"], 1 if ok else -1, None if ok else msg))
        if ok:
            con.execute("UPDATE repricer SET ultimo_precio=?, ultimo_cambio=? WHERE sku=?", (d["objetivo"], ts, sku))
        con.commit()
        if not quiet:
            print(f"    Repricer {sku}: {d['actual']} → {d['objetivo']} € ({'ok' if ok else 'ERROR: ' + msg})", flush=True)
        time.sleep(0.5)


if __name__ == "__main__":
    con = sn.db()
    ensure_tables(con)
    for sku, c in settings(con).items():
        d = decide(con, sku, c["asin"], c)
        print(sku, d["actual"], "→", d["objetivo"], "|", d["motivo"])
