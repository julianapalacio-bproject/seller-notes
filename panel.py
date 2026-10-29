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
import os
import sys
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Thread, Timer

from flask import Flask, jsonify, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

import analizador as an
import rentabilidad as rt
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
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(sn.TZ).strftime(fmt) if ts else ""


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
    """{asin: (sku, nombre)}. El nombre sale, por orden: de tu hoja de costes (el más corto), de tus listings
    o del título de la página de amazon.es (para los ASIN que no vendes)."""
    out = {}
    for sql in ("SELECT asin, '', titulo FROM titulos",
                "SELECT asin, sku, nombre FROM listings l WHERE ts=(SELECT MAX(ts) FROM listings WHERE asin=l.asin)",
                "SELECT asin, sku, nombre FROM costes c WHERE desde=(SELECT MAX(desde) FROM costes WHERE sku=c.sku)"):
        try:
            rows = con.execute(sql).fetchall()
        except Exception:  # la tabla aún no existe
            continue
        for asin, sku, nombre in rows:
            if asin:
                old = out.get(asin, ("", ""))
                out[asin] = (sku or old[0], nombre or old[1])
    return out


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
        "SELECT id, ts, total_offers, bb_seller, bb_price, my_stock, bb_prime, bb_normal, bb_fast, bb_fast_price, "
        "bb_fast_label FROM snapshots "
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
    # quién gana para clientes Prime y para el resto; None si la consulta es anterior a este dato
    data["segments"] = [(label, names.get(sid, sid), bool(sn.MY_SELLER_ID) and sid == sn.MY_SELLER_ID)
                        for label, sid in (("Prime", last[6]), ("Normal", last[7])) if sid] or None
    # segunda opción de compra que enseña la página de amazon.es (p. ej. «Recíbelo más rápido»)
    data["fast"] = dict(label=last[10], seller=names.get(last[8], last[8]), price=last[9],
                        me=bool(sn.MY_SELLER_ID) and last[8] == sn.MY_SELLER_ID) if last[8] else None
    prices = [s[4] for s in snaps if s[4] is not None]
    data["price_min"], data["price_max"] = (min(prices), max(prices)) if prices else (None, None)
    changes = con.execute("SELECT COUNT(*) FROM events WHERE asin=? AND ts>=? AND ts<? AND type='cambio_buybox'",
                          (asin, s_utc, e_utc)).fetchone()[0]
    data["bb_changes"] = changes
    data["chart"] = json.dumps({"labels": [local(s[1], label_fmt) for s in snaps],
                                "prices": [s[4] for s in snaps],
                                "offers": [s[2] for s in snaps]})
    # se señala solo la oferta que tiene la Buy Box para un cliente en España (la que guardó el monitor)
    data["offers"] = []
    for r in con.execute("SELECT seller_id, price, shipping, landed, fba FROM offers "
                         "WHERE snapshot_id=? ORDER BY landed", (last[0],)):
        data["offers"].append(dict(seller=sn.name(r[0], names), is_me=bool(sn.MY_SELLER_ID) and r[0] == sn.MY_SELLER_ID,
                                   price=r[1], shipping=r[2], landed=r[3], fba=r[4], is_bb=bool(r[0]) and r[0] == last[3],
                                   is_prime=bool(r[0]) and r[0] == last[6], is_normal=bool(r[0]) and r[0] == last[7],
                                   is_fast=bool(r[0]) and r[0] == last[8]))
    data["events"] = [
        dict(ts=r[0], label=EVENT_LABELS.get(r[1], r[1]), type=r[1], seller=sn.name(r[2], names), old=r[3], new=r[4])
        for r in con.execute("SELECT ts, type, seller_id, old_value, new_value FROM events "
                             "WHERE asin=? AND ts>=? AND ts<? ORDER BY ts DESC LIMIT 500", (asin, s_utc, e_utc))]
    return data


_fee_cache = {}  # {asin: (fecha, comisión/precio, tarifa FBA)}: la estimación de Amazon se pide una vez al día


