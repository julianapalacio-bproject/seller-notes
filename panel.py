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
import sqlite3
import sys
import time
import webbrowser
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Thread, Timer

import requests
from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

import analizador as an
import rentabilidad as rt
import repricer as rp
import informes as inf
import resenas as rs
import seller_notes as sn

PORT = 5050
app = Flask(__name__)

DIAS_STOCK_AVISO = 21  # avisar cuando el stock dure menos que esto al ritmo de venta de los últimos 30 días
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


# ---------------------------------------------------------------- inicio de sesión
# Usuarios en usuarios.json ({"usuario": "hash de la contraseña"}, fuera del historial). Sin ese archivo
# (el panel en tu Mac) no se pide contraseña. La sesión dura 30 días, también en la app de la pantalla de inicio.
USERS_FILE = sn.BASE / "usuarios.json"
USERS = json.loads(USERS_FILE.read_text()) if USERS_FILE.exists() else {}
SECRET_FILE = sn.BASE / ".panel_secret"
if not SECRET_FILE.exists():
    SECRET_FILE.write_text(os.urandom(32).hex())
    SECRET_FILE.chmod(0o600)
app.secret_key = SECRET_FILE.read_text().strip()
app.config.update(PERMANENT_SESSION_LIFETIME=timedelta(days=30), SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=bool(USERS))  # en el servidor solo se entra por https
PUBLIC = {"login", "static", "manifest", "service_worker", "sin_conexion"}
MAX_FAILS, LOCK_SECONDS = 5, 15 * 60
_failed = {}  # usuario → horas de los intentos fallidos recientes


@app.before_request
def require_login():
    if not USERS or request.endpoint in PUBLIC or session.get("usuario") in USERS:
        return None
    if request.method == "GET" and request.accept_mimetypes.accept_html:
        return redirect(url_for("login", next=request.full_path.rstrip("?")))
    return jsonify(error="Sesión caducada: vuelve a entrar"), 401


@app.route("/entrar", methods=["GET", "POST"])
def login():
    nxt = request.values.get("next") or ""
    nxt = nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("rentabilidad")
    error = None
    if request.method == "POST":
        user = request.form.get("usuario", "").strip()
        match = next((u for u in USERS if u.lower() == user.lower()), None)
        fails = [t for t in _failed.get(match, []) if time.time() - t < LOCK_SECONDS]
        if len(fails) >= MAX_FAILS:  # contraseñas cortas: tras 5 fallos, ese usuario espera 15 min
            error = f"Demasiados intentos. Prueba de nuevo dentro de {int(LOCK_SECONDS - (time.time() - fails[0])) // 60 + 1} min"
        elif match and check_password_hash(USERS[match], request.form.get("contrasena", "")):
            _failed.pop(match, None)
            session.clear()
            session.permanent = True
            session["usuario"] = match
            return redirect(nxt)
        else:
            _failed[match] = fails + [time.time()]
            time.sleep(1)  # frena a quien pruebe contraseñas a ciegas
            error = "Usuario o contraseña incorrectos"
    return render_template("login.html", error=error, next=nxt, usuario=request.form.get("usuario", ""))


@app.route("/salir", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------- mis listings
def add_stock_cover(rows, profit_rows, days):
    """Pone en cada listing ventas_dia (unidades al día en el periodo) y dias (cuántos días dura su stock a ese ritmo)."""
    sold = {r["sku"]: r["uds"] for r in profit_rows}
    for r in rows:
        r["ventas_dia"] = sold.get(r["sku"], 0) / days
        r["dias"] = r["stock"] / r["ventas_dia"] if r["ventas_dia"] > 0 and r["stock"] is not None else None
        r["stock_bajo"] = bool(r["stock"]) and r["dias"] is not None and r["dias"] < DIAS_STOCK_AVISO


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
    today = datetime.now(sn.TZ).date()
    add_stock_cover(rows, profit_view(con, today - timedelta(days=29), today, "mes")[0], 30)
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
                           fresh=freshness(ts, 3 * 60, 24 * 60), updated=request.args.get("ok") == "1", dias_aviso=DIAS_STOCK_AVISO,
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


MARKET_PERIODS = {"24h": ("Últimas 24 h", timedelta(hours=24)), "7d": ("Últimos 7 días", timedelta(days=7))}
PRICE_ALERT_PCT = 10  # bajada del precio de la Buy Box en 24 h que merece aviso


def market_moves(con, names, labels, period, bb):
    """Por cada ASIN vigilado, qué ha cambiado en el periodo: precio de la Buy Box (y el tuyo), vendedores que
    entran y salen, cuántos cambios de precio hacen los demás, y si hay señales de guerra de precios."""
    since = datetime.now(timezone.utc) - period
    me = sn.MY_SELLER_ID
    war_of = {b["asin"]: b.get("war") for b in bb}
    out = []
    for asin in sn.ASINS:
        last = con.execute("SELECT id, ts, bb_price FROM snapshots WHERE asin=? ORDER BY ts DESC, id DESC LIMIT 1",
                           (asin,)).fetchone()
        if not last:
            continue
        # la foto de entonces: la última antes del inicio del periodo (o la primera si el ASIN es más nuevo)
        then = (con.execute("SELECT id, ts, bb_price FROM snapshots WHERE asin=? AND ts<=? ORDER BY ts DESC, id DESC LIMIT 1",
                            (asin, since.isoformat())).fetchone()
                or con.execute("SELECT id, ts, bb_price FROM snapshots WHERE asin=? ORDER BY ts, id LIMIT 1", (asin,)).fetchone())
        offers = lambda sid: dict(con.execute("SELECT seller_id, landed FROM offers WHERE snapshot_id=?", (sid,)).fetchall())
        now_o, then_o = offers(last[0]), offers(then[0])
        lo, hi = con.execute("SELECT MIN(bb_price), MAX(bb_price) FROM snapshots WHERE asin=? AND ts>=? AND bb_price IS NOT NULL",
                             (asin, since.isoformat())).fetchone()
        changes = con.execute("SELECT COUNT(*) FROM events WHERE asin=? AND type='cambio_precio' AND ts>=? AND seller_id!=?",
                              (asin, since.isoformat(), me or "")).fetchone()[0]
        who = lambda s: (names.get(s) or s).split(" [")[0]
        price, before = last[2], then[2]
        sku, nombre = labels.get(asin, ("", ""))
        out.append(dict(
            asin=asin, sku=sku, nombre=nombre or asin, desde=then[1], selling=me in now_o,
            precio=price, antes=before, lo=lo, hi=hi,
            pct=100 * (price - before) / before if price and before else None,
            mio=now_o.get(me), mio_antes=then_o.get(me),
            entran=sorted((who(s), now_o[s]) for s in now_o.keys() - then_o.keys() if s != me),
            salen=sorted(who(s) for s in then_o.keys() - now_o.keys() if s != me),
            vendedores=len(now_o), vendedores_antes=len(then_o), cambios=changes, guerra=war_of.get(asin),
            bots=[(n.split(" [")[0], c) for n, c, mine in (war_of.get(asin) or {}).get("bots", []) if not mine]))
    # primero lo que más se ha movido
    out.sort(key=lambda m: -(abs(m["pct"] or 0) + 5 * (len(m["entran"]) + len(m["salen"])) + (20 if (m["guerra"] or {}).get("active") else 0)))
    return out


def attention_items(con, rows, bb, money, fresh):
    """Lo que pide una acción hoy, de lo más grave a lo menos. Cada aviso dice desde cuándo y adónde ir."""
    items = []
    add = lambda tone, area, title, detail="", since=None, href="", cta="", asin=None: items.append(
        dict(tone=tone, area=area, title=title, detail=detail, since=since, href=href, cta=cta, asin=asin))

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
                None, href, "Ver producto", b["asin"])
        elif b.get("selling") and not b["is_me"]:
            add("bad", "Buy Box", f"Has perdido la Buy Box de {name}",
                f"La tiene {b['holder']} a {money(b['price'])}; tu oferta está a {money(b['my_price'])}. "
                f"Hoy la has tenido el {b['share']:.0f} % del tiempo.", buybox_lost_since(con, b["asin"]), href, "Ver producto",
                b["asin"])
        if b.get("war") and b["war"]["active"]:
            w = b["war"]
            detail = f"El precio de la Buy Box ha bajado un {w['drop']:.0f} % en 7 días." if w["drop"] >= 5 else ""
            bots = [f"{n.split(' [')[0]} ({c} cambios)" for n, c, me in w["bots"] if not me]
            if bots:
                detail += (" " if detail else "") + "Cambian de precio sin parar: " + ", ".join(bots) + "."
            add("warn", "Buy Box", f"Guerra de precios en {name}", detail, None, href, "Ver producto", b["asin"])

    for r in rows:
        name = r["nombre"] or r["sku"]
        href = url_for("listings", q=r["sku"])
        if r["estado"] != "ACTIVO":
            add("warn" if r["estado"] == "VISIBLE, NO COMPRABLE" and r["stock"] != 0 else "bad", "Listings",
                f"{r['sku']}: {r['estado'].lower()}" + (" y sin stock" if r["stock"] == 0 else ""), name,
                listing_since(con, r["sku"], "estado='ACTIVO'"), href, "Ver listing", r["asin"])
        elif r["stock"] == 0:
            add("bad", "Listings", f"{r['sku']}: sin stock", name,
                listing_since(con, r["sku"], "stock>0"), href, "Ver listing", r["asin"])
        if r["errores"]:
            add("bad", "Listings", f"{r['sku']}: {r['errores']} error{'es' if r['errores'] > 1 else ''} en el listing",
                r["lista_incidencias"][0] if r["lista_incidencias"] else name, None, href, "Ver incidencias", r["asin"])
        if r["stock_bajo"]:
            add("warn", "Listings", f"{r['sku']}: stock para unos {r['dias']:.0f} días",
                f"{r['stock']} uds; vendes unas {str(round(r['ventas_dia'], 1)).replace('.', ',')} al día "
                "(media de los últimos 30 días).", None, href, "Ver listing", r["asin"])
    return items


