#!/usr/bin/env python3
"""
Seller Notes — panel web (solo en tu ordenador).

Lee la misma base de datos que seller_notes.py y muestra:
  /listings  estado de tus SKU (activo/inactivo, incidencias, precio, stock)
  /buybox    Buy Box de los ASIN vigilados: ganador, reparto del día, precio y movimientos

Uso:
  python panel.py        # abre http://127.0.0.1:5050
"""
import json
import sys
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from threading import Timer

from flask import Flask, redirect, render_template, request, url_for

import seller_notes as sn

PORT = 5050
app = Flask(__name__)

STATUS_ORDER = {"SUPRIMIDO": 0, "INACTIVO": 1, "VISIBLE, NO COMPRABLE": 2, "ACTIVO": 3}
EVENT_LABELS = {
    "vendedor_entra": "Entra",
    "vendedor_sale": "Sale",
    "cambio_buybox": "Buy Box →",
    "cambio_precio": "Cambio de precio",
    "cambio_precio_buybox": "Precio Buy Box",
}


def local(ts, fmt="%d/%m %H:%M"):
    return datetime.fromisoformat(ts).astimezone(sn.TZ).strftime(fmt) if ts else ""


def day_range(day):
    start = datetime(day.year, day.month, day.day, tzinfo=sn.TZ)
    end = start + timedelta(days=1)
    return start, end, start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()


app.jinja_env.filters["local"] = local


# ---------------------------------------------------------------- mis listings
def latest_listings(con):
    """Filas de la última consulta de listings (sin los SKU ignorados)."""
    try:
        ts = con.execute("SELECT MAX(ts) FROM listings").fetchone()[0]
    except Exception:  # la tabla aún no existe
        return None, []
    if not ts:
        return None, []
    skip = sn.ignored_skus()
    cols = ["sku", "asin", "nombre", "estado", "precio", "canal", "stock", "errores", "avisos", "incidencias"]
    rows = [dict(zip(cols, r)) for r in con.execute(f"SELECT {', '.join(cols)} FROM listings WHERE ts=?", (ts,))]
    rows = [r for r in rows if r["sku"] not in skip]
    for r in rows:
        r["lista_incidencias"] = [i for i in (r["incidencias"] or "").split(" | ") if i]
    rows.sort(key=lambda r: (STATUS_ORDER.get(r["estado"], 9), r["sku"]))
    return ts, rows


@app.route("/")
def home():
    return redirect(url_for("listings"))


@app.route("/listings")
def listings():
    con = sn.db()
    ts, rows = latest_listings(con)
    count = defaultdict(int)
    for r in rows:
        count[r["estado"]] += 1
    tiles = [
        ("Activos", count["ACTIVO"], "ok"),
        ("Visibles, no comprables", count["VISIBLE, NO COMPRABLE"], "warn"),
        ("Inactivos", count["INACTIVO"], "bad"),
        ("Suprimidos", count["SUPRIMIDO"], "bad"),
        ("Unidades en stock", sum(r["stock"] or 0 for r in rows), ""),
    ]
    return render_template("listings.html", page="listings", ts=ts, rows=rows, tiles=tiles,
                           ignored=len(sn.ignored_skus()), error=request.args.get("error"),
                           has_seller_id=bool(sn.MY_SELLER_ID))


@app.route("/listings/actualizar", methods=["POST"])
def refresh_listings():
    try:
        sn.listings_status(sn.db(), quiet=True)
    except Exception as e:
        return redirect(url_for("listings", error=str(e)[:300]))
    return redirect(url_for("listings"))


# ---------------------------------------------------------------- buy box
def asin_day(con, asin, day, names):
    start, end, s_utc, e_utc = day_range(day)
    snaps = con.execute(
        "SELECT id, ts, total_offers, bb_seller, bb_price, my_stock FROM snapshots "
        "WHERE asin=? AND ts>=? AND ts<? ORDER BY ts", (asin, s_utc, e_utc)).fetchall()
    data = {"asin": asin, "snaps": len(snaps)}
    if not snaps:
        return data

    # % de Buy Box ponderado por tiempo (igual que el informe diario)
    share = defaultdict(float)
    now = datetime.now(sn.TZ)
    for cur, nxt in zip(snaps, snaps[1:] + [None]):
        t0 = datetime.fromisoformat(cur[1])
        t1 = datetime.fromisoformat(nxt[1]) if nxt else min(end, now)
        share[cur[3]] += max((t1 - t0).total_seconds(), 0)
    tot = sum(share.values()) or 1
    data["share"] = [(sn.name(s, names), 100 * v / tot, s == sn.MY_SELLER_ID and bool(s))
                     for s, v in sorted(share.items(), key=lambda x: -x[1])]

    last = snaps[-1]
    data.update(ts=last[1], offers_n=last[2], bb_seller=names.get(last[3], last[3]) or "(nadie / suprimida)",
                bb_is_me=bool(sn.MY_SELLER_ID) and last[3] == sn.MY_SELLER_ID, bb_price=last[4], my_stock=last[5])
    prices = [s[4] for s in snaps if s[4] is not None]
    data["price_min"], data["price_max"] = (min(prices), max(prices)) if prices else (None, None)
    data["chart"] = json.dumps({"labels": [local(s[1], "%H:%M") for s in snaps],
                                "prices": [s[4] for s in snaps],
                                "offers": [s[2] for s in snaps]})
    # Amazon a veces marca varias ofertas como ganadoras; se señala solo la que guardó el monitor
    data["offers"], bb_marked = [], False
    for r in con.execute("SELECT seller_id, price, shipping, landed, fba FROM offers "
                         "WHERE snapshot_id=? ORDER BY landed", (last[0],)):
        is_bb = not bb_marked and r[0] == last[3] and r[3] == last[4]
        bb_marked = bb_marked or is_bb
        data["offers"].append(dict(seller=sn.name(r[0], names), is_me=bool(sn.MY_SELLER_ID) and r[0] == sn.MY_SELLER_ID,
                                   price=r[1], shipping=r[2], landed=r[3], fba=r[4], is_bb=is_bb))
    data["events"] = [
        dict(ts=r[0], label=EVENT_LABELS.get(r[1], r[1]), type=r[1], seller=sn.name(r[2], names), old=r[3], new=r[4])
        for r in con.execute("SELECT ts, type, seller_id, old_value, new_value FROM events "
                             "WHERE asin=? AND ts>=? AND ts<? ORDER BY ts DESC", (asin, s_utc, e_utc))]
    return data


@app.route("/buybox")
def buybox():
    con = sn.db()
    names = sn.seller_names()
    try:
        day = datetime.strptime(request.args.get("dia", ""), "%Y-%m-%d").date()
    except ValueError:
        day = datetime.now(sn.TZ).date()
    asins = [asin_day(con, a, day, names) for a in sn.ASINS]
    return render_template("buybox.html", page="buybox", asins=asins, day=day,
                           prev_day=day - timedelta(days=1), next_day=day + timedelta(days=1),
                           is_today=day == datetime.now(sn.TZ).date())


if __name__ == "__main__":
    if "--no-abrir" not in sys.argv:
        Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    print(f"Seller Notes · panel en http://127.0.0.1:{PORT}  (Ctrl+C para cerrar)")
    app.run(host="127.0.0.1", port=PORT, debug=False)