def unit_economics(con, asin, sku):
    """Comisión (fracción del precio con IVA, negativa), tarifa FBA (negativa), coste e IVA de un SKU.
    Usa las ventas ya liquidadas; si aún no hay, la estimación de comisiones de Amazon."""
    cost, iva = rt.cost_on(con, sku, datetime.now(sn.TZ).date().isoformat()) if sku else (None, None)
    if cost is None:
        return None
    rates, default = rt.fee_rates(con)
    rate, fba, source = (*rates[sku], "tus ventas") if sku in rates else (None, None, "")
    if rate is None:
        today = datetime.now(sn.TZ).date()
        cached = _fee_cache.get(asin)
        if not cached or cached[0] != today:
            price = con.execute("SELECT bb_price FROM snapshots WHERE asin=? AND bb_price IS NOT NULL "
                                "ORDER BY ts DESC LIMIT 1", (asin,)).fetchone()
            try:
                est = an.fees_batch({asin: price[0]}, lambda x: None).get(asin) if price else None
            except Exception:
                est = None
            cached = (today, -est[1] / price[0], -est[2]) if est else (today, None, None)
            _fee_cache[asin] = cached
        rate, fba, source = cached[1], cached[2], "estimación de Amazon"
    if rate is None:
        rate, fba, source = default[0], default[1], "media de tu cuenta"
    net = 1 / (1 + iva / 100) + rate  # lo que te queda de cada euro cobrado, antes de FBA y coste
    return dict(cost=cost, iva=iva, rate=rate, fba=fba, source=source,
                breakeven=(cost - fba) / net if net > 0 else None,
                profit=lambda p: None if p is None else p / (1 + iva / 100) + rate * p + fba - cost)


def price_war(con, asin, names):
    """Señales de guerra de precios: caída del precio de la Buy Box en 7 días y vendedores que
    cambian de precio sin parar en las últimas 24 h (suele ser un programa automático)."""
    now = datetime.now(timezone.utc)
    week = con.execute("SELECT bb_price FROM snapshots WHERE asin=? AND ts>=? AND bb_price IS NOT NULL ORDER BY ts LIMIT 1",
                       (asin, (now - timedelta(days=7)).isoformat())).fetchone()
    last = con.execute("SELECT bb_price FROM snapshots WHERE asin=? AND bb_price IS NOT NULL ORDER BY ts DESC LIMIT 1",
                       (asin,)).fetchone()
    drop = 100 * (week[0] - last[0]) / week[0] if week and last and week[0] else 0.0
    bots = [(sn.name(sid, names), n, sid == sn.MY_SELLER_ID) for sid, n in con.execute(
        "SELECT seller_id, COUNT(*) n FROM events WHERE asin=? AND type='cambio_precio' AND ts>=? "
        "GROUP BY seller_id HAVING n >= 10 ORDER BY n DESC", (asin, (now - timedelta(days=1)).isoformat()))]
    return dict(active=drop >= 5 or any(not me for _, _, me in bots), drop=drop,
                week_price=week[0] if week else None, bots=bots)


def first_poll(asins):
    """Primera consulta de los ASIN recién añadidos, sin esperar a la siguiente vuelta del monitor."""
    try:
        sn.poll_once(sn.db(), asins)
    except Exception as e:
        print(f"    (Primera consulta de {', '.join(asins)}: {e})", flush=True)


@app.route("/buybox/asins", methods=["POST"])
def buybox_asins():
    sn.ASINS[:] = sn.read_asins()
    quitar = request.form.get("quitar", "").strip().upper()
    if quitar:
        sn.save_asins([a for a in sn.ASINS if a != quitar])
        return redirect(url_for("buybox"))
    found = list(dict.fromkeys(sn.ASIN_RE.findall(request.form.get("asins", "").upper())))
    if not found:
        return redirect(url_for("buybox", error="No encuentro ningún ASIN. Un ASIN tiene 10 caracteres, p. ej. B0G643D8ZG."))
    nuevos = [a for a in found if a not in sn.ASINS]
    if nuevos:
        sn.save_asins(sn.ASINS + nuevos)
        if os.getenv("LWA_REFRESH_TOKEN"):
            Thread(target=first_poll, args=(nuevos,), daemon=True).start()
    return redirect(url_for("buybox", asin=found[-1], aviso=f"{len(nuevos)} ASIN añadido{'s' if len(nuevos) != 1 else ''}"
                            if nuevos else "Ese ASIN ya estaba en la lista"))


