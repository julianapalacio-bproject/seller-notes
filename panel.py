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

import requests
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


REFRESH_ERRORS = {  # lo que pasó al pulsar «Actualizar ahora» y qué hacer; el detalle técnico se queda en el panel, no en la URL
    "red": "No se pudo conectar con Amazon. Revisa la conexión a internet y vuelve a intentarlo.",
    "permiso": "Amazon ha rechazado la consulta por falta de permiso. Comprueba que la app tiene el rol "
               "«Publicación de productos» y que las claves del archivo .env son las actuales.",
    "claves": "Amazon no acepta las claves del archivo .env (Client ID, Client Secret o Refresh Token). "
              "Cópialas de nuevo desde Seller Central.",
    "limite": "Amazon ha frenado las consultas un momento por exceso de peticiones. Espera un minuto y vuelve a intentarlo.",
    "amazon": "Amazon ha fallado por su lado. Suele arreglarse solo: vuelve a intentarlo en unos minutos.",
    "otro": "Ha ocurrido un error inesperado. Vuelve a intentarlo; si se repite, guarda el detalle técnico.",
}
_refresh_detail = {"text": ""}


def refresh_error(e):
    """Clasifica una excepción de la consulta de listings en una de las claves de REFRESH_ERRORS."""
    if isinstance(e, (requests.ConnectionError, requests.Timeout)):
        return "red"
    status = getattr(getattr(e, "response", None), "status_code", None)
    if status is not None:
        if "api.amazon.com/auth" in str(getattr(e.response, "url", "")):
            return "claves"
        return {401: "permiso", 403: "permiso", 429: "limite"}.get(status, "amazon" if status >= 500 else "otro")
    if isinstance(e, KeyError) and str(e).strip("'").startswith("LWA_"):
        return "claves"
    return "otro"


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
    error = request.args.get("error")
    return render_template("listings.html", page="listings", ts=ts, rows=rows, tiles=tiles,
                           ignored=len(sn.ignored_skus()), has_seller_id=bool(sn.MY_SELLER_ID),
                           fresh=freshness(ts, 3 * 60, 24 * 60), updated=request.args.get("ok") == "1",
                           error=REFRESH_ERRORS.get(error) if error else None,
                           error_detail=_refresh_detail["text"] if error == "otro" else "")


@app.route("/listings/actualizar", methods=["POST"])
def refresh_listings():
    con = sn.db()
    before = latest_listings(con)[0]
    sn._listings_api["enabled"] = True  # un 403 anterior no debe bloquear el reintento
    try:
        sn.listings_status(con, quiet=True)
    except Exception as e:
        _refresh_detail["text"] = f"{type(e).__name__}: {e}"[:500]
        print(f"    (Actualizar listings: {_refresh_detail['text']})", flush=True)
        return redirect(url_for("listings", error=refresh_error(e)))
    if latest_listings(con)[0] == before:  # la consulta no guardó nada: el rol de listings no está activo
        return redirect(url_for("listings", error="permiso"))
    return redirect(url_for("listings", ok=1))


# ---------------------------------------------------------------- buy box
PERIODS = {"dia": ("Día", 1), "7d": ("7 días", 7), "30d": ("30 días", 30)}


def product_labels(con):
    """{asin: (sku, nombre)}. El nombre sale, por orden: de tu hoja de costes (el más corto), de tus listings
    o del título de la página de amazon.es (para los ASIN que no vendes)."""
    out = {}
    # con MAX() en un GROUP BY, SQLite devuelve las demás columnas de la fila más reciente de cada grupo
    for sql in ("SELECT asin, '', titulo FROM titulos",
                "SELECT asin, sku, nombre, MAX(ts) FROM listings GROUP BY asin",
                "SELECT asin, sku, nombre, MAX(desde) FROM costes GROUP BY sku"):
        try:
            rows = con.execute(sql).fetchall()
        except Exception:  # la tabla aún no existe
            continue
        for asin, sku, nombre, *_ in rows:
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


