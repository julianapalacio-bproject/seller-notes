#!/usr/bin/env python3
"""
Seller Notes — panel web (solo en tu ordenador).

Lee la misma base de datos que seller_notes.py y muestra:
  /listings  estado de tus SKU (activo/inactivo, incidencias, precio, stock)
  /buybox    Buy Box de los ASIN vigilados: ganador, reparto del día, precio y movimientos
  /catalogos analizador de catálogos de proveedor (ROI, BSR, comisiones)

Uso:
  python panel.py        # abre http://127.0.0.1:5050
"""
import io
import json
import sys
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Thread, Timer

from flask import Flask, jsonify, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

import analizador as an
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
PERIODS = {"dia": ("Día", 1), "7d": ("7 días", 7), "30d": ("30 días", 30)}


def product_labels(con):
    """{asin: (sku, nombre)} según la última consulta de tus listings."""
    try:
        rows = con.execute("SELECT asin, sku, nombre FROM listings l "
                           "WHERE ts=(SELECT MAX(ts) FROM listings WHERE asin=l.asin)").fetchall()
    except Exception:  # la tabla aún no existe
        return {}
    return {r[0]: (r[1], r[2]) for r in rows}


def buybox_menu(con, names, labels):
    """Una entrada por ASIN vigilado con quién tiene ahora la Buy Box."""
    menu = []
    for asin in sn.ASINS:
        last = con.execute("SELECT bb_seller, bb_price FROM snapshots WHERE asin=? ORDER BY ts DESC, id DESC LIMIT 1",
                           (asin,)).fetchone()
        sku, nombre = labels.get(asin, ("", ""))
        menu.append(dict(asin=asin, sku=sku, nombre=nombre, has_data=bool(last),
                         bb_price=last[1] if last else None,
                         bb_is_me=bool(last and sn.MY_SELLER_ID and last[0] == sn.MY_SELLER_ID)))
    return menu


def asin_history(con, asin, start, end, names):
    s_utc, e_utc = start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()
    label_fmt = "%H:%M" if end - start <= timedelta(days=1) else "%d/%m %H:%M"
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
        t1 = min(t1, t0 + timedelta(minutes=2 * sn.INTERVAL_MINUTES))  # huecos con el monitor apagado no cuentan
        share[cur[3]] += max((t1 - t0).total_seconds(), 0)
    tot = sum(share.values()) or 1
    data["share"] = [(sn.name(s, names), 100 * v / tot, s == sn.MY_SELLER_ID and bool(s))
                     for s, v in sorted(share.items(), key=lambda x: -x[1])]

    last = snaps[-1]
    data.update(ts=last[1], offers_n=last[2], bb_seller=names.get(last[3], last[3]) or "(nadie / suprimida)",
                bb_is_me=bool(sn.MY_SELLER_ID) and last[3] == sn.MY_SELLER_ID, bb_price=last[4], my_stock=last[5])
    prices = [s[4] for s in snaps if s[4] is not None]
    data["price_min"], data["price_max"] = (min(prices), max(prices)) if prices else (None, None)
    changes = con.execute("SELECT COUNT(*) FROM events WHERE asin=? AND ts>=? AND ts<? AND type='cambio_buybox'",
                          (asin, s_utc, e_utc)).fetchone()[0]
    data["bb_changes"] = changes
    data["chart"] = json.dumps({"labels": [local(s[1], label_fmt) for s in snaps],
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
                             "WHERE asin=? AND ts>=? AND ts<? ORDER BY ts DESC LIMIT 500", (asin, s_utc, e_utc))]
    return data


@app.route("/buybox")
def buybox():
    con = sn.db()
    names = sn.seller_names()
    labels = product_labels(con)
    menu = buybox_menu(con, names, labels)
    asin = request.args.get("asin")
    if asin not in sn.ASINS:
        asin = sn.ASINS[0] if sn.ASINS else None
    period = request.args.get("periodo", "dia")
    if period not in PERIODS:
        period = "dia"
    today = datetime.now(sn.TZ).date()
    try:
        day = datetime.strptime(request.args.get("dia", ""), "%Y-%m-%d").date()
    except ValueError:
        day = today
    # "dia" muestra ese día; 7d/30d muestran los N días que terminan ese día
    days = PERIODS[period][1]
    start, _, _, _ = day_range(day - timedelta(days=days - 1))
    _, end, _, _ = day_range(day)
    data = asin_history(con, asin, start, end, names) if asin else None
    if data is not None:
        data["sku"], data["nombre"] = labels.get(asin, ("", ""))
    return render_template("buybox.html", page="buybox", menu=menu, a=data, asin=asin,
                           period=period, periods=PERIODS, day=day, first_day=day - timedelta(days=days - 1),
                           prev_day=day - timedelta(days=days), next_day=day + timedelta(days=days),
                           is_today=day >= today)


# ---------------------------------------------------------------- catálogos de proveedor
UPLOAD_DIR = sn.BASE / "catalogos"


def catalog_jobs(con):
    an.ensure_tables(con)
    cols = ["id", "ts", "archivo", "proveedor", "estado", "progreso", "mensaje", "n"]
    return [dict(zip(cols, r)) for r in con.execute(f"SELECT {', '.join(cols)} FROM catalogos ORDER BY id DESC")]


@app.route("/catalogos")
def catalogos():
    return render_template("catalogos.html", page="catalogos", jobs=catalog_jobs(sn.db()),
                           error=request.args.get("error"), iva=an.IVA_VENTA)