@app.route("/buybox")
def buybox():
    sn.ASINS[:] = sn.read_asins()
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
        if data.get("snaps"):
            econ = unit_economics(con, asin, data["sku"])
            my_price = next((o["landed"] for o in data["offers"] if o["is_me"]), None)
            if econ:
                data["econ"] = dict(econ, my_price=my_price, my_profit=econ["profit"](my_price),
                                    bb_profit=econ["profit"](data["bb_price"]))
            data["war"] = price_war(con, asin, names)
    return render_template("buybox.html", page="buybox", menu=menu, a=data, asin=asin,
                           period=period, periods=PERIODS, day=day, first_day=day - timedelta(days=days - 1),
                           prev_day=day - timedelta(days=days), next_day=day + timedelta(days=days),
                           is_today=day >= today, error=request.args.get("error"), aviso=request.args.get("aviso"))


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


# ---------------------------------------------------------------- rentabilidad real
MONTHS_ES = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre",
             "Octubre", "Noviembre", "Diciembre"]
_sync_state = {"running": False, "msg": ""}


GROUPS = {"dia": "Día", "semana": "Semana", "mes": "Mes"}


def period_label(key, group):
    if group == "mes":
        return f"{MONTHS_ES[int(key[5:])][:3]} {key[:4]}"
    d = datetime.strptime(key, "%Y-%m-%d")
    return f"{d:%d/%m}" if group == "dia" else f"Sem. {d:%d/%m}"


def date_presets(today):
    first = today.replace(day=1)
    prev_last = first - timedelta(days=1)
    return {
        "hoy": ("Hoy", today, today),
        "ayer": ("Ayer", today - timedelta(days=1), today - timedelta(days=1)),
        "7d": ("Últimos 7 días", today - timedelta(days=6), today),
        "30d": ("Últimos 30 días", today - timedelta(days=29), today),
        "este_mes": ("Este mes", first, today),
        "mes_pasado": ("Mes pasado", prev_last.replace(day=1), prev_last),
        "todo": ("Todo", datetime.strptime(rt.DEFAULT_FROM, "%Y-%m-%d").date(), today),
    }


def profit_view(con, start, end, group):
    """Filas por SKU y gastos de cuenta del rango, totales y evolución por periodo."""
    skus, account = rt.aggregate(con, start, end, group)
    names = {r[0]: r[6] for r in rt.costs_table(con)}
    agg = {}
    for by_sku in skus.values():
        for sku, r in by_sku.items():
            a = agg.setdefault(sku, {k: 0 for k, v in r.items() if isinstance(v, (int, float)) and not isinstance(v, bool)})
            for k, v in r.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool) and k not in ("margen", "roi"):
                    a[k] += v
            a["sin_coste"] = a.get("sin_coste") or r["sin_coste"]
    rows = []
    for sku, a in agg.items():
        a["sku"], a["nombre"] = sku, names.get(sku, "")
        a["margen"] = 100 * a["beneficio"] / a["ingresos"] if a["ingresos"] else None
        a["roi"] = 100 * a["beneficio"] / a["coste"] if a["coste"] > 0 else None
        a["por_ud"] = a["beneficio"] / a["uds"] if a["uds"] else None
        rows.append(a)
    rows.sort(key=lambda r: -r["beneficio"])
    acc = {}
    for by_concept in account.values():
        for k, v in by_concept.items():
            acc[k] = acc.get(k, 0.0) + v
    prod = sum(r["beneficio"] for r in rows)
    tot = dict(ventas=sum(r["ventas"] for r in rows), devoluciones=sum(r["devoluciones"] for r in rows),
               ingresos=sum(r["ingresos"] for r in rows), uds=sum(r["uds"] for r in rows),
               estimadas=sum(r["estimadas"] for r in rows), devueltas=sum(r["devueltas"] for r in rows),
               producto=prod, cuenta=sum(acc.values()), neto=prod + sum(acc.values()))
    tot["margen"] = 100 * tot["neto"] / tot["ingresos"] if tot["ingresos"] else None
    keys, d = set(skus) | set(account), start
    while d <= end:  # también los periodos sin ventas, para que el gráfico no se salte días
        keys.add(rt.period_key(d, group))
        d += timedelta(days=1)
    periods = []
    for k in sorted(keys):
        by = skus.get(k, {}).values()
        p, c = sum(r["beneficio"] for r in by), sum(account.get(k, {}).values())
        periods.append(dict(key=k, label=period_label(k, group), uds=sum(r["uds"] for r in by),
                            ventas=sum(r["ventas"] for r in by), producto=p, cuenta=c, neto=p + c))
    return rows, sorted(acc.items(), key=lambda x: x[1]), tot, periods