def attention_groups(items, labels, rows):
    """Los avisos agrupados por ASIN (los de la cuenta, sin ASIN, juntos), del grupo más grave al menos."""
    names = {a: (sku, nombre) for a, (sku, nombre) in labels.items()}
    for r in rows:
        if r["asin"] and not (names.get(r["asin"]) or ("", ""))[1]:
            names[r["asin"]] = (r["sku"], r["nombre"])
    groups = {}
    for i in items:
        asin = i.get("asin")
        sku, nombre = names.get(asin, ("", "")) if asin else ("", "Tu cuenta")
        g = groups.setdefault(asin, dict(asin=asin, sku=sku, nombre=nombre or asin, items=[], areas={}))
        g["items"].append(i)
        g["areas"][i["area"]] = g["areas"].get(i["area"], 0) + 1
    for g in groups.values():
        g["n_bad"] = sum(1 for i in g["items"] if i["tone"] == "bad")
        g["tone"] = "bad" if g["n_bad"] else "warn"
    return sorted(groups.values(), key=lambda g: (-g["n_bad"], -len(g["items"]), g["nombre"]))


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


def _month_back(d, n=1):
    """La misma fecha n meses antes (o el último día de ese mes si no existe)."""
    y, m = d.year, d.month - n
    while m < 1:
        y, m = y - 1, m + 12
    last = ((date(y, m, 1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)).day
    return date(y, m, min(d.day, last))


def prev_range(a, b):
    """El periodo anterior equivalente: meses enteros → los meses de antes; un mes empezado → los mismos días del
    mes anterior; cualquier otro rango → los mismos días justo antes."""
    month_end = lambda d: (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    if a.day == 1 and b == month_end(b):
        n = (b.year - a.year) * 12 + b.month - a.month + 1
        return _month_back(a, n), month_end(_month_back(b, n))
    if a.day == 1 and a.month == b.month and a.year == b.year:
        return _month_back(a), _month_back(b)
    days = (b - a).days + 1
    return a - timedelta(days=days), a - timedelta(days=1)


def tile_prev(key, a, b, today):
    """Con qué se compara cada tarjeta (None = sin comparación: hoy va a medias y «desde el inicio» no tiene antes)."""
    if key in ("hoy", "todo"):
        return None
    if key == "prevision":
        first = today.replace(day=1)
        pa, pb = _month_back(first), first - timedelta(days=1)
    else:
        pa, pb = prev_range(a, b)
    return (pa, pb) if pa >= datetime.strptime(rt.DEFAULT_FROM, "%Y-%m-%d").date() else None


def change(now, before, pct=True):
    """Diferencia frente al periodo anterior: % para ventas y pedidos; en € para el beneficio (puede ser negativo)."""
    if before is None:
        return None
    if not pct:
        return dict(diff=now - before, up=now >= before)
    if abs(before) < 0.005:
        return None
    return dict(pct=100 * (now - before) / abs(before), up=now >= before)


def tiles_config(con):
    con.execute("CREATE TABLE IF NOT EXISTS ajustes (clave TEXT PRIMARY KEY, valor TEXT)")
    r = con.execute("SELECT valor FROM ajustes WHERE clave='tarjetas'").fetchone()
    keys = [k for k in json.loads(r[0]) if k in TILE_OPTIONS] if r else []
    return keys or TILES_DEFAULT


def period_tiles(con, today):
    """Una tarjeta por periodo elegido: ventas, pedidos, devoluciones, publicidad, gastos y beneficio neto."""
    keys = tiles_config(con)
    ranges = {k: tile_range(k, today) for k in keys}
    prevs = {k: tile_prev(k, *ranges[k], today) for k in keys}
    start = min([a for a, _ in ranges.values()] + [p[0] for p in prevs.values() if p] + [today - timedelta(days=30)])
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
        cmp = None
        if prevs[k]:
            pa, pb = prevs[k]
            p = total(pa, pb)
            p_neto = p["beneficio"] + p["publicidad"] + p["otros"]
            cmp = dict(dates=fmt(pa) if pa == pb else f"{fmt(pa)} – {fmt(pb)}",
                       ventas=change(t["ventas"], p["ventas"]), neto=change(t["neto"], p_neto, pct=False),
                       neto_antes=p_neto)
        tiles.append(dict(key=k, label=TILE_OPTIONS[k], t=t, forecast=k == "prevision", cmp=cmp,
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


@app.route("/avisos")
def avisos():
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
    add_stock_cover(rows, prows, (today - first).days + 1)
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
    asin_of = {r["sku"]: r["asin"] for r in rows}
    for it in refunds_view(con, first, today, prows)["productos"]:
        if not it["tone"]:
            continue
        motivo = f" Motivo más repetido: {it['motivos'][0][0].lower()} ({it['motivos'][0][1]})." if it["motivos"] else ""
        items.append(dict(tone=it["tone"], area="Reembolsos",
                          title=f"{it['sku']}: {it['devueltas']} reembolsos en 30 días ({it['tasa']:.0f} % de lo vendido)",
                          detail=f"{it['nombre'][:60]}. Restan {eur(-it['efecto'])} a tu beneficio.{motivo}".lstrip(". "),
                          since=None, href=url_for("rentabilidad", rango="30d", _anchor="reembolsos"),
                          cta="Ver reembolsos", asin=asin_of.get(it["sku"])))
    mb = money_back(con, today)
    if mb["reclamar"]:
        n = len(mb["reclamar"])
        items.append(dict(tone="warn", area="Money Back",
                          title=f"{n} devolucion{'es' if n > 1 else ''} no recibida{'s' if n > 1 else ''}: puedes reclamar unos {eur(mb['total_reclamar'])}",
                          detail="Amazon reembolsó al cliente hace más de 45 días, el producto no ha vuelto al almacén y no te ha compensado.",
                          since=None, href=url_for("money_back_page"), cta="Ver qué reclamar"))
    roles = missing_roles(con)
    if roles:
        que = " y ".join({"envios": "los envíos FBA", "valoraciones": "las valoraciones"}.get(k, k) for k in roles)
        items.append(dict(tone="warn", area="Amazon", title=f"Tu app de Amazon no deja leer {que}",
                          detail="; ".join(roles.values()) + ". Añade el rol en Seller Central → Aplicaciones y servicios → "
                                 "Desarrollar aplicaciones, y vuelve a autorizar la app.",
                          since=None, href=url_for("envios"), cta="Ver qué falta"))
    for e in shipments_view(con):
        if e["reclamar"]:
            items.append(dict(tone="warn", area="Envíos FBA",
                              title=f"Faltan {e['faltan']} uds en el envío {e['id']}",
                              detail=f"Enviaste {e['enviadas']} y Amazon recibió {e['recibidas']} (unos {eur(e['valor'])} de coste). "
                                     "Puedes abrir un caso de unidades no recibidas en Seller Central.",
                              since=None, href=url_for("envios", _anchor="e-" + e["id"]), cta="Ver envío"))
    for pedido, fecha, nota, comentario in negative_feedback(con, today):
        items.append(dict(tone="bad" if nota <= 2 else "warn", area="Valoraciones",
                          title=f"Valoración de {nota} estrella{'s' if nota != 1 else ''} en el pedido {pedido}",
                          detail=(comentario[:160] + " · " if comentario else "") +
                                 "Puedes responder o pedir a Amazon que la retire si habla del producto o del envío de FBA.",
                          since=None, href="https://sellercentral.amazon.es/feedback-manager/index.html", cta="Abrir en Seller Central"))
    name_of = {r["sku"]: r["nombre"] or r["sku"] for r in rows}
    for sku, c in rp.settings(con).items():
        if c["activo"] and c["regla_amazon"]:
            items.append(dict(tone="bad", area="Repricer",
                              title=f"{sku}: el repricer no puede cambiar el precio por una regla de Amazon",
                              detail="Quita este SKU de la regla en Seller Central (Precios → Automatizar precios); "
                                     "si no, Amazon y Seller Notes se pelean por el precio.",
                              since=None, href=url_for("repricer", _anchor="p-" + sku), cta="Ver repricer",
                              asin=c["asin"] or asin_of.get(sku)))
    for f in fee_changes(con, today):
        if f["tipo"] == "fba":
            title = f"{f['sku']}: la tarifa FBA ha subido de {eur(f['antes'])} a {eur(f['ahora'])} por unidad"
            detail = ("Si el producto no ha cambiado, suele ser un error de medidas o peso en el almacén de Amazon: "
                      "pide en Seller Central que lo vuelvan a medir y que te devuelvan la diferencia.")
        else:
            pct = lambda v: f"{v:g}".replace(".", ",") + " %"
            title = f"{f['sku']}: la comisión ha subido del {pct(f['antes'])} al {pct(f['ahora'])}"
            detail = "Amazon ha podido cambiar la categoría del producto. Revisa la categoría en Seller Central."
        items.append(dict(tone="warn", area="Tarifas", title=title,
                          detail=f"{name_of.get(f['sku'], '')[:60]}. {detail}".lstrip(". "), since=f["desde"],
                          href=url_for("rentabilidad", rango="30d"), cta="Ver Profit", asin=asin_of.get(f["sku"])))
    my_asins = sorted({r["asin"] for r in rows if r["asin"]})
    sku_of = {r["asin"]: r["sku"] for r in rows}
    for asin, sellers in new_sellers(con, my_asins).items():
        name = (labels.get(asin, (None, None))[1] or sku_of.get(asin) or asin)[:45]
        def who(s):
            return (names.get(s) or s).split(" [")[0]
        def one(s, first, price):
            return f"{who(s)} llegó el {local(first, '%d/%m a las %H:%M')} y está a {eur(price)}"
        sellers = [x for x in sellers if x[2] is not None]  # solo los que siguen en la ficha
        if not sellers:
            continue
        items.append(dict(tone="warn", area="Buy Box",
                          title=(f"Nuevo vendedor en {name}: {who(sellers[0][0])}" if len(sellers) == 1
                                 else f"{len(sellers)} vendedores nuevos en {name}"),
                          detail=". ".join(one(*x) for x in sellers) + ".",
                          since=sellers[0][1], href=url_for("buybox", asin=asin), cta="Ver producto", asin=asin))
    periodo = request.args.get("mercado") if request.args.get("mercado") in MARKET_PERIODS else "24h"
    moves = market_moves(con, names, labels, MARKET_PERIODS[periodo][1], bb)
    for m in (moves if periodo == "24h" else market_moves(con, names, labels, MARKET_PERIODS["24h"][1], bb)):
        if m["selling"] and m["pct"] is not None and m["pct"] <= -PRICE_ALERT_PCT:
            items.append(dict(tone="warn", area="Precio",
                              title=f"El precio de la Buy Box de {m['nombre'][:45]} ha bajado un {-m['pct']:.0f} % en 24 h",
                              detail=f"De {eur(m['antes'])} a {eur(m['precio'])}." + (
                                  f" Tu oferta está a {eur(m['mio'])}." if m["mio"] is not None else ""),
                              since=m["desde"], href=url_for("buybox", asin=m["asin"]), cta="Ver producto", asin=m["asin"]))
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
    return render_template("dashboard.html", page="avisos", items=items, moves=moves, periodo=periodo,
                           periodos=MARKET_PERIODS, fresh=fresh, bb=bb, profit=profit,
                           health=health, eur=eur, groups=attention_groups(items, labels, rows),
                           fecha=f"{DIAS_ES[now.weekday()].capitalize()} {now.day} de {MONTHS_ES[now.month].lower()} · {now:%H:%M}")


# ---------------------------------------------------------------- repricer
def _num(v):
    try:
        return round(float(str(v).replace(",", ".").replace("€", "").strip()), 2) if str(v).strip() else None
    except ValueError:
        return None


@app.route("/repricer")
def repricer():
    con = sn.db()
    rp.ensure_tables(con)
    names = sn.seller_names()
    ts, rows = latest_listings(con)
    conf = rp.settings(con)
    watched = set(sn.read_asins())
    items = []
    sin_stock = [r for r in rows if not r["stock"]]
    for r in rows:
        if not r["stock"]:  # sin stock no hay oferta que ajustar
            continue
        c = conf.get(r["sku"]) or dict(estrategia="igualar", activo=0, minimo=None, maximo=None, margen=None, rebaja=None)
        d = rp.decide(con, r["sku"], r["asin"], c, r["precio"]) if r["asin"] in watched else None
        bb = d and d["bb"]
        items.append(dict(r, conf=c, d=d, vigilado=r["asin"] in watched,
                          bb_nombre=sn.name(bb["seller_id"], names) if bb else None,
                          bb_soy_yo=bool(bb and bb["seller_id"] == sn.MY_SELLER_ID)))
    items.sort(key=lambda i: (not i["vigilado"], not i["conf"]["activo"], i["sku"]))
    activos = sum(1 for i in items if i["conf"]["activo"])
    ver = request.args.get("ver") if request.args.get("ver") in ("activados", "sin_activar") else "todos"
    filtros = [("todos", "Todos", len(items)), ("activados", "Activados", activos),
               ("sin_activar", "Sin activar", len(items) - activos)]
    if ver != "todos":
        items = [i for i in items if bool(i["conf"]["activo"]) == (ver == "activados")]
    log = con.execute("SELECT ts, sku, antes, nuevo, bb_precio, motivo, aplicado, error FROM repricer_log "
                      "ORDER BY ts DESC LIMIT 60").fetchall()
    return render_template("repricer.html", page="repricer", items=items, log=log, strategies=rp.STRATEGIES,
                           default_margin=rp.DEFAULT_MARGIN,
                           saved=request.args.get("ok"), sin_stock=len(sin_stock), activos=activos, ver=ver, filtros=filtros)


@app.route("/repricer/guardar", methods=["POST"])
def repricer_guardar():
    con = sn.db()
    f = request.form
    sku, asin = f.get("sku", ""), f.get("asin", "")
    strategy = f.get("estrategia") if f.get("estrategia") in rp.STRATEGIES else "igualar"
    rp.save(con, sku, asin, estrategia=strategy, activo=1 if f.get("activo") else 0,
            minimo=_num(f.get("minimo")), maximo=_num(f.get("maximo")), margen=_num(f.get("margen")))
    try:  # al activarlo, mira enseguida si una regla de precios automática de Amazon le impediría funcionar
        if f.get("activo"):
            rp.check_rule(con, sku)
        else:
            con.execute("UPDATE repricer SET regla_amazon=0 WHERE sku=?", (sku,))
            con.commit()
    except Exception:
        pass
    ver = f.get("ver") if f.get("ver") in ("activados", "sin_activar") else None
    return redirect(url_for("repricer", ok=sku, ver=ver, _anchor="p-" + sku))


# ---------------------------------------------------------------- informes descargables
def report_range():
    """Periodo de los informes: un atajo (?rango=) o desde/hasta; por defecto, el mes pasado."""
    today = datetime.now(sn.TZ).date()
    presets = date_presets(today)
    preset = request.args.get("rango", "" if request.args.get("desde") else "mes_pasado")
    if preset in presets:
        _, start, end = presets[preset]
    else:
        try:
            start = datetime.strptime(request.args["desde"], "%Y-%m-%d").date()
            end = datetime.strptime(request.args.get("hasta") or today.isoformat(), "%Y-%m-%d").date()
        except (KeyError, ValueError):
            preset, (_, start, end) = "mes_pasado", presets["mes_pasado"]
        if end < start:
            start, end = end, start
    return preset, presets, start, min(end, today)


@app.route("/resenas", methods=["GET", "POST"])
def resenas():
    con = sn.db()
    rt.ensure_tables(con)
    conf = rs.settings(con)
    if request.method == "POST":
        f = request.form
        dias = int(_num(f.get("dias")) or conf["dias"])
        rs.save_settings(con, activo=f.get("activo") == "1", dias=min(max(dias, 5), 25))
        return redirect(url_for("resenas", ok=1))
    month = datetime.now(sn.TZ).date().replace(day=1).isoformat()
    counts = dict(con.execute("SELECT estado, COUNT(*) FROM resenas GROUP BY estado"))
    mes = con.execute("SELECT COUNT(*) FROM resenas WHERE estado='enviada' AND ts>=?", (month,)).fetchone()[0]
    pend = rs.candidates(con, int(conf["dias"]))
    log = con.execute("SELECT pedido, compra, estado, ts, detalle FROM resenas ORDER BY ts DESC LIMIT 60").fetchall()
    return render_template("resenas.html", page="resenas", conf=conf, counts=counts, mes=mes, log=log,
                           pendientes=[p for p in pend if not p[2]], excluir=[p for p in pend if p[2]],
                           estados=rs.ESTADOS, ok=request.args.get("ok"))


@app.route("/informes")
def informes():
    preset, presets, start, end = report_range()
    return render_template("informes.html", page="informes", reports=inf.REPORTS, preset=preset, presets=presets,
                           start=start, end=end)


@app.route("/informes/<kind>.xlsx")
def informe_excel(kind):
    if kind not in inf.REPORTS:
        return redirect(url_for("informes"))
    _, _, start, end = report_range()
    name, data = inf.build(kind, start, end)
    return send_file(io.BytesIO(data), as_attachment=True, download_name=name,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


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
         json.dumps({"iva": iva, "envio": envio, "mapping": mapping, "ruta": path.name,
                     "header_idx": int(form["header_idx"])})))
    con.commit()
    an.start_job(cur.lastrowid)
    return redirect(url_for("catalogo", job_id=cur.lastrowid))


@app.route("/catalogos/<int:job_id>/reiniciar", methods=["POST"])
def reiniciar_catalogo(job_id):
    """Vuelve a analizar el catálogo desde el principio con las mismas columnas y opciones."""
    con = sn.db()
    con.row_factory = sqlite3.Row
    job = con.execute("SELECT * FROM catalogos WHERE id=?", (job_id,)).fetchone()
    if job and not (job["estado"] == "en_curso" and an.job_alive(job)):
        an.start_job(job_id)
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


# ---------------------------------------------------------------- Profit: mapa de ventas
# Provincias por los 2 primeros dígitos del código postal: (nombre, abreviatura, columna, fila) en un mapa esquemático
PROVINCIAS = {
    "15": ("A Coruña", "C", 0, 0), "27": ("Lugo", "LU", 1, 0), "33": ("Asturias", "O", 2, 0), "39": ("Cantabria", "S", 3, 0),
    "48": ("Bizkaia", "BI", 4, 0), "20": ("Gipuzkoa", "SS", 5, 0),
    "36": ("Pontevedra", "PO", 0, 1), "32": ("Ourense", "OU", 1, 1), "24": ("León", "LE", 2, 1), "34": ("Palencia", "P", 3, 1),
    "09": ("Burgos", "BU", 4, 1), "01": ("Álava", "VI", 5, 1), "31": ("Navarra", "NA", 6, 1), "22": ("Huesca", "HU", 7, 1),
    "25": ("Lleida", "L", 8, 1), "17": ("Girona", "GI", 9, 1),
    "49": ("Zamora", "ZA", 1, 2), "47": ("Valladolid", "VA", 2, 2), "40": ("Segovia", "SG", 3, 2), "42": ("Soria", "SO", 4, 2),
    "26": ("La Rioja", "LO", 5, 2), "50": ("Zaragoza", "Z", 6, 2), "43": ("Tarragona", "T", 8, 2), "08": ("Barcelona", "B", 9, 2),
    "37": ("Salamanca", "SA", 1, 3), "05": ("Ávila", "AV", 2, 3), "28": ("Madrid", "M", 3, 3), "19": ("Guadalajara", "GU", 4, 3),
    "44": ("Teruel", "TE", 5, 3), "12": ("Castellón", "CS", 6, 3),
    "10": ("Cáceres", "CC", 1, 4), "45": ("Toledo", "TO", 2, 4), "16": ("Cuenca", "CU", 3, 4), "46": ("Valencia", "V", 5, 4),
    "07": ("Baleares", "IB", 8, 4),
    "06": ("Badajoz", "BA", 1, 5), "13": ("Ciudad Real", "CR", 2, 5), "02": ("Albacete", "AB", 3, 5), "03": ("Alicante", "A", 5, 5),
    "41": ("Sevilla", "SE", 1, 6), "14": ("Córdoba", "CO", 2, 6), "23": ("Jaén", "J", 3, 6), "30": ("Murcia", "MU", 4, 6),
    "21": ("Huelva", "H", 0, 7), "11": ("Cádiz", "CA", 1, 7), "29": ("Málaga", "MA", 2, 7), "18": ("Granada", "GR", 3, 7),
    "04": ("Almería", "AL", 4, 7),
    "51": ("Ceuta", "CE", 1, 8), "52": ("Melilla", "ML", 3, 8), "38": ("S.C. de Tenerife", "TF", 7, 8), "35": ("Las Palmas", "GC", 8, 8),
}
PAISES = {"ES": "España", "PT": "Portugal", "FR": "Francia", "DE": "Alemania", "IT": "Italia", "NL": "Países Bajos",
          "BE": "Bélgica", "CH": "Suiza", "AD": "Andorra", "GB": "Reino Unido", "IE": "Irlanda", "AT": "Austria",
          "LU": "Luxemburgo", "PL": "Polonia", "SE": "Suecia"}


def sales_map(con, start, end):
    """Pedidos, unidades y ventas (con IVA) por país y, en España, por provincia (código postal del envío)."""
    gross, units = defaultdict(float), defaultdict(int)
    for pedido, concepto, cantidad, importe in con.execute(
            "SELECT pedido, concepto, cantidad, importe FROM fin_lineas WHERE tipo='venta' "
            "AND (concepto='UNIDADES' OR concepto IN ('Principal','ShippingCharge','GiftWrap','Promotion'))"):
        if concepto == "UNIDADES":
            units[pedido] += cantidad or 0
        else:
            gross[pedido] += importe or 0
    pending = defaultdict(lambda: [0, 0.0])  # pedidos aún sin liquidar: lo que dice el pedido
    for pedido, qty, precio, promo in con.execute("SELECT pedido, cantidad, precio, promo FROM pedido_items"):
        pending[pedido][0] += qty or 0
        pending[pedido][1] += (precio or 0) - (promo or 0)
    countries = defaultdict(lambda: dict(pedidos=0, uds=0, ventas=0.0))
    provinces = defaultdict(lambda: dict(pedidos=0, uds=0, ventas=0.0))
    for pedido, compra, pais, cp in con.execute(
            "SELECT pedido, compra, pais, cp FROM pedidos WHERE estado != 'Canceled' AND compra >= ?",
            ((start - timedelta(days=1)).isoformat(),)):
        d = rt.local_date(compra)
        if d < start or d > end:
            continue
        u, v = (units[pedido], gross[pedido]) if pedido in units else pending.get(pedido, (0, 0.0))
        for bucket in [countries[pais or "?"]] + ([provinces[(cp or "")[:2]]] if pais == "ES" and (cp or "")[:2] in PROVINCIAS else []):
            bucket["pedidos"] += 1
            bucket["uds"] += u
            bucket["ventas"] += v
    total = sum(c["pedidos"] for c in countries.values()) or 1
    paises = sorted(({**c, "codigo": k, "nombre": PAISES.get(k, "Sin país (pedido pendiente)" if k == "?" else k),
                      "parte": 100 * c["pedidos"] / total} for k, c in countries.items()), key=lambda c: -c["pedidos"])
    top = max((p["pedidos"] for p in provinces.values()), default=0)
    es = sum(p["pedidos"] for p in provinces.values()) or 1
    tiles = [dict(codigo=k, nombre=n, abr=a, col=c, fila=f, **provinces.get(k, dict(pedidos=0, uds=0, ventas=0.0)),
                  nivel=1 + min(3, int(4 * (provinces[k]["pedidos"] / top) ** 0.5)) if provinces.get(k) else 0,
                  parte=100 * provinces.get(k, {}).get("pedidos", 0) / es)
             for k, (n, a, c, f) in PROVINCIAS.items()]
    ranking = sorted((t for t in tiles if t["pedidos"]), key=lambda t: -t["pedidos"])
    return dict(paises=paises, provincias=tiles, ranking=ranking, total=sum(c["pedidos"] for c in countries.values()))


# ---------------------------------------------------------------- Profit: reembolsos
RETURN_REASONS = {
    "ORDERED_WRONG_ITEM": "Pidió el producto equivocado", "UNWANTED_ITEM": "Ya no lo quiere",
    "NOT_AS_DESCRIBED": "No es como se describe", "FOUND_BETTER_PRICE": "Encontró mejor precio",
    "NO_REASON_GIVEN": "Sin motivo", "SWITCHEROO": "Devolvió otro artículo", "NOT_COMPATIBLE": "No le sirve",
    "QUALITY_UNACCEPTABLE": "Mala calidad", "DEFECTIVE": "Defectuoso", "DAMAGED_BY_FC": "Dañado en el almacén",
    "DAMAGED_BY_CARRIER": "Dañado en el envío", "MISSED_ESTIMATED_DELIVERY": "Llegó tarde",
    "UNDELIVERABLE_UNKNOWN": "No se pudo entregar", "UNDELIVERABLE_REFUSED": "Rechazado en la entrega",
    "UNAUTHORIZED_PURCHASE": "Compra no autorizada", "EXTRA_ITEM": "Le llegó de más", "MISSING_PARTS": "Faltan piezas",
}
RETURN_STATES = {"SELLABLE": "Vendible", "CUSTOMER_DAMAGED": "Dañado por el cliente", "DEFECTIVE": "Defectuoso",
                 "CARRIER_DAMAGED": "Dañado en el envío", "DISTRIBUTOR_DAMAGED": "Dañado en el almacén",
                 "EXPIRED": "Caducado"}
REFUND_ALERT_UNITS, REFUND_ALERT_RATE, REFUND_BAD_RATE = 3, 15, 20  # aviso: 3+ reembolsos y 15 % o más de lo vendido


def return_details(con, start, end):
    """Devoluciones físicas del rango por SKU: motivos y estado en que llegaron al almacén."""
    out = defaultdict(lambda: dict(motivos=Counter(), estados=Counter()))
    for fecha, sku, qty, estado, motivo in con.execute(
            "SELECT fecha, sku, cantidad, estado, motivo FROM devoluciones_fba"):
        if start <= rt.local_date(fecha) <= end:
            out[sku]["motivos"][RETURN_REASONS.get(motivo, (motivo or "Sin motivo").replace("_", " ").capitalize())] += qty or 1
            out[sku]["estados"][RETURN_STATES.get(estado, (estado or "").replace("_", " ").capitalize())] += qty or 1
    return out


FEE_ALERT_EUR, FEE_ALERT_PCT = 0.10, 1.0  # subida de tarifa FBA (€/u) o de comisión (puntos de %) que merece aviso


def fee_changes(con, today):
    """SKUs cuya tarifa FBA por unidad o cuyo % de comisión ha subido en las ventas de los últimos 14 días frente a
    los 75 anteriores (lo más repetido en cada tramo). Suele ser un cambio de medidas, peso o categoría."""
    from statistics import mode
    recent_from = (today - timedelta(days=14)).isoformat()
    old_from = (today - timedelta(days=90)).isoformat()
    per = defaultdict(lambda: dict(fba=([], []), com=([], []), first_new=None))
    for sku, posted, uds, fba, com, principal in con.execute(
            "SELECT sku, MIN(posted), SUM(CASE WHEN concepto='UNIDADES' THEN cantidad ELSE 0 END), "
            "SUM(CASE WHEN concepto='FBAPerUnitFulfillmentFee' THEN importe ELSE 0 END), "
            "SUM(CASE WHEN concepto='Commission' THEN importe ELSE 0 END), "
            "SUM(CASE WHEN concepto='Principal' THEN importe ELSE 0 END) "
            "FROM fin_lineas WHERE tipo='venta' AND sku IS NOT NULL AND posted>=? GROUP BY pedido, sku ORDER BY 2",
            (old_from,)):
        if not uds:
            continue
        p = per[sku]
        new = posted >= recent_from
        if fba < 0:
            p["fba"][new].append((round(-fba / uds, 2), posted))
        if com < 0 and principal > 0:
            p["com"][new].append((round(-100 * com / principal, 1), posted))
    out = []
    for sku, p in per.items():
        for kind, limit in (("fba", FEE_ALERT_EUR), ("com", FEE_ALERT_PCT)):
            old, new = p[kind]
            if len(old) < 3 or len(new) < 2:
                continue
            was, now = mode(v for v, _ in old), mode(v for v, _ in new)
            if now - was >= limit and sum(1 for v, _ in new if v == now) >= 2:
                since = next(ts for v, ts in new if v == now)
                out.append(dict(sku=sku, tipo=kind, antes=was, ahora=now, desde=since,
                                uds=sum(1 for v, _ in new if v == now)))
    return out


def new_sellers(con, asins, days=3):
    """Vendedores que aparecen por primera vez en tus ASIN en los últimos días: {asin: [(seller, primera, precio o None)]}."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    start = dict(con.execute("SELECT asin, MIN(ts) FROM snapshots GROUP BY asin"))
    me = sn.MY_SELLER_ID
    out = defaultdict(list)
    q = ",".join("?" * len(asins))
    for asin, seller, first in con.execute(
            f"SELECT asin, seller_id, primera FROM vendedores_vistos WHERE primera>? AND asin IN ({q}) ORDER BY primera",
            (cutoff, *asins)):
        a0 = start.get(asin)
        # los que ya estaban cuando empezó la vigilancia del ASIN no son nuevos
        if seller == me or not a0 or datetime.fromisoformat(first) - datetime.fromisoformat(a0) < timedelta(hours=2):
            continue
        last = sn.last_snapshot(con, asin) or {"offers": {}}
        out[asin].append((seller, first, last["offers"].get(seller)))
    return out


def refunds_view(con, start, end, rows):
    """Productos con reembolsos en el rango: unidades, % sobre lo vendido, dinero y motivos."""
    details = return_details(con, start, end)
    items, motivos, estados = [], Counter(), Counter()
    for d in details.values():
        motivos.update(d["motivos"])
        estados.update(d["estados"])
    for r in rows:
        if not r["devueltas"]:
            continue
        d = details.get(r["sku"], dict(motivos=Counter(), estados=Counter()))
        items.append(dict(sku=r["sku"], nombre=r["nombre"], uds=r["uds"], devueltas=r["devueltas"],
                          tasa=100 * r["devueltas"] / r["uds"] if r["uds"] else None,
                          importe=-r["devoluciones"], efecto=r["dev_neto"] + r["recuperado"],
                          vendibles=r["vendibles"], motivos=d["motivos"].most_common(3)))
    for it in items:
        t = it["tasa"]
        it["tone"] = "bad" if t is not None and t >= REFUND_BAD_RATE and it["devueltas"] >= REFUND_ALERT_UNITS else \
            "warn" if t is not None and t >= REFUND_ALERT_RATE and it["devueltas"] >= REFUND_ALERT_UNITS else ""
    items.sort(key=lambda x: (-x["devueltas"], x["efecto"]))
    uds = sum(r["uds"] for r in rows)
    dev = sum(i["devueltas"] for i in items)
    return dict(productos=items, devueltas=dev, uds=uds, tasa=100 * dev / uds if uds else None,
                importe=sum(i["importe"] for i in items), efecto=sum(i["efecto"] for i in items),
                vendibles=sum(i["vendibles"] for i in items), motivos=motivos.most_common(6),
                estados=estados.most_common(), fisicas=sum(estados.values()))


# App instalable (PWA): el manifest y el service worker se sirven desde la raíz para que la app abarque todo el panel
@app.route("/manifest.webmanifest")
def manifest():
    r = send_file(Path(app.static_folder) / "manifest.webmanifest", mimetype="application/manifest+json")
    r.headers["Cache-Control"] = "no-cache"
    return r


@app.route("/sw.js")
def service_worker():
    r = send_file(Path(app.static_folder) / "sw.js", mimetype="text/javascript")
    r.headers["Cache-Control"] = "no-cache"
    return r


@app.route("/sin-conexion")
def sin_conexion():
    return """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Sin conexión · Seller Notes</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f5f5f4;color:#111;
font:16px/1.5 -apple-system,system-ui,sans-serif;text-align:center;padding:24px;box-sizing:border-box}
h1{font-size:20px;margin:16px 0 4px}p{margin:0 0 20px;color:#6b7280}
button{font:inherit;font-weight:600;border:0;border-radius:10px;padding:12px 22px;background:#111;color:#fff}
@media (prefers-color-scheme:dark){body{background:#111213;color:#f1f1f0}button{background:#fff;color:#111}}</style></head>
<body><div><img src="/static/logo.png" width="64" height="64" alt="" style="border-radius:14px">
<h1>Sin conexión</h1><p>Seller Notes necesita internet para mostrar tus datos.</p>
<button onclick="location.reload()">Reintentar</button></div></body></html>"""


@app.route("/rentabilidad")
@app.route("/")  # Profit es la página de inicio
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
    cmp = None
    pa, pb = prev_range(start, end)
    if pa >= datetime.strptime(rt.DEFAULT_FROM, "%Y-%m-%d").date():  # solo si el periodo anterior tiene datos completos
        _, _, ptot, _ = profit_view(con, pa, pb, group)
        cmp = dict(desde=pa, hasta=pb, ventas=change(tot["ventas"], ptot["ventas"]), uds=change(tot["uds"], ptot["uds"]),
                   neto=change(tot["neto"], ptot["neto"], pct=False), antes=ptot,
                   href=url_for("rentabilidad", desde=pa.isoformat(), hasta=pb.isoformat()))
    last = con.execute("SELECT MAX(posted) FROM fin_lineas").fetchone()[0]
    return render_template("rentabilidad.html", page="rentabilidad", rows=rows, acc=acc, tot=tot, periods=periods,
                           today=today,
                           refunds=refunds_view(con, start, end, rows), payout=rt.payouts(con), cmp=cmp,
                           mapa=sales_map(con, start, end),
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
    return render_template("costes.html", page="costes", costs=sorted(latest.values(), key=lambda c: c["sku"]),
                           missing=missing, today=datetime.now(sn.TZ).date().isoformat(), saved=saved)


RETURN_WAIT_DAYS = 45  # días que Amazon da al cliente para devolver el producto antes de poder reclamarlo
COMP_REASONS = {"CustomerReturn": "Devolución no recibida", "Lost_Warehouse": "Perdida en el almacén",
                "Damaged_Warehouse": "Dañada en el almacén", "Lost_Inbound": "Perdida en un envío a Amazon",
                "CustomerServiceIssue": "Atención al cliente", "Reimbursement_Reversal": "Anulación de compensación",
                "FeeCorrection": "Corrección de tarifa", "Disposed_Warehouse": "Desechada en el almacén"}


def money_back(con, today):
    """Reembolsos al cliente cuyo producto no ha vuelto al almacén ni te ha compensado Amazon.
    Pasados 45 días desde el reembolso se pueden reclamar en Seller Central."""
    rt.ensure_tables(con)
    refunds = {}
    for pedido, sku, first, uds, importe in con.execute(
            "SELECT pedido, sku, MIN(posted), SUM(CASE WHEN concepto='UNIDADES' THEN cantidad ELSE 0 END), "
            "SUM(CASE WHEN concepto IN ('Principal','ShippingCharge','GiftWrap','Promotion') THEN importe ELSE 0 END) "
            "FROM fin_lineas WHERE tipo='devolucion' AND sku IS NOT NULL AND pedido != '' GROUP BY pedido, sku"):
        if uds:
            refunds[(pedido, sku)] = (first, uds, -importe)
    returned = defaultdict(int)
    for pedido, sku, qty in con.execute("SELECT pedido, sku, cantidad FROM devoluciones_fba"):
        returned[(pedido, sku)] += qty or 0
    comp = defaultdict(int)
    for pedido, sku, a, b in con.execute("SELECT pedido, sku, uds_dinero, uds_stock FROM compensaciones WHERE pedido != ''"):
        comp[(pedido, sku)] += (a or 0) + (b or 0)
    names = {r[0]: r[6] for r in rt.costs_table(con)}
    claim, wait = [], []
    for (pedido, sku), (first, uds, importe) in refunds.items():
        pend = uds - returned[(pedido, sku)] - comp[(pedido, sku)]
        if pend <= 0:
            continue
        d = rt.local_date(first)
        days = (today - d).days
        item = dict(pedido=pedido, sku=sku, nombre=names.get(sku) or sku, fecha=d, dias=days, uds=pend,
                    importe=importe * pend / uds, faltan=max(0, RETURN_WAIT_DAYS - days))
        (claim if days >= RETURN_WAIT_DAYS else wait).append(item)
    claim.sort(key=lambda i: i["fecha"])
    wait.sort(key=lambda i: i["fecha"])
    paid = [dict(fecha=rt.local_date(f), pedido=p, motivo=COMP_REASONS.get(m, m), sku=k, importe=i, uds=(a or 0) + (b or 0))
            for f, p, m, k, i, a, b in con.execute(
                "SELECT fecha, pedido, motivo, sku, importe, uds_dinero, uds_stock FROM compensaciones ORDER BY fecha DESC")]
    return dict(reclamar=claim, esperar=wait, pagadas=paid,
                total_reclamar=sum(i["importe"] for i in claim), total_esperar=sum(i["importe"] for i in wait),
                total_pagadas=sum(p["importe"] for p in paid),
                sin_datos=not con.execute("SELECT 1 FROM compensaciones LIMIT 1").fetchone())


def negative_feedback(con, today, days=60):
    """Valoraciones de 1 a 3 estrellas de los últimos días: [(pedido, fecha, nota, comentario)]."""
    rt.ensure_tables(con)
    out = []
    for pedido, fecha, nota, comentario in con.execute(
            "SELECT pedido, fecha, nota, comentario FROM valoraciones WHERE nota <= 3"):
        d = None
        for fmt in ("%d/%m/%y", "%d/%m/%Y", "%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d", "%d.%m.%Y"):
            try:
                d = datetime.strptime(fecha[:10], fmt).date()
                break
            except ValueError:
                continue
        if d and (today - d).days <= days:
            out.append((pedido, d, nota, comentario))
    return sorted(out, key=lambda x: x[1], reverse=True)


def shipments_view(con):
    """Envíos a FBA con unidades enviadas, recibidas y diferencia; y los SKU donde faltan unidades."""
    rt.ensure_tables(con)
    names, cost = {}, {}
    for sku, desde, coste, envio, iva, asin, nombre in rt.costs_table(con):
        names[sku], cost[sku] = nombre, coste + (envio or 0)
    out = []
    for sid, nombre, estado, destino, visto in con.execute(
            "SELECT id, nombre, estado, destino, visto FROM envios_fba WHERE estado != 'DELETED' ORDER BY visto DESC, id DESC"):
        items = [dict(sku=k, nombre=names.get(k) or k, enviadas=e or 0, recibidas=r or 0, dif=(r or 0) - (e or 0),
                      coste=cost.get(k)) for k, e, r in con.execute(
                     "SELECT sku, enviadas, recibidas FROM envio_items WHERE envio=? ORDER BY sku", (sid,))]
        env, rec = sum(i["enviadas"] for i in items), sum(i["recibidas"] for i in items)
        falta = [i for i in items if i["dif"] < 0]
        out.append(dict(id=sid, nombre=nombre, estado=estado, destino=destino, items=items, enviadas=env, recibidas=rec,
                        pct=100 * rec / env if env else None, faltan=-sum(i["dif"] for i in falta),
                        valor=sum(-i["dif"] * (i["coste"] or 0) for i in falta),
                        reclamar=estado == "CLOSED" and bool(falta)))
    return out


SHIPMENT_LABELS = {"WORKING": "Preparando", "READY_TO_SHIP": "Listo para enviar", "SHIPPED": "Enviado",
                   "IN_TRANSIT": "En tránsito", "DELIVERED": "Entregado", "CHECKED_IN": "Registrado",
                   "RECEIVING": "Recibiendo", "CLOSED": "Cerrado", "CANCELLED": "Cancelado", "ERROR": "Error"}


def missing_roles(con):
    rt.ensure_tables(con)
    return dict(con.execute("SELECT clave, mensaje FROM sync_permisos"))


@app.route("/envios")
def envios():
    con = sn.db()
    return render_template("envios.html", page="envios", envios=shipments_view(con), labels=SHIPMENT_LABELS,
                           falta_rol=missing_roles(con).get("envios"),
                           sin_datos=not con.execute("SELECT 1 FROM envios_fba LIMIT 1").fetchone())


@app.route("/rentabilidad/money-back")
def money_back_page():
    con = sn.db()
    today = datetime.now(sn.TZ).date()
    return render_template("money_back.html", page="money_back_page", mb=money_back(con, today), espera=RETURN_WAIT_DAYS,
                           envios=[e for e in shipments_view(con) if e["reclamar"]], falta_rol=missing_roles(con).get("envios"))


TREND_METRICS = {  # clave: (etiqueta, campo de la fila por SKU o del tráfico, formato, se suma por meses)
    "ventas": ("Ventas con IVA", "eur", True), "uds": ("Unidades", "int", True),
    "beneficio": ("Beneficio de producto", "eur", True), "margen": ("Margen", "pct", False),
    "devueltas": ("Reembolsos (uds)", "int", True), "sesiones": ("Sesiones", "int", True),
    "conversion": ("Conversión", "pct", False),
}


@app.route("/rentabilidad/tendencias")
def tendencias():
    """Producto × mes: la cifra de cada mes, el cambio frente al anterior y la previsión del mes en curso."""
    con = sn.db()
    rt.ensure_tables(con)
    today = datetime.now(sn.TZ).date()
    metric = request.args.get("ver") if request.args.get("ver") in TREND_METRICS else "ventas"
    label, fmt, additive = TREND_METRICS[metric]
    first = datetime.strptime(rt.DEFAULT_FROM, "%Y-%m-%d").date()
    months, d = [], first
    while d <= today:
        months.append(d.strftime("%Y-%m"))
        d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    skus, _ = rt.aggregate(con, first, today, "mes")
    names = {r[0]: r[6] for r in rt.costs_table(con)}
    asin_of = {r[0]: r[5] for r in rt.costs_table(con)}
    traffic = rt.traffic_by_month(con) if metric in ("sesiones", "conversion") else {}

    def value(sku, m):
        r = skus.get(m, {}).get(sku)
        if metric in ("sesiones", "conversion"):
            t = traffic.get((asin_of.get(sku), m))
            if not t:
                return None
            if metric == "sesiones":
                return t["sesiones"]
            return 100 * t["uds"] / t["sesiones"] if t["sesiones"] else None
        if not r:
            return None
        if metric == "beneficio":
            return r["ingresos"] + r["comisiones"] + r["fba"] + r["reembolsos"] - r["coste"]
        if metric == "margen":
            b = r["ingresos"] + r["comisiones"] + r["fba"] + r["reembolsos"] - r["coste"]
            return 100 * b / r["ingresos"] if r["ingresos"] else None
        return r[metric]

    all_skus = {s for by in skus.values() for s in by}
    days_in = ((today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)).day
    ratio = days_in / today.day  # previsión: lo que va de mes al mismo ritmo
    rows = []
    for sku in all_skus:
        vals = [value(sku, m) for m in months]
        if not any(v for v in vals):
            continue
        cells = []
        for i, v in enumerate(vals):
            prev = vals[i - 1] if i else None
            pct = None
            if v is not None and prev not in (None, 0) and fmt != "pct":
                pct = 100 * (v - prev) / abs(prev)
            elif v is not None and prev is not None and fmt == "pct":
                pct = v - prev  # en puntos
            cells.append(dict(v=v, pct=pct))
        cur = vals[-1]
        forecast = cur * ratio if additive and cur is not None and today.day < days_in else None
        last3 = sum(abs(v or 0) for v in vals[-3:])
        rows.append(dict(sku=sku, nombre=names.get(sku) or sku, asin=asin_of.get(sku), cells=cells, forecast=forecast,
                         spark=vals, orden=last3))
    rows.sort(key=lambda r: -r["orden"])
    total = None
    if additive:
        tv = [sum((r["cells"][i]["v"] or 0) for r in rows) for i in range(len(months))]
        total = dict(cells=[dict(v=v, pct=100 * (v - tv[i - 1]) / abs(tv[i - 1]) if i and tv[i - 1] else None)
                            for i, v in enumerate(tv)],
                     forecast=tv[-1] * ratio if today.day < days_in else None)
    return render_template("tendencias.html", page="tendencias", rows=rows, total=total, metric=metric,
                           metrics=TREND_METRICS, label=label, fmt=fmt,
                           months=[(m, period_label(m, "mes")) for m in months],
                           sin_trafico=metric in ("sesiones", "conversion") and not traffic,
                           dia=today.day, dias_mes=days_in)


@app.route("/rentabilidad/gastos", methods=["GET", "POST"])
def gastos():
    con = sn.db()
    rt.ensure_tables(con)
    today = datetime.now(sn.TZ).date()
    if request.method == "POST":
        f = request.form
        if f.get("borrar"):
            con.execute("DELETE FROM gastos_indirectos WHERE id=?", (f["borrar"],))
        elif f.get("terminar"):  # un gasto mensual que dejas de pagar: se cuenta hasta el final de este mes
            con.execute("UPDATE gastos_indirectos SET hasta=? WHERE id=?", (today.isoformat(), f["terminar"]))
        else:
            concepto, importe = f.get("concepto", "").strip()[:60], an.parse_money(f.get("importe"))
            frecuencia = "puntual" if f.get("frecuencia") == "puntual" else "mensual"
            if not concepto or not importe:
                return redirect(url_for("gastos", error=1))
            con.execute("INSERT INTO gastos_indirectos(concepto, importe, frecuencia, desde, hasta) VALUES (?,?,?,?,?)",
                        (concepto, abs(importe), frecuencia, f.get("desde") or today.isoformat(), f.get("hasta") or None))
        con.commit()
        return redirect(url_for("gastos"))
    rows = [dict(id=i, concepto=c, importe=imp, frecuencia=fr, desde=d, hasta=h,
                 activo=fr == "mensual" and d <= today.isoformat() and (not h or h[:7] >= today.isoformat()[:7]))
            for i, c, imp, fr, d, h in con.execute(
                "SELECT id, concepto, importe, frecuencia, desde, hasta FROM gastos_indirectos ORDER BY frecuencia, desde DESC")]
    month_start = today.replace(day=1)
    this_month = sum(rt.indirect_by_day(con, month_start, (month_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)).values())
    conceptos = sorted({r["concepto"] for r in rows} | {"Gestoría", "Software", "Prep center", "Muestras", "Banco"})
    return render_template("gastos.html", page="gastos", rows=rows, today=today.isoformat(), conceptos=conceptos,
                           mensual=sum(r["importe"] for r in rows if r["activo"]), este_mes=this_month,
                           mes=MONTHS_ES[today.month].lower(), error=request.args.get("error"))


def mark_interrupted_jobs():
    """Los análisis van en su propio proceso; si ese proceso ya no existe (p. ej. se reinició el servidor), no va a terminar."""
    con = sn.db()
    an.ensure_tables(con)
    con.row_factory = sqlite3.Row
    for job in con.execute("SELECT * FROM catalogos WHERE estado='en_curso'").fetchall():
        if not an.job_alive(job):
            con.execute("UPDATE catalogos SET estado='error', mensaje='Interrumpido antes de terminar' WHERE id=?",
                        (job["id"],))
    con.commit()


import api  # noqa: E402  API JSON para la app de iPhone (/api/...), con el mismo inicio de sesión
api.init(app, sys.modules[__name__])

if __name__ == "__main__":
    if "--puerto" in sys.argv:  # otro puerto para probar cambios sin cerrar el panel abierto
        PORT = int(sys.argv[sys.argv.index("--puerto") + 1])
    mark_interrupted_jobs()
    if "--no-abrir" not in sys.argv:
        Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    print(f"Seller Notes · panel en http://127.0.0.1:{PORT}  (Ctrl+C para cerrar)")
    app.run(host="127.0.0.1", port=PORT, debug=False)