@app.route("/catalogos/subir", methods=["POST"])
def subir_catalogo():
    f = request.files.get("archivo")
    if not f or not f.filename:
        return redirect(url_for("catalogos", error="Elige un archivo .xlsx o .csv"))
    UPLOAD_DIR.mkdir(exist_ok=True)
    path = UPLOAD_DIR / f"{datetime.now(sn.TZ):%Y%m%d-%H%M%S}_{secure_filename(f.filename) or 'catalogo.xlsx'}"
    f.save(path)
    try:
        rows = an.read_table(path)
    except Exception as e:
        path.unlink(missing_ok=True)
        return redirect(url_for("catalogos", error=str(e)))
    if not rows:
        return redirect(url_for("catalogos", error="El archivo está vacío"))
    header_idx, mapping = an.detect_columns(rows)
    headers = [str(c if c is not None else "") for c in rows[header_idx]]
    preview = [[("" if c is None else str(c)) for c in r] for r in rows[header_idx + 1: header_idx + 6]]
    return render_template("catalogo_columnas.html", page="catalogos", archivo=path.name, original=f.filename,
                           header_idx=header_idx, headers=headers, mapping=mapping, preview=preview,
                           fields=an.FIELD_LABELS, total=len(rows) - header_idx - 1, iva=an.IVA_VENTA,
                           proveedor=Path(f.filename).stem)


@app.route("/catalogos/analizar", methods=["POST"])
def analizar_catalogo():
    form = request.form
    path = UPLOAD_DIR / secure_filename(form["archivo"])
    mapping = {f: int(form[f"col_{f}"]) for f in an.FIELD_LABELS if form.get(f"col_{f}", "") != ""}
    if "ean" not in mapping or "coste" not in mapping:
        return redirect(url_for("catalogos", error="Hay que indicar al menos la columna de EAN y la de coste"))
    iva = an.parse_money(form.get("iva")) or an.IVA_VENTA
    envio = an.parse_money(form.get("envio")) or 0.0
    con = sn.db()
    an.ensure_tables(con)
    cur = con.execute(
        "INSERT INTO catalogos(ts, archivo, proveedor, estado, progreso, mensaje, opciones) VALUES (?,?,?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), form.get("original") or path.name,
         form.get("proveedor", "").strip(), "en_curso", 0, "Empezando…",
         json.dumps({"iva": iva, "envio": envio, "mapping": mapping})))
    con.commit()
    job_id = cur.lastrowid
    Thread(target=an.run_job, args=(job_id, path, int(form["header_idx"]), mapping, iva, envio), daemon=True).start()
    return redirect(url_for("catalogo", job_id=job_id))


def load_job(job_id):
    con = sn.db()
    job = next((j for j in catalog_jobs(con) if j["id"] == job_id), None)
    items = [json.loads(r[0]) for r in con.execute(
        "SELECT data FROM catalogo_items WHERE catalogo_id=? ORDER BY fila", (job_id,))] if job else []
    for it in items:  # los productos sin datos de Amazon no traen todos los campos
        for key, *_ in an.COLUMNS:
            it.setdefault(key, None)
    return job, items


@app.route("/catalogos/<int:job_id>")
def catalogo(job_id):
    job, items = load_job(job_id)
    if not job:
        return redirect(url_for("catalogos"))
    found = [it for it in items if it.get("asin")]
    roi_ok = [it for it in found if (it.get("roi") or -999) >= 30]
    tiles = [("Productos", len(items), ""), ("En Amazon.es", len(found), ""),
             ("ROI ≥ 30%", len(roi_ok), "ok"),
             ("Amazon vende", sum(1 for it in found if it.get("amazon_vende")), "bad")]
    items.sort(key=lambda it: (it.get("roi") is None, -(it.get("roi") or 0)))
    return render_template("catalogo.html", page="catalogos", job=job, items=items, tiles=tiles)


@app.route("/catalogos/<int:job_id>/estado")
def catalogo_estado(job_id):
    job, _ = load_job(job_id)
    return jsonify({k: job[k] for k in ("estado", "progreso", "mensaje")} if job else {"estado": "error"})


@app.route("/catalogos/<int:job_id>/excel")
def catalogo_excel(job_id):
    job, items = load_job(job_id)
    if not job or job["estado"] != "terminado":
        return redirect(url_for("catalogos"))
    items.sort(key=lambda it: (it.get("roi") is None, -(it.get("roi") or 0)))
    name = f"analisis_{secure_filename(job['proveedor'] or Path(job['archivo']).stem)}_{local(job['ts'], '%Y-%m-%d')}.xlsx"
    return send_file(io.BytesIO(an.to_excel(items, job["archivo"])), as_attachment=True, download_name=name,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def mark_interrupted_jobs():
    """Si el panel se cerró a mitad de un análisis, ese análisis no va a terminar."""
    con = sn.db()
    an.ensure_tables(con)
    con.execute("UPDATE catalogos SET estado='error', mensaje='Interrumpido: el panel se cerró durante el análisis' "
                "WHERE estado='en_curso'")
    con.commit()


if __name__ == "__main__":
    mark_interrupted_jobs()
    if "--no-abrir" not in sys.argv:
        Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    print(f"Seller Notes · panel en http://127.0.0.1:{PORT}  (Ctrl+C para cerrar)")
    app.run(host="127.0.0.1", port=PORT, debug=False)
