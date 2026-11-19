#!/usr/bin/env python3
"""
Seller Notes — pedir reseñas automáticamente (botón «Solicitar una reseña» de Seller Central).

Usa la API oficial de Amazon (Solicitations): Amazon envía al cliente su correo estándar pidiendo una reseña del
producto y una valoración del vendedor. Solo se puede una vez por pedido, entre 5 y 30 días después de la entrega.
Va apagado hasta que se active en el panel. El monitor lo ejecuta tras actualizar la rentabilidad.
"""
import json
import time
from datetime import datetime, timedelta, timezone

import requests

import seller_notes as sn

DEFAULTS = dict(activo=False, dias=10)  # días desde la compra: unos 3 de envío + 7 desde la entrega
MAX_PER_RUN = 40  # la API permite 1 petición por segundo; así cada ronda dura como mucho ~1,5 minutos
RETRY_HOURS = 24  # si Amazon aún no deja pedirla, se vuelve a mirar al día siguiente
WINDOW_DAYS = 30
ACTION = "productReviewAndSellerFeedback"

ESTADOS = {"enviada": "Pedida", "no_disponible": "Amazon no la permite", "error": "Error",
           "excluido": "Excluido"}


def ensure_tables(con):
    con.execute("CREATE TABLE IF NOT EXISTS ajustes (clave TEXT PRIMARY KEY, valor TEXT)")
    con.execute("""CREATE TABLE IF NOT EXISTS resenas (
        pedido TEXT PRIMARY KEY, compra TEXT, estado TEXT, ts TEXT, detalle TEXT)""")


def settings(con):
    ensure_tables(con)
    r = con.execute("SELECT valor FROM ajustes WHERE clave='resenas'").fetchone()
    out = dict(DEFAULTS)
    if r:
        out.update(json.loads(r[0]))
    return out


def save_settings(con, **kw):
    conf = settings(con)
    conf.update(kw)
    with con:
        con.execute("INSERT OR REPLACE INTO ajustes VALUES ('resenas', ?)", (json.dumps(conf),))
    return conf


def candidates(con, dias, now=None):
    """Pedidos enviados hace entre `dias` y 30 días, sin devolución ni reembolso, que aún no tienen reseña pedida
    (o que Amazon no dejó pedir hace más de un día). Devuelve [(pedido, compra, motivo de exclusión o None)]."""
    now = now or datetime.now(timezone.utc)
    newest = (now - timedelta(days=dias)).strftime("%Y-%m-%dT%H:%M:%SZ")
    oldest = (now - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    retry = (now - timedelta(hours=RETRY_HOURS)).isoformat()
    refunded = {r[0] for r in con.execute("SELECT DISTINCT pedido FROM fin_lineas WHERE tipo='devolucion'")}
    try:
        returned = {r[0] for r in con.execute("SELECT DISTINCT pedido FROM devoluciones_fba")}
    except Exception:
        returned = set()
    out = []
    for pedido, compra, estado, ts in con.execute(
            "SELECT p.pedido, p.compra, r.estado, r.ts FROM pedidos p LEFT JOIN resenas r ON r.pedido = p.pedido "
            "WHERE p.estado = 'Shipped' AND p.compra BETWEEN ? AND ? ORDER BY p.compra", (oldest, newest)):
        if estado in ("enviada", "excluido") or (estado in ("no_disponible", "error") and ts > retry):
            continue
        why = "Tiene una devolución o un reembolso" if pedido in refunded or pedido in returned else None
        out.append((pedido, compra, why))
    return out


def _call(method, path):
    for attempt in range(5):
        r = requests.request(method, f"{sn.ENDPOINT}{path}", params={"marketplaceIds": sn.MARKETPLACE_ID},
                             headers={"x-amz-access-token": sn.access_token(), "content-type": "application/json"},
                             timeout=30)
        if r.status_code != 429:
            return r
        time.sleep(2 * (attempt + 1))
    return r


def request_review(pedido):
    """(estado, detalle). Primero pregunta si Amazon deja pedirla (ya pedida a mano, fuera de plazo…)."""
    r = _call("GET", f"/solicitations/v1/orders/{pedido}")
    if r.status_code != 200:
        return "error", f"Amazon respondió {r.status_code} al consultar el pedido"
    actions = [a.get("name") for a in (r.json().get("_links") or {}).get("actions") or []]
    if ACTION not in actions:
        return "no_disponible", "Amazon no la permite ahora (fuera de plazo o ya pedida desde Seller Central)"
    time.sleep(1.1)
    r = _call("POST", f"/solicitations/v1/orders/{pedido}/solicitations/{ACTION}")
    if r.status_code == 201:
        return "enviada", ""
    try:
        msg = "; ".join(e.get("message", "") for e in r.json().get("errors") or [])
    except ValueError:
        msg = r.text[:200]
    return "error", f"Amazon respondió {r.status_code}: {msg}".strip(": ")


def run(con=None, progress=print):
    """Una ronda: pide la reseña de los pedidos que ya toca. No hace nada si está apagado."""
    con = con or sn.db()
    conf = settings(con)
    if not conf["activo"]:
        return 0
    sent = 0
    for pedido, compra, why in candidates(con, int(conf["dias"]))[:MAX_PER_RUN]:
        estado, detalle = ("excluido", why) if why else request_review(pedido)
        with con:
            con.execute("INSERT OR REPLACE INTO resenas VALUES (?,?,?,?,?)",
                        (pedido, compra, estado, datetime.now(timezone.utc).isoformat(), detalle))
        sent += estado == "enviada"
        if not why:
            time.sleep(1.1)
    if sent:
        progress(f"Reseñas pedidas: {sent}")
    return sent