@app.route("/rentabilidad")
def rentabilidad():
    con = sn.db()
    rt.ensure_tables(con)
    today = datetime.now(sn.TZ).date()
    presets = date_presets(today)
    preset = request.args.get("rango", "" if request.args.get("desde") else "este_mes")
    if preset in presets:
        _, start, end = presets[preset]
    else:
        try:
            start = datetime.strptime(request.args["desde"], "%Y-%m-%d").date()
            end = datetime.strptime(request.args.get("hasta") or today.isoformat(), "%Y-%m-%d").date()
        except (KeyError, ValueError):
            preset, (_, start, end) = "este_mes", presets["este_mes"]
        if end < start:
            start, end = end, start
    group = request.args.get("agrupar")
    if group not in GROUPS:
        days = (end - start).days
        group = "dia" if days <= 31 else "semana" if days <= 120 else "mes"
    rows, acc, tot, periods = profit_view(con, start, end, group)
    last = con.execute("SELECT MAX(posted) FROM fin_lineas").fetchone()[0]
    return render_template("rentabilidad.html", page="rentabilidad", rows=rows, acc=acc, tot=tot, periods=periods,
                           chart=json.dumps([dict(l=p["label"], p=round(p["producto"], 2), n=round(p["neto"], 2)) for p in periods]),
                           start=start, end=end, group=group, groups=GROUPS, preset=preset, presets=presets,
                           last=last, sync=_sync_state, error=request.args.get("error"))


@app.route("/rentabilidad/actualizar", methods=["POST"])
def rentabilidad_actualizar():
    if not _sync_state["running"]:
        def job():
            _sync_state.update(running=True, msg="Empezando…")
            try:
                rt.sync(progress=lambda m: _sync_state.update(msg=m))
                _sync_state["msg"] = ""
            except Exception as e:
                _sync_state["msg"] = f"Error: {e}"
            _sync_state["running"] = False
        Thread(target=job, daemon=True).start()
    back = request.form.get("volver") or ""
    return redirect(back if back.startswith("/rentabilidad") else url_for("rentabilidad"))


@app.route("/rentabilidad/estado")
def rentabilidad_estado():
    return jsonify(_sync_state)


@app.route("/rentabilidad/costes", methods=["GET", "POST"])
def costes():
    con = sn.db()
    if request.method == "POST":
        f = request.form
        sku = f["sku"].strip()
        coste = an.parse_money(f.get("coste"))
        if sku and coste is not None:
            prev = con.execute("SELECT asin, nombre FROM costes WHERE sku=? ORDER BY desde DESC LIMIT 1", (sku,)).fetchone() or ("", "")
            con.execute("INSERT OR REPLACE INTO costes(sku, desde, coste, envio_fba, iva, asin, nombre) VALUES (?,?,?,?,?,?,?)",
                        (sku, f.get("desde") or datetime.now(sn.TZ).date().isoformat(), coste,
                         an.parse_money(f.get("envio")) or 0.0, an.parse_money(f.get("iva")) or 21.0,
                         f.get("asin", "").strip() or prev[0], f.get("nombre", "").strip() or prev[1]))
            con.commit()
        return redirect(url_for("costes"))
    rows = rt.costs_table(con)
    latest = {}
    for sku, desde, coste, envio, iva, asin, nombre in rows:
        latest[sku] = dict(sku=sku, desde=desde, coste=coste, envio=envio, iva=iva, asin=asin, nombre=nombre,
                           historial=latest.get(sku, {}).get("historial", 0) + 1)
    sold = {r[0] for r in con.execute("SELECT DISTINCT sku FROM fin_lineas WHERE tipo='venta' AND sku IS NOT NULL")}
    missing = sorted(sold - set(latest))
    return render_template("costes.html", page="rentabilidad", costs=sorted(latest.values(), key=lambda c: c["sku"]),
                           missing=missing, today=datetime.now(sn.TZ).date().isoformat())


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