def buybox_seconds(snaps, end):
    """Segundos que cada vendedor tuvo la Buy Box, ponderado por tiempo (igual que el informe diario).
    snaps: [(ts, vendedor)] en orden."""
    share = defaultdict(float)
    now = datetime.now(sn.TZ)
    for cur, nxt in zip(snaps, snaps[1:] + [None]):
        t0 = datetime.fromisoformat(cur[0])
        t1 = datetime.fromisoformat(nxt[0]) if nxt else min(end, now)
        t1 = min(t1, t0 + timedelta(minutes=2 * sn.INTERVAL_MINUTES))  # huecos con el monitor apagado no cuentan
        share[cur[1]] += max((t1 - t0).total_seconds(), 0)
    return share


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

    share = buybox_seconds([(s[1], s[3]) for s in snaps], end)
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


# ---------------------------------------------------------------- dashboard: qué cambió y qué hacer
DIAS_STOCK_AVISO = 21  # avisar cuando el stock dure menos que esto al ritmo de venta de los últimos 30 días
DIAS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def minutes_since(ts):
    return (datetime.now(timezone.utc) - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds() / 60


def ago(minutes):
    if minutes < 1:
        return "ahora mismo"
    if minutes < 60:
        return f"hace {int(minutes)} min"
    if minutes < 48 * 60:
        return f"hace {int(minutes // 60)} h"
    return f"hace {int(minutes // 1440)} días"


def freshness(ts, warn, bad):
    """Tono ok/warn/bad según los minutos desde la última consulta."""
    if not ts:
        return dict(tone="bad", text="sin datos", ts=None)
    m = minutes_since(ts)
    return dict(tone="bad" if m > bad else "warn" if m > warn else "ok", text=ago(m), ts=ts)


def listing_since(con, sku, ok_sql):
    """Primera consulta de la racha actual en la que el SKU dejó de cumplir ok_sql."""
    last_ok = con.execute(f"SELECT MAX(ts) FROM listings WHERE sku=? AND {ok_sql}", (sku,)).fetchone()[0]
    if last_ok:
        return con.execute("SELECT MIN(ts) FROM listings WHERE sku=? AND ts>?", (sku, last_ok)).fetchone()[0]
    return con.execute("SELECT MIN(ts) FROM listings WHERE sku=?", (sku,)).fetchone()[0]


def buybox_lost_since(con, asin):
    """Primera consulta tras la última vez que tuviste la Buy Box de este ASIN."""
    last_mine = con.execute("SELECT MAX(ts) FROM snapshots WHERE asin=? AND bb_seller=?", (asin, sn.MY_SELLER_ID)).fetchone()[0]
    if not last_mine:
        return None
    return con.execute("SELECT MIN(ts) FROM snapshots WHERE asin=? AND ts>?", (asin, last_mine)).fetchone()[0]


def dashboard_buybox(con, names, labels):
    """Por cada ASIN vigilado: quién tiene ahora la Buy Box, si tienes oferta y tu % de hoy.
    Solo lee lo necesario (la última consulta y los ganadores de hoy), no el historial completo de Análisis Productos."""
    _, end, s_utc, e_utc = day_range(datetime.now(sn.TZ).date())
    me = sn.MY_SELLER_ID
    out = []
    for asin in sn.ASINS:
        sku, nombre = labels.get(asin, ("", ""))
        last = con.execute("SELECT id, bb_seller, bb_price FROM snapshots WHERE asin=? ORDER BY ts DESC, id DESC LIMIT 1",
                           (asin,)).fetchone()
        if not last:
            out.append(dict(asin=asin, sku=sku, nombre=nombre, has_data=False))
            continue
        mine = con.execute("SELECT landed FROM offers WHERE snapshot_id=? AND seller_id=?", (last[0], me)).fetchone() if me else None
        today = con.execute("SELECT ts, bb_seller FROM snapshots WHERE asin=? AND ts>=? AND ts<? ORDER BY ts",
                            (asin, s_utc, e_utc)).fetchall()
        secs = buybox_seconds(today, end)
        out.append(dict(asin=asin, sku=sku, nombre=nombre, has_data=True,
                        holder=names.get(last[1], last[1]) or "(nadie / suprimida)", is_me=bool(me) and last[1] == me,
                        nobody=not last[1], price=last[2], my_price=mine[0] if mine else None, selling=bool(mine),
                        share=100 * secs.get(me, 0) / (sum(secs.values()) or 1) if me else 0.0,
                        war=price_war(con, asin, names)))
    return out


def attention_items(con, rows, bb, money, fresh):
    """Lo que pide una acción hoy, de lo más grave a lo menos. Cada aviso dice desde cuándo y adónde ir."""
    items = []
    add = lambda tone, area, title, detail="", since=None, href="", cta="": items.append(
        dict(tone=tone, area=area, title=title, detail=detail, since=since, href=href, cta=cta))

    # el monitor tiene que estar en marcha para que todo lo demás sea cierto
    if fresh["buybox"]["tone"] != "ok":
        add(fresh["buybox"]["tone"], "Monitor", "El monitor no ha consultado la Buy Box " + fresh["buybox"]["text"],
            "Abre «2 - Iniciar monitor» para que la Buy Box y los listings se sigan actualizando.",
            fresh["buybox"]["ts"])
    if fresh["listings"]["tone"] != "ok":
        add("warn", "Listings", "La última consulta de tus listings fue " + fresh["listings"]["text"],
            "El monitor los consulta cada hora; también puedes actualizarlos desde Listings.",
            fresh["listings"]["ts"], url_for("listings"), "Ir a Listings")

    for b in bb:
        name = b["nombre"] or b["sku"] or b["asin"]
        href = url_for("buybox", asin=b["asin"])
        if b.get("selling") and b["nobody"]:
            add("warn", "Buy Box", f"Nadie tiene la Buy Box de {name}", "Amazon la ha suprimido: suele ser por precio alto.",
                None, href, "Ver producto")
        elif b.get("selling") and not b["is_me"]:
            add("bad", "Buy Box", f"Has perdido la Buy Box de {name}",
                f"La tiene {b['holder']} a {money(b['price'])}; tu oferta está a {money(b['my_price'])}. "
                f"Hoy la has tenido el {b['share']:.0f} % del tiempo.", buybox_lost_since(con, b["asin"]), href, "Ver producto")
        if b.get("war") and b["war"]["active"]:
            w = b["war"]
            detail = f"El precio de la Buy Box ha bajado un {w['drop']:.0f} % en 7 días." if w["drop"] >= 5 else ""
            bots = [f"{n.split(' [')[0]} ({c} cambios)" for n, c, me in w["bots"] if not me]
            if bots:
                detail += (" " if detail else "") + "Cambian de precio sin parar: " + ", ".join(bots) + "."
            add("warn", "Buy Box", f"Guerra de precios en {name}", detail, None, href, "Ver producto")

    for r in rows:
        name = r["nombre"] or r["sku"]
        href = url_for("listings", q=r["sku"])
        if r["estado"] != "ACTIVO":
            add("warn" if r["estado"] == "VISIBLE, NO COMPRABLE" and r["stock"] != 0 else "bad", "Listings",
                f"{r['sku']}: {r['estado'].lower()}" + (" y sin stock" if r["stock"] == 0 else ""), name,
                listing_since(con, r["sku"], "estado='ACTIVO'"), href, "Ver listing")
        elif r["stock"] == 0:
            add("bad", "Listings", f"{r['sku']}: sin stock", name,
                listing_since(con, r["sku"], "stock>0"), href, "Ver listing")
        if r["errores"]:
            add("bad", "Listings", f"{r['sku']}: {r['errores']} error{'es' if r['errores'] > 1 else ''} en el listing",
                r["lista_incidencias"][0] if r["lista_incidencias"] else name, None, href, "Ver incidencias")
        if r["stock"] and r.get("dias") is not None and r["dias"] < DIAS_STOCK_AVISO:
            add("warn", "Listings", f"{r['sku']}: stock para unos {r['dias']:.0f} días",
                f"{r['stock']} uds; vendes unas {str(round(r['ventas_dia'], 1)).replace('.', ',')} al día "
                "(media de los últimos 30 días).", None, href, "Ver listing")
    return items


# ---------------------------------------------------------------- Profit: tarjetas de periodos (como Sellerboard)
TILE_OPTIONS = {
    "hoy": "Hoy", "ayer": "Ayer", "7d": "Últimos 7 días", "esta_semana": "Esta semana", "semana_pasada": "Semana pasada",
    "30d": "Últimos 30 días", "este_mes": "Este mes", "prevision": "Previsión del mes", "mes_pasado": "Mes pasado",
    "todo": "Desde el inicio",
}
TILES_DEFAULT = ["hoy", "ayer", "7d", "este_mes", "mes_pasado", "prevision"]
TILES_MAX = 6
ADS_LABEL = rt.ACCOUNT_LABELS["ProductAds"]


def tile_range(key, today):
    """(desde, hasta) de una tarjeta; la previsión abarca el mes entero."""
    first = today.replace(day=1)
    monday = today - timedelta(days=today.weekday())
    if key == "esta_semana":
        return monday, today
    if key == "semana_pasada":
        return monday - timedelta(days=7), monday - timedelta(days=1)
    if key == "prevision":
        return first, (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return date_presets(today)[key][1:]


def tiles_config(con):
    con.execute("CREATE TABLE IF NOT EXISTS ajustes (clave TEXT PRIMARY KEY, valor TEXT)")
    r = con.execute("SELECT valor FROM ajustes WHERE clave='tarjetas'").fetchone()
    keys = [k for k in json.loads(r[0]) if k in TILE_OPTIONS] if r else []
    return keys or TILES_DEFAULT


def period_tiles(con, today):
    """Una tarjeta por periodo elegido: ventas, pedidos, devoluciones, publicidad, gastos y beneficio neto."""
    keys = tiles_config(con)
    ranges = {k: tile_range(k, today) for k in keys}
    start = min([a for a, _ in ranges.values()] + [today - timedelta(days=30)])
    skus, account = rt.aggregate(con, start, today, "dia")
    orders = defaultdict(int)
    for compra, in con.execute("SELECT compra FROM pedidos WHERE estado != 'Canceled' AND compra >= ?",
                               ((start - timedelta(days=1)).isoformat(),)):
        orders[rt.local_date(compra).isoformat()] += 1

    def total(a, b):
        t = defaultdict(float)
        d = a
        while d <= b:
            k = d.isoformat()
            for r in skus.get(k, {}).values():
                for f in ("ventas", "devoluciones", "ingresos", "comisiones", "fba", "reembolsos", "coste", "beneficio",
                          "uds", "devueltas", "estimadas"):
                    t[f] += r[f]
            for label, v in account.get(k, {}).items():
                t["publicidad" if label == ADS_LABEL else "otros"] += v
            t["pedidos"] += orders.get(k, 0)
            d += timedelta(days=1)
        return t

    tiles = []
    for k in keys:
        a, b = ranges[k]
        if k == "prevision":  # lo que va de mes hasta ayer + el ritmo diario de los últimos 30 días cerrados
            t, rate = total(a, today - timedelta(days=1)), total(today - timedelta(days=30), today - timedelta(days=1))
            left = (b - today).days + 1
            for f, v in rate.items():
                t[f] += v / 30 * left
        else:
            t = total(a, b)
        t["iva"] = t["ventas"] + t["devoluciones"] - t["ingresos"]
        t["amazon"] = t["comisiones"] + t["fba"]
        t["neto"] = t["beneficio"] + t["publicidad"] + t["otros"]
        t["margen"] = 100 * t["neto"] / t["ingresos"] if t["ingresos"] else None
        t["roi"] = 100 * t["neto"] / t["coste"] if t["coste"] > 0 else None
        fmt = lambda d: d.strftime("%d/%m")
        tiles.append(dict(key=k, label=TILE_OPTIONS[k], t=t, forecast=k == "prevision",
                          dates=fmt(a) if a == b else f"{fmt(a)} – {fmt(b)}",
                          href=url_for("rentabilidad", desde=a.isoformat(), hasta=min(b, today).isoformat())))
    return tiles


@app.route("/rentabilidad/tarjetas", methods=["POST"])
def guardar_tarjetas():
    con = sn.db()
    tiles_config(con)
    keys = [request.form.get(f"t{i}", "") for i in range(TILES_MAX)]
    keys = [k for k in keys if k in TILE_OPTIONS] or TILES_DEFAULT
    with con:
        con.execute("INSERT OR REPLACE INTO ajustes VALUES ('tarjetas', ?)", (json.dumps(keys),))
    return redirect(url_for("rentabilidad"))


@app.route("/")
def home():
    sn.ASINS[:] = sn.read_asins()
    con = sn.db()
    rt.ensure_tables(con)
    names = sn.seller_names()
    labels = product_labels(con)
    ts, rows = latest_listings(con)
    today = datetime.now(sn.TZ).date()
    now = datetime.now(sn.TZ)
    eur = lambda v: "—" if v is None else f"{v:,.2f}".replace(",", " ").replace(".", ",").replace(" ", ".") + " €"

    # dinero: los últimos 30 días por día (también sirve para el ritmo de venta de cada SKU)
    first = min(today - timedelta(days=29), today.replace(day=1))
    prows, _, _, periods = profit_view(con, first, today, "dia")
    sold = {r["sku"]: r["uds"] for r in prows}
    for r in rows:
        per_day = sold.get(r["sku"], 0) / ((today - first).days + 1)
        r["ventas_dia"] = per_day
        r["dias"] = r["stock"] / per_day if per_day and r["stock"] is not None else None
    by_day = {p["key"]: p for p in periods}
    day = lambda d: by_day.get(d.isoformat(), {"neto": 0.0, "uds": 0})
    yesterday = day(today - timedelta(days=1))
    profit = dict(
        hoy=day(today), ayer=yesterday,
        semana=sum(day(today - timedelta(days=i))["neto"] for i in range(1, 8)),
        semana_antes=sum(day(today - timedelta(days=i))["neto"] for i in range(8, 15)),
        mes=sum(p["neto"] for p in periods if p["key"] >= today.replace(day=1).isoformat()),
        dias=[dict(label=DIAS_ES[(today - timedelta(days=i)).weekday()][:2].capitalize(),
                   fecha=(today - timedelta(days=i)), neto=day(today - timedelta(days=i))["neto"],
                   uds=day(today - timedelta(days=i))["uds"]) for i in range(13, -1, -1)],
        sin_coste=sorted(r["sku"] for r in prows if r["sin_coste"]),
        estimadas=sum(r["estimadas"] for r in prows))

    bb_last = con.execute("SELECT MAX(ts) FROM snapshots").fetchone()[0]
    fin_last = con.execute("SELECT MAX(posted) FROM fin_lineas").fetchone()[0]
    fresh = dict(listings=freshness(ts, 3 * 60, 24 * 60),
                 buybox=freshness(bb_last, 30, 6 * 60),
                 dinero=freshness(fin_last, 2 * 1440, 4 * 1440))
    bb = dashboard_buybox(con, names, labels)
    items = attention_items(con, rows, bb, eur, fresh)
    if yesterday["neto"] < 0:
        items.append(dict(tone="warn", area="Profit", title=f"Ayer perdiste dinero: {eur(yesterday['neto'])}",
                          detail="Mira qué productos o gastos de la cuenta lo causaron.", since=None,
                          href=url_for("rentabilidad", rango="ayer"), cta="Ver ayer"))
    if profit["sin_coste"]:
        n = len(profit["sin_coste"])
        items.append(dict(tone="warn", area="Profit", title=f"{n} SKU vendido{'s' if n > 1 else ''} sin coste de producto",
                          detail=", ".join(profit["sin_coste"]) + ". El beneficio sale más alto de lo real.", since=None,
                          href=url_for("costes"), cta="Poner costes"))
    items.sort(key=lambda i: {"bad": 0, "warn": 1}.get(i["tone"], 2))

    count = defaultdict(int)
    for r in rows:
        count[r["estado"]] += 1
    health = dict(total=len(rows), estados=[(e, count[e], tone) for e, tone in (
        ("ACTIVO", "ok"), ("VISIBLE, NO COMPRABLE", "warn"), ("INACTIVO", "bad"), ("SUPRIMIDO", "bad")) if count[e]],
        sin_stock=sum(1 for r in rows if r["stock"] == 0), unidades=sum(r["stock"] or 0 for r in rows),
        con_errores=sum(1 for r in rows if r["errores"]))
    return render_template("dashboard.html", page="dashboard", items=items, fresh=fresh, bb=bb, profit=profit,
                           health=health, eur=eur,
                           fecha=f"{DIAS_ES[now.weekday()].capitalize()} {now.day} de {MONTHS_ES[now.month].lower()} · {now:%H:%M}")


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


CATALOG_PAGE = 100  # filas por página: un catálogo de miles de productos no cabe de una vez en el navegador
CATALOG_SORTS = {  # columna de la tabla → valor por el que se ordena (None va siempre al final)
    "producto": lambda it: (it.get("nombre") or it.get("titulo") or "").lower() or None,
    "bsr": lambda it: it.get("bsr"), "ofertas": lambda it: it.get("ofertas"), "precio": lambda it: it.get("precio"),
    "coste": lambda it: it.get("coste"), "comisiones": lambda it: it.get("comisiones"),
    "beneficio": lambda it: it.get("beneficio"), "roi": lambda it: it.get("roi"), "margen": lambda it: it.get("margen"),
}


def number_arg(name):
    try:
        return float(request.args.get(name, "").replace(",", "."))
    except ValueError:
        return None


@app.route("/catalogos/<int:job_id>")
def catalogo(job_id):
    job, items = load_job(job_id)
    if not job:
        return redirect(url_for("catalogos"))
    found = [it for it in items if it.get("asin")]
    roi_ok = [it for it in found if (it.get("roi") or -999) >= 30]
    tiles = [("productos", len(items), ""), ("en Amazon.es", len(found), ""), ("con ROI ≥ 30 %", len(roi_ok), "ok"),
             ("los vende Amazon", sum(1 for it in found if it.get("amazon_vende")), "bad")]

    # filtros y orden en el servidor; «f» marca que el formulario se envió (si no, «solo en Amazon» va marcado)
    a = request.args
    f = dict(roi=number_arg("roi"), bsr=number_arg("bsr"),
             en_amazon="en_amazon" in a if "f" in a else True, sin_amazon="sin_amazon" in a)
    rows = [it for it in items
            if (f["roi"] is None or (it.get("roi") is not None and it["roi"] >= f["roi"]))
            and (f["bsr"] is None or (it.get("bsr") and it["bsr"] <= f["bsr"]))
            and (not f["en_amazon"] or it.get("asin")) and (not f["sin_amazon"] or not it.get("amazon_vende"))]
    sort = a.get("orden") if a.get("orden") in CATALOG_SORTS else "roi"
    desc = a.get("dir", "desc" if sort != "producto" else "asc") == "desc"
    key = CATALOG_SORTS[sort]
    rows = sorted((it for it in rows if key(it) is not None), key=key, reverse=desc) + [it for it in rows if key(it) is None]

    pages = max(1, -(-len(rows) // CATALOG_PAGE))
    try:
        page = min(max(1, int(a.get("pagina", 1))), pages)
    except ValueError:
        page = 1
    keep = {k: v for k, v in a.items() if k != "pagina"}
    link = lambda **kw: url_for("catalogo", job_id=job_id, **{**keep, **kw})
    return render_template("catalogo.html", page="catalogos", job=job, tiles=tiles, f=f, sort=sort, desc=desc,
                           items=rows[(page - 1) * CATALOG_PAGE: page * CATALOG_PAGE], matched=len(rows), total=len(items),
                           pagina=page, paginas=pages, first=(page - 1) * CATALOG_PAGE + 1, link=link)


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
                           last=last, sync=_sync_state, error=request.args.get("error"),
                           tiles=period_tiles(con, today), tile_options=TILE_OPTIONS, tiles_max=TILES_MAX)


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
        desde = f.get("desde") or datetime.now(sn.TZ).date().isoformat()
        if f.get("borrar"):  # quitar un tramo de coste (nunca el único del SKU)
            if con.execute("SELECT COUNT(*) FROM costes WHERE sku=?", (sku,)).fetchone()[0] > 1:
                con.execute("DELETE FROM costes WHERE sku=? AND desde=?", (sku, desde))
                con.commit()
            return redirect(url_for("costes"))
        if sku and coste is not None:
            prev = con.execute("SELECT asin, nombre FROM costes WHERE sku=? ORDER BY desde DESC LIMIT 1", (sku,)).fetchone() or ("", "")
            iva, envio = an.parse_money(f.get("iva")), an.parse_money(f.get("envio"))
            con.execute("INSERT OR REPLACE INTO costes(sku, desde, coste, envio_fba, iva, asin, nombre) VALUES (?,?,?,?,?,?,?)",
                        (sku, desde, coste, envio or 0.0, 21.0 if iva is None else iva,
                         f.get("asin", "").strip() or prev[0], f.get("nombre", "").strip() or prev[1]))
            con.commit()
            return redirect(url_for("costes", ok=sku, desde=desde, coste=coste))
        return redirect(url_for("costes"))
    latest = {}
    for sku, desde, coste, envio, iva, asin, nombre in rt.costs_table(con):
        hist = latest.get(sku, {}).get("historial", []) + [dict(desde=desde, coste=coste)]
        latest[sku] = dict(sku=sku, desde=desde, coste=coste, envio=envio, iva=iva, asin=asin, nombre=nombre, historial=hist)
    sold = {r[0] for r in con.execute("SELECT DISTINCT sku FROM fin_lineas WHERE tipo='venta' AND sku IS NOT NULL")}
    missing = sorted(sold - set(latest))
    a = request.args
    saved = dict(sku=a["ok"], desde=a.get("desde", ""), coste=float(a.get("coste", 0))) if a.get("ok") else None
    return render_template("costes.html", page="rentabilidad", costs=sorted(latest.values(), key=lambda c: c["sku"]),
                           missing=missing, today=datetime.now(sn.TZ).date().isoformat(), saved=saved)


def mark_interrupted_jobs():
    """Si el panel se cerró a mitad de un análisis, ese análisis no va a terminar."""
    con = sn.db()
    an.ensure_tables(con)
    con.execute("UPDATE catalogos SET estado='error', mensaje='Interrumpido: el panel se cerró durante el análisis' "
                "WHERE estado='en_curso'")
    con.commit()


if __name__ == "__main__":
    if "--puerto" in sys.argv:  # otro puerto para probar cambios sin cerrar el panel abierto
        PORT = int(sys.argv[sys.argv.index("--puerto") + 1])
    mark_interrupted_jobs()
    if "--no-abrir" not in sys.argv:
        Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    print(f"Seller Notes · panel en http://127.0.0.1:{PORT}  (Ctrl+C para cerrar)")
    app.run(host="127.0.0.1", port=PORT, debug=False)

