#!/usr/bin/env python3
"""
Seller Notes — rentabilidad real por SKU (Amazon.es).

Costes de producto por SKU (con fecha «desde» para cuando cambian) + movimientos de dinero de
Amazon (Finances API, rol «Finanzas y contabilidad») → beneficio, margen y ROI por SKU y mes.

Uso:
  python rentabilidad.py costes "Profit report.xlsx"   # importa la pestaña Productos
  python rentabilidad.py costes                        # muestra los costes cargados
  python rentabilidad.py descargar [--todo]            # baja los movimientos nuevos de Amazon
  python rentabilidad.py informe [AAAA-MM]             # beneficio por SKU y mes
"""
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests

import seller_notes as sn

DEFAULT_FROM = "2026-07-01"  # primera venta


# ---------------------------------------------------------------- tablas
def ensure_tables(con):
    cols = [r[1] for r in con.execute("PRAGMA table_info(devoluciones_fba)")]
    if cols and "clave" not in cols:  # versión anterior: la etiqueta (LPN) sola no es única; se vuelve a descargar
        con.execute("DROP TABLE devoluciones_fba")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS costes (
            sku TEXT, desde TEXT, coste REAL, envio_fba REAL DEFAULT 0, iva REAL,
            asin TEXT, nombre TEXT, PRIMARY KEY (sku, desde));
        CREATE TABLE IF NOT EXISTS fin_lineas (
            posted TEXT, mes TEXT, tipo TEXT, pedido TEXT, sku TEXT, cantidad INTEGER,
            concepto TEXT, importe REAL);
        CREATE INDEX IF NOT EXISTS ix_fin ON fin_lineas(mes, sku);
        CREATE INDEX IF NOT EXISTS ix_fin_posted ON fin_lineas(posted);
        CREATE TABLE IF NOT EXISTS devoluciones_fba (
            fecha TEXT, mes TEXT, pedido TEXT, sku TEXT, cantidad INTEGER, estado TEXT, motivo TEXT,
            clave TEXT PRIMARY KEY);  -- pedido|etiqueta|fecha: Amazon reutiliza etiquetas entre devoluciones
        CREATE TABLE IF NOT EXISTS pedidos (
            pedido TEXT PRIMARY KEY, compra TEXT, estado TEXT, total REAL, actualizado TEXT);
        CREATE TABLE IF NOT EXISTS pedido_items (  -- solo pedidos aún sin liquidar (para estimarlos)
            pedido TEXT, sku TEXT, cantidad INTEGER, precio REAL, promo REAL);
        CREATE TABLE IF NOT EXISTS gastos_manual (
            mes TEXT, concepto TEXT, importe REAL, PRIMARY KEY (mes, concepto));
        CREATE TABLE IF NOT EXISTS gastos_indirectos (  -- gastos fuera de Amazon: gestor, software, prep center…
            id INTEGER PRIMARY KEY, concepto TEXT, importe REAL, frecuencia TEXT, desde TEXT, hasta TEXT);
        CREATE TABLE IF NOT EXISTS pagos (  -- liquidaciones de Amazon: la abierta es el próximo pago
            grupo TEXT PRIMARY KEY, inicio TEXT, fin TEXT, estado TEXT, transferencia TEXT, total REAL,
            fecha_pago TEXT, moneda TEXT);
        CREATE TABLE IF NOT EXISTS compensaciones (  -- informe de reembolsos de Amazon al vendedor (FBA)
            id TEXT PRIMARY KEY, fecha TEXT, pedido TEXT, motivo TEXT, sku TEXT, importe REAL,
            uds_dinero INTEGER, uds_stock INTEGER);
        CREATE TABLE IF NOT EXISTS envios_fba (  -- envíos a los almacenes de Amazon (Fulfillment Inbound API)
            id TEXT PRIMARY KEY, nombre TEXT, estado TEXT, destino TEXT, visto TEXT, actualizado TEXT);
        CREATE TABLE IF NOT EXISTS envio_items (
            envio TEXT, sku TEXT, enviadas INTEGER, recibidas INTEGER, PRIMARY KEY (envio, sku));
        CREATE TABLE IF NOT EXISTS valoraciones (  -- valoraciones de vendedor (sin datos del comprador)
            pedido TEXT, fecha TEXT, nota INTEGER, comentario TEXT, respuesta TEXT, PRIMARY KEY (pedido, fecha));
        CREATE TABLE IF NOT EXISTS sync_permisos (  -- informes que Amazon no deja descargar por falta de un rol
            clave TEXT PRIMARY KEY, mensaje TEXT, ts TEXT);
        CREATE TABLE IF NOT EXISTS trafico (  -- informe de ventas y tráfico: sesiones y conversión por ASIN y día
            fecha TEXT, asin TEXT, sesiones INTEGER, paginas INTEGER, uds INTEGER, pedidos INTEGER, buybox REAL,
            PRIMARY KEY (fecha, asin));
        CREATE TABLE IF NOT EXISTS tarifas_estimadas (  -- tarifa de Amazon para SKUs aún sin ventas liquidadas
            sku TEXT PRIMARY KEY, fecha TEXT, comision REAL, fba REAL);
    """)
    if "pais" not in [r[1] for r in con.execute("PRAGMA table_info(pedidos)")]:
        con.execute("ALTER TABLE pedidos ADD COLUMN pais TEXT")
        con.execute("ALTER TABLE pedidos ADD COLUMN cp TEXT")


# ---------------------------------------------------------------- movimientos de Amazon (Finances API)
def fetch_events(after, before):
    """Todos los FinancialEvents publicados entre dos fechas ISO (UTC), siguiendo las páginas."""
    params = {"PostedAfter": after, "PostedBefore": before, "MaxResultsPerPage": 100}
    events = {}
    while True:
        for attempt in range(6):
            r = requests.get(f"{sn.ENDPOINT}/finances/v0/financialEvents", params=params,
                             headers={"x-amz-access-token": sn.access_token()}, timeout=90)
            if r.status_code != 429:  # límite: 0,5 peticiones/segundo
                break
            time.sleep(3 * (attempt + 1))
        if r.status_code in (401, 403):
            raise PermissionError("Amazon denegó el acceso a los datos financieros: revisa el rol «Finanzas y contabilidad»")
        r.raise_for_status()
        p = r.json()["payload"]
        for k, v in p["FinancialEvents"].items():
            events.setdefault(k, []).extend(v or [])
        if not p.get("NextToken"):
            return events
        params = {"NextToken": p["NextToken"]}
        time.sleep(2.1)


def _amt(m):
    return float((m or {}).get("CurrencyAmount", 0) or 0)


def flatten(events, default_date):
    """Convierte los eventos de Amazon en líneas (posted, tipo, pedido, sku, cantidad, concepto, importe).
    Los cargos de servicio no traen fecha: se les pone default_date (inicio del tramo descargado)."""
    lines = []

    def item_lines(e, tipo, items_key, charges_key, fees_key, promos_key):
        for it in e.get(items_key) or []:
            sku, qty = it.get("SellerSKU"), it.get("QuantityShipped") or 0
            base = (e.get("PostedDate"), tipo, e.get("AmazonOrderId"), sku)
            lines.append(base + (qty, "UNIDADES", 0.0))
            for c in it.get(charges_key) or []:
                lines.append(base + (0, c["ChargeType"], _amt(c.get("ChargeAmount"))))
            for f in it.get(fees_key) or []:
                lines.append(base + (0, f["FeeType"], _amt(f.get("FeeAmount"))))
            for p in it.get(promos_key) or []:
                lines.append(base + (0, "Promotion", _amt(p.get("PromotionAmount"))))
            for t in it.get("ItemTaxWithheldList") or []:
                for c in t.get("TaxesWithheld") or []:
                    lines.append(base + (0, "Withheld:" + c["ChargeType"], _amt(c.get("ChargeAmount"))))

    for e in events.get("ShipmentEventList", []):
        item_lines(e, "venta", "ShipmentItemList", "ItemChargeList", "ItemFeeList", "PromotionList")
    for e in events.get("RefundEventList", []):
        item_lines(e, "devolucion", "ShipmentItemAdjustmentList", "ItemChargeAdjustmentList",
                   "ItemFeeAdjustmentList", "PromotionAdjustmentList")
    for e in events.get("AdjustmentEventList", []):
        items = e.get("AdjustmentItemList") or []
        for it in items:
            lines.append((e.get("PostedDate"), "ajuste", "", it.get("SellerSKU"), int(it.get("Quantity") or 0),
                          e.get("AdjustmentType"), _amt(it.get("TotalAmount"))))
        if not items:
            lines.append((e.get("PostedDate"), "ajuste", "", None, 0, e.get("AdjustmentType"), _amt(e.get("AdjustmentAmount"))))
    for e in events.get("ServiceFeeEventList", []):
        for f in e.get("FeeList") or []:
            lines.append((e.get("PostedDate") or default_date, "servicio", e.get("AmazonOrderId") or "",
                          e.get("SellerSKU") or None, 0, f["FeeType"], _amt(f.get("FeeAmount"))))
    for e in events.get("ProductAdsPaymentEventList", []):
        lines.append((e.get("postedDate"), "publicidad", e.get("invoiceId") or "", None, 0,
                      "ProductAds", _amt(e.get("transactionValue"))))
    # Otros tipos (préstamos, recuperación de deuda, etc.) son movimientos de caja, no beneficio: se ignoran.
    return [l for l in lines if l[0]]


def sync_returns(progress=print):
    """Informe de devoluciones FBA: en qué estado volvió cada unidad (solo SELLABLE vuelve a tu stock)."""
    import gzip
    base = f"{sn.ENDPOINT}/reports/2021-06-30"
    hdr = lambda: {"x-amz-access-token": sn.access_token(), "content-type": "application/json"}
    r = requests.post(f"{base}/reports", headers=hdr(), timeout=60, json={
        "reportType": "GET_FBA_FULFILLMENT_CUSTOMER_RETURNS_DATA", "marketplaceIds": [sn.MARKETPLACE_ID],
        "dataStartTime": DEFAULT_FROM + "T00:00:00Z"})
    r.raise_for_status()
    rid = r.json()["reportId"]
    progress("Pidiendo a Amazon el informe de devoluciones…")
    for _ in range(90):
        time.sleep(10)
        st = requests.get(f"{base}/reports/{rid}", headers=hdr(), timeout=60).json()
        if st["processingStatus"] in ("DONE", "CANCELLED", "FATAL"):
            break
    if st["processingStatus"] != "DONE":  # CANCELLED = sin datos nuevos
        progress(f"Informe de devoluciones: {st['processingStatus']}")
        return 0
    doc = requests.get(f"{base}/documents/{st['reportDocumentId']}", headers=hdr(), timeout=60).json()
    raw = requests.get(doc["url"], timeout=120).content
    if doc.get("compressionAlgorithm") == "GZIP":
        raw = gzip.decompress(raw)
    lines = [l.split("\t") for l in raw.decode("utf-8", "replace").strip().splitlines()]
    rows = []
    for x in (dict(zip(lines[0], l)) for l in lines[1:]):
        when = datetime.fromisoformat(x["return-date"].replace("Z", "+00:00"))
        rows.append((x["return-date"], when.astimezone(sn.TZ).strftime("%Y-%m"), x["order-id"], x["sku"],
                     int(x["quantity"] or 1), x["detailed-disposition"], x["reason"],
                     f"{x['order-id']}|{x.get('license-plate-number') or x['sku']}|{x['return-date']}"))
    con = sn.db()
    ensure_tables(con)
    with con:
        con.executemany("INSERT OR REPLACE INTO devoluciones_fba VALUES (?,?,?,?,?,?,?,?)", rows)
    progress(f"{len(rows)} devoluciones físicas guardadas")
    return len(rows)


def _get(path, params=None):
    for attempt in range(8):
        r = requests.get(f"{sn.ENDPOINT}{path}", params=params, timeout=60,
                         headers={"x-amz-access-token": sn.access_token()})
        if r.status_code != 429:  # la API de pedidos permite ráfagas cortas y luego 1 petición/minuto
            r.raise_for_status()
            return r.json()["payload"]
        time.sleep(min(10 * (attempt + 1), 60))
    raise RuntimeError("Amazon sigue limitando las peticiones (429)")


def sync_orders(full=False, progress=print, recent=False):
    """Pedidos con su fecha de compra (Orders API) y artículos de los que aún no se han liquidado.
    recent=True solo pide los pedidos nuevos o cambiados desde la última vez (el monitor lo hace cada pocos minutos)."""
    con = sn.db()
    ensure_tables(con)
    has, last = con.execute("SELECT COUNT(*), MAX(actualizado) FROM pedidos").fetchone()
    after = DEFAULT_FROM + "T00:00:00Z"
    if has and not full:
        after = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    since = {"CreatedAfter": after}
    if recent and has and last:  # margen de 2 h por si Amazon publica algún cambio con retraso
        changed = datetime.fromisoformat(last.replace("Z", "+00:00")) - timedelta(hours=2)
        since = {"LastUpdatedAfter": changed.strftime("%Y-%m-%dT%H:%M:%SZ")}
    progress("Descargando pedidos…")
    params, orders = {"MarketplaceIds": sn.MARKETPLACE_ID, **since, "MaxResultsPerPage": 100}, []
    while True:
        p = _get("/orders/v0/orders", params)
        orders += p.get("Orders", [])
        if not p.get("NextToken"):
            break
        params = {"MarketplaceIds": sn.MARKETPLACE_ID, "NextToken": p["NextToken"]}
        time.sleep(1)
    seen = dict(con.execute("SELECT pedido, actualizado FROM pedidos"))
    touched = {o["AmazonOrderId"] for o in orders if seen.get(o["AmazonOrderId"]) != o.get("LastUpdateDate")}
    with con:
        con.executemany("INSERT OR REPLACE INTO pedidos(pedido, compra, estado, total, actualizado, pais, cp) "
                        "VALUES (?,?,?,?,?,?,?)", [
            (o["AmazonOrderId"], o["PurchaseDate"], o["OrderStatus"],
             float((o.get("OrderTotal") or {}).get("Amount") or 0), o.get("LastUpdateDate"),
             (o.get("ShippingAddress") or {}).get("CountryCode"), (o.get("ShippingAddress") or {}).get("PostalCode"))
            for o in orders])
    settled = {r[0] for r in con.execute("SELECT DISTINCT pedido FROM fin_lineas WHERE tipo='venta'")}
    pending = [r[0] for r in con.execute("SELECT pedido FROM pedidos WHERE estado != 'Canceled'") if r[0] not in settled]
    # Los artículos de un pedido no cambian: solo se piden los de pedidos nuevos, los que acaban de cambiar
    # y los «Pending», que Amazon da sin precio hasta que se envían
    known = {r[0] for r in con.execute("SELECT DISTINCT pedido FROM pedido_items WHERE precio > 0")}
    fetch = [p for p in pending if full or p not in known or p in touched]
    items = []
    for pedido in fetch:
        for it in _get(f"/orders/v0/orders/{pedido}/orderItems").get("OrderItems", []):
            items.append((pedido, it.get("SellerSKU"), int(it.get("QuantityOrdered") or 0),
                          float((it.get("ItemPrice") or {}).get("Amount") or 0),
                          float((it.get("PromotionDiscount") or {}).get("Amount") or 0)))
        time.sleep(2.1)  # 0,5 peticiones/segundo
    with con:
        con.executemany("DELETE FROM pedido_items WHERE pedido=?", [(p,) for p in fetch])
        con.executemany("INSERT INTO pedido_items VALUES (?,?,?,?,?)", items)
        keep = set(pending)  # fuera los ya liquidados o cancelados
        gone = [r[0] for r in con.execute("SELECT DISTINCT pedido FROM pedido_items") if r[0] not in keep]
        con.executemany("DELETE FROM pedido_items WHERE pedido=?", [(p,) for p in gone])
    progress(f"{len(orders)} pedidos · {len(pending)} aún sin liquidar · {len(fetch)} consultados")
    try:
        estimate_new_fees(con, con.execute("SELECT * FROM pedido_items").fetchall())
    except Exception as e:  # sin tarifa estimada se usa la media de la cuenta
        progress(f"No se pudo consultar la tarifa de Amazon: {e}")
    return len(orders)


def estimate_new_fees(con, items):
    """Para SKUs vendidos sin apenas ventas liquidadas, guarda la tarifa que Amazon calcula para su precio
    (comisión como fracción del precio con IVA y FBA por unidad). Se refresca cada 7 días."""
    import analizador as an
    rates, _ = fee_rates(con, estimated=False)
    old = (datetime.now(sn.TZ) - timedelta(days=7)).date().isoformat()
    fresh = {r[0] for r in con.execute("SELECT sku FROM tarifas_estimadas WHERE fecha >= ?", (old,))}
    asin = dict(con.execute("SELECT sku, asin FROM costes WHERE asin != ''"))
    try:
        listed = dict(con.execute("SELECT sku, precio FROM listings WHERE ts=(SELECT MAX(ts) FROM listings) AND precio > 0"))
    except Exception:
        listed = {}
    prices = {}
    for _, sku, qty, precio, promo in items:
        if sku and sku not in rates and sku not in fresh and asin.get(sku) and qty:
            p = (precio - (promo or 0)) / qty if precio else listed.get(sku)
            if p:
                prices[sku] = p
    if not prices:
        return
    fees = an.fees_batch({asin[s]: p for s, p in prices.items()}, lambda f: None)
    today = datetime.now(sn.TZ).date().isoformat()
    with con:
        for sku, p in prices.items():
            if asin[sku] in fees:
                total, _, fba = fees[asin[sku]]
                # Amazon añade un 3 % de impuesto de servicios digitales sobre la comisión
                con.execute("INSERT OR REPLACE INTO tarifas_estimadas VALUES (?,?,?,?)",
                            (sku, today, -(total - fba) * 1.03 / p, -fba))


def request_reports(specs, progress=print, wait_minutes=15):
    """Pide varios informes a la vez y devuelve {clave: bytes del documento o None si Amazon no lo da}.
    specs = {clave: (tipo de informe, opciones extra del cuerpo)}."""
    import gzip
    base = f"{sn.ENDPOINT}/reports/2021-06-30"
    hdr = lambda: {"x-amz-access-token": sn.access_token(), "content-type": "application/json"}
    ids, out = {}, {}
    for key, (rtype, extra) in specs.items():
        for attempt in range(6):  # Amazon deja pedir pocos informes seguidos: si dice 429, se espera y se repite
            r = requests.post(f"{base}/reports", headers=hdr(), timeout=60,
                              json={"reportType": rtype, "marketplaceIds": [sn.MARKETPLACE_ID], **extra})
            if r.status_code != 429:
                break
            time.sleep(60)
        if r.status_code in (401, 403):
            raise PermissionError(f"Amazon no da acceso al informe {rtype}: falta un rol en la app de SP-API")
        r.raise_for_status()
        ids[key] = r.json()["reportId"]
        time.sleep(1)
    deadline = time.time() + wait_minutes * 60
    while ids and time.time() < deadline:
        time.sleep(15)
        for key, rid in list(ids.items()):
            st = requests.get(f"{base}/reports/{rid}", headers=hdr(), timeout=60).json()
            if st.get("processingStatus") not in ("DONE", "CANCELLED", "FATAL"):
                continue
            del ids[key]
            out[key] = None
            if st["processingStatus"] == "DONE":
                doc = requests.get(f"{base}/documents/{st['reportDocumentId']}", headers=hdr(), timeout=60).json()
                raw = requests.get(doc["url"], timeout=120).content
                out[key] = gzip.decompress(raw) if doc.get("compressionAlgorithm") == "GZIP" else raw
    for key in ids:
        out[key] = None
        progress(f"El informe {key} no estuvo listo a tiempo")
    return out


def sync_reimbursements(progress=print):
    """Compensaciones de Amazon (devoluciones no recibidas, unidades perdidas o dañadas en el almacén)."""
    raw = request_reports({"r": ("GET_FBA_REIMBURSEMENTS_DATA", {"dataStartTime": DEFAULT_FROM + "T00:00:00Z"})},
                          progress)["r"]
    if raw is None:
        return 0
    lines = [l.split("\t") for l in raw.decode("utf-8", "replace").strip().splitlines()]
    rows = []
    for x in (dict(zip(lines[0], l)) for l in lines[1:]):
        num = lambda k: float(x.get(k) or 0)
        rows.append((x["reimbursement-id"] + "|" + x.get("sku", ""), x["approval-date"], x.get("amazon-order-id") or "",
                     x.get("reason") or "", x.get("sku") or "", num("amount-total"),
                     int(num("quantity-reimbursed-cash")), int(num("quantity-reimbursed-inventory"))))
    con = sn.db()
    ensure_tables(con)
    with con:
        con.executemany("INSERT OR REPLACE INTO compensaciones VALUES (?,?,?,?,?,?,?,?)", rows)
    progress(f"{len(rows)} compensaciones de Amazon")
    return len(rows)


def sync_traffic(progress=print, full=False):
    """Sesiones, páginas vistas y conversión por ASIN y mes (informe de ventas y tráfico de Amazon).
    Se piden los meses que faltan y siempre el mes en curso y el anterior (Amazon los completa con retraso)."""
    con = sn.db()
    ensure_tables(con)
    today = datetime.now(sn.TZ).date()
    have = {r[0] for r in con.execute("SELECT DISTINCT fecha FROM trafico")}
    months, d = [], date.fromisoformat(DEFAULT_FROM)
    while d <= today:
        months.append(d)
        d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    recent = {m for m in months if (today.year - m.year) * 12 + today.month - m.month <= 1}
    todo = [m for m in months if full or m in recent or m.isoformat() not in have]
    specs = {}
    for m in todo:
        last = min((m.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1), today - timedelta(days=1))
        if last < m:
            continue
        specs[m.isoformat()] = ("GET_SALES_AND_TRAFFIC_REPORT", {
            "dataStartTime": m.isoformat(), "dataEndTime": last.isoformat(),
            "reportOptions": {"dateGranularity": "MONTH", "asinGranularity": "CHILD"}})
    if not specs:
        return 0
    import json
    n = 0
    for month, raw in request_reports(specs, progress).items():
        if raw is None:
            continue
        rows = []
        for a in json.loads(raw).get("salesAndTrafficByAsin") or []:
            t, s_ = a.get("trafficByAsin") or {}, a.get("salesByAsin") or {}
            rows.append((month, a.get("childAsin"), t.get("sessions") or 0, t.get("pageViews") or 0,
                         s_.get("unitsOrdered") or 0, s_.get("totalOrderItems") or 0, t.get("buyBoxPercentage")))
        with con:
            con.execute("DELETE FROM trafico WHERE fecha=?", (month,))
            con.executemany("INSERT INTO trafico VALUES (?,?,?,?,?,?,?)", rows)
        n += len(rows)
    progress(f"Tráfico: {len(specs)} meses, {n} filas")
    return n


SHIPMENT_STATES = "WORKING,READY_TO_SHIP,SHIPPED,RECEIVING,CANCELLED,DELETED,CLOSED,ERROR,IN_TRANSIT,DELIVERED,CHECKED_IN"


def _permission(con, key, error=None):
    """Anota (o borra, si error es None) que Amazon no deja descargar algo por falta de un rol en la app."""
    with con:
        if error:
            con.execute("INSERT OR REPLACE INTO sync_permisos VALUES (?,?,?)",
                        (key, str(error)[:300], datetime.now(timezone.utc).isoformat()))
        else:
            con.execute("DELETE FROM sync_permisos WHERE clave=?", (key,))


def sync_shipments(progress=print):
    """Envíos a FBA y, por SKU, unidades enviadas y recibidas (para ver si Amazon perdió alguna al recibirlas).
    Necesita el rol «Amazon Fulfillment» en la app de SP-API."""
    con = sn.db()
    ensure_tables(con)
    hdr = lambda: {"x-amz-access-token": sn.access_token()}
    params = {"MarketplaceId": sn.MARKETPLACE_ID, "QueryType": "SHIPMENT", "ShipmentStatusList": SHIPMENT_STATES}
    shipments = []
    while True:
        r = requests.get(f"{sn.ENDPOINT}/fba/inbound/v0/shipments", params=params, headers=hdr(), timeout=60)
        if r.status_code in (401, 403):
            _permission(con, "envios", "Falta el rol «Amazon Fulfillment» en la app de SP-API")
            raise PermissionError("Amazon no da acceso a los envíos FBA: falta el rol «Amazon Fulfillment»")
        if r.status_code == 429:
            time.sleep(5)
            continue
        r.raise_for_status()
        p = r.json().get("payload") or {}
        shipments += p.get("ShipmentData") or []
        if not p.get("NextToken"):
            break
        params = {"MarketplaceId": sn.MARKETPLACE_ID, "QueryType": "NEXT_TOKEN", "NextToken": p["NextToken"]}
        time.sleep(2)
    _permission(con, "envios")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    known = {i: (e, v) for i, e, v in con.execute("SELECT id, estado, visto FROM envios_fba")}
    for sh in shipments:
        sid, state = sh["ShipmentId"], sh.get("ShipmentStatus")
        old = known.get(sid)
        with con:
            con.execute("INSERT OR REPLACE INTO envios_fba VALUES (?,?,?,?,?,?)",
                        (sid, sh.get("ShipmentName") or sid, state, sh.get("DestinationFulfillmentCenterId") or "",
                         old[1] if old else now, now))
        if state in ("CANCELLED", "DELETED") or (old and old[0] == "CLOSED" and state == "CLOSED"):
            continue  # un envío cerrado ya no cambia: no hace falta volver a pedir sus artículos
        items, ip = [], {"MarketplaceId": sn.MARKETPLACE_ID}
        while True:
            time.sleep(2)  # 2 peticiones por segundo como mucho
            r = requests.get(f"{sn.ENDPOINT}/fba/inbound/v0/shipments/{sid}/items", params=ip, headers=hdr(), timeout=60)
            if r.status_code == 429:
                time.sleep(5)
                continue
            r.raise_for_status()
            p = r.json().get("payload") or {}
            items += p.get("ItemData") or []
            if not p.get("NextToken"):
                break
            ip = {"MarketplaceId": sn.MARKETPLACE_ID, "QueryType": "NEXT_TOKEN", "NextToken": p["NextToken"]}
        with con:
            con.execute("DELETE FROM envio_items WHERE envio=?", (sid,))
            con.executemany("INSERT OR REPLACE INTO envio_items VALUES (?,?,?,?)",
                            [(sid, it.get("SellerSKU"), it.get("QuantityShipped") or 0, it.get("QuantityReceived") or 0)
                             for it in items])
    progress(f"{len(shipments)} envíos FBA")
    return len(shipments)


def sync_feedback(progress=print):
    """Valoraciones de vendedor (1 a 5 estrellas). No se guarda nada del comprador.
    Necesita el rol «Selling Partner Insights» en la app de SP-API."""
    con = sn.db()
    ensure_tables(con)
    try:
        raw = request_reports({"f": ("GET_SELLER_FEEDBACK_DATA", {"dataStartTime": DEFAULT_FROM + "T00:00:00Z"})},
                              progress)["f"]
    except PermissionError as e:
        _permission(con, "valoraciones", "Falta el rol «Selling Partner Insights» en la app de SP-API")
        raise e
    _permission(con, "valoraciones")
    if raw is None:
        return 0
    lines = [l.split("\t") for l in raw.decode("utf-8", "replace").strip().splitlines()]
    head = [h.strip().lower() for h in lines[0]]
    col = lambda *names: next((i for i, h in enumerate(head) if any(n in h for n in names)), None)
    c_date, c_rating, c_comment = col("date", "fecha"), col("rating", "puntu"), col("comment", "coment")
    c_resp, c_order = col("response", "respuesta"), col("order", "pedido")
    rows = []
    for l in lines[1:]:
        get = lambda c: l[c].strip() if c is not None and c < len(l) else ""
        try:
            nota = int(float(get(c_rating)))
        except ValueError:
            continue
        rows.append((get(c_order), get(c_date), nota, get(c_comment)[:500], get(c_resp)[:500]))
    with con:
        con.executemany("INSERT OR REPLACE INTO valoraciones VALUES (?,?,?,?,?)", rows)
    progress(f"{len(rows)} valoraciones")
    return len(rows)


def sync_payouts(progress=print):
    """Liquidaciones de Amazon (grupos de movimientos): las cerradas son pagos hechos; la abierta, el próximo."""
    params, groups = {"FinancialEventGroupStartedAfter": DEFAULT_FROM + "T00:00:00Z", "MaxResultsPerPage": 100}, []
    while True:
        p = _get("/finances/v0/financialEventGroups", params)
        groups += p.get("FinancialEventGroupList") or []
        if not p.get("NextToken"):
            break
        params = {"NextToken": p["NextToken"]}
        time.sleep(2.1)
    rows = [(g["FinancialEventGroupId"], g.get("FinancialEventGroupStart"), g.get("FinancialEventGroupEnd"),
             g.get("ProcessingStatus"), g.get("FundTransferStatus"), _amt(g.get("OriginalTotal")),
             g.get("FundTransferDate"), (g.get("OriginalTotal") or {}).get("CurrencyCode")) for g in groups]
    con = sn.db()
    ensure_tables(con)
    with con:
        con.execute("DELETE FROM pagos")
        con.executemany("INSERT INTO pagos VALUES (?,?,?,?,?,?,?,?)", rows)
    progress(f"{len(rows)} liquidaciones de Amazon")
    return rows


def payouts(con):
    """Próximo pago (la liquidación abierta, con la fecha estimada según la duración de la anterior) y los anteriores."""
    ensure_tables(con)
    rows = [dict(zip(("inicio", "fin", "estado", "transferencia", "total", "fecha_pago"), r)) for r in con.execute(
        "SELECT inicio, fin, estado, transferencia, total, fecha_pago FROM pagos WHERE moneda='EUR' ORDER BY inicio DESC")]
    nxt = next((r for r in rows if r["estado"] == "Open"), None)
    done = [r for r in rows if r["estado"] == "Closed"]
    if nxt and nxt["inicio"]:
        start = datetime.fromisoformat(nxt["inicio"].replace("Z", "+00:00"))
        spans = [datetime.fromisoformat(r["fin"].replace("Z", "+00:00")) - datetime.fromisoformat(r["inicio"].replace("Z", "+00:00"))
                 for r in done[:3] if r["inicio"] and r["fin"]]
        span = max(spans) if spans else timedelta(days=14)  # Amazon.es suele liquidar cada 14 días
        nxt["estimada"] = (start + span).astimezone(sn.TZ).date()
    return nxt, done


def sync(full=False, progress=print, returns=True):
    """Descarga los movimientos nuevos (los últimos 45 días, o todo desde julio) y reemplaza ese tramo.
    returns=False omite el informe de devoluciones, que tarda varios minutos (el monitor lo pide una vez al día)."""
    con = sn.db()
    ensure_tables(con)
    last = con.execute("SELECT MAX(posted) FROM fin_lineas").fetchone()[0]
    start = DEFAULT_FROM + "T00:00:00Z"
    if last and not full:
        # Siempre desde el día 1 de un mes: los cargos de servicio no traen fecha y se fechan al inicio del
        # tramo descargado, así que hay que reemplazar meses completos para no duplicarlos.
        back = (datetime.fromisoformat(last.replace("Z", "+00:00")) - timedelta(days=45)).astimezone(sn.TZ)
        month_start = back.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        start = max(start, month_start.strftime("%Y-%m-%dT%H:%M:%SZ"))
    now = datetime.now(timezone.utc) - timedelta(minutes=5)
    progress(f"Descargando movimientos de Amazon desde {start[:10]}…")
    # Se descarga mes a mes (hora de España) para poder fechar los cargos de servicio, que no traen fecha
    rows, cur = [], datetime.fromisoformat(start.replace("Z", "+00:00"))
    while cur < now:
        local = cur.astimezone(sn.TZ)
        nxt = (local.replace(day=1, hour=0, minute=0, second=0) + timedelta(days=32)).replace(day=1)
        end = min(nxt.astimezone(timezone.utc), now)
        a, b = cur.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ")
        for p, *rest in flatten(fetch_events(a, b), a):
            rows.append((p, datetime.fromisoformat(p.replace("Z", "+00:00")).astimezone(sn.TZ).strftime("%Y-%m"), *rest))
        progress(f"  {local:%Y-%m}: descargado")
        cur = end
        time.sleep(2.1)
    with con:
        con.execute("DELETE FROM fin_lineas WHERE posted >= ?", (start,))
        con.executemany("INSERT INTO fin_lineas VALUES (?,?,?,?,?,?,?,?)", rows)
    progress(f"{len(rows)} líneas guardadas")
    sync_orders(full, progress)
    try:
        sync_payouts(progress)
    except Exception as e:  # el próximo pago es un extra: no debe parar la actualización de la rentabilidad
        progress(f"No se pudieron leer los pagos de Amazon: {e}")
    if returns:
        sync_returns(progress)
        for extra in (sync_reimbursements, sync_traffic, sync_shipments, sync_feedback):  # informes diarios: si uno falla, los demás siguen
            try:
                extra(progress)
            except Exception as e:
                progress(f"{extra.__name__}: {e}")
    return len(rows)


# ---------------------------------------------------------------- cálculo de rentabilidad
INCOME = {"Principal", "ShippingCharge", "GiftWrap", "Promotion"}  # cobrado al cliente, con IVA
VAT_ONLY = {"Tax", "ShippingTax", "GiftWrapTax"}  # IVA (junto con Withheld:* se anula; no es beneficio)
REFERRAL = {"Commission", "RefundCommission", "DigitalServicesFee", "FixedClosingFee", "VariableClosingFee"}
FBA = {"FBAPerUnitFulfillmentFee", "ShippingChargeback", "GiftwrapChargeback", "DigitalServicesFeeFBA"}
ACCOUNT_LABELS = {
    "ProductAds": "Publicidad (Sponsored Products)", "Subscription": "Suscripción de vendedor",
    "FBAInboundTransportationFee": "Envío a almacenes de Amazon", "FBAInboundTransportationProgramFee": "Envío a almacenes de Amazon",
    "FBAStorageFee": "Almacenamiento FBA", "FBALongTermStorageFee": "Almacenamiento FBA",
    "FBADisposalFee": "Retirada / eliminación de stock", "FBARemovalFee": "Retirada / eliminación de stock",
}


def _sku_row(skus, period, sku):
    return skus.setdefault(period, {}).setdefault(sku, dict(
        sku=sku, uds=0, estimadas=0, devueltas=0, vendibles=0, ventas=0.0, devoluciones=0.0, ingresos=0.0,
        comisiones=0.0, fba=0.0, reembolsos=0.0, coste=0.0, sin_coste=False,
        dev_neto=0.0, recuperado=0.0))  # dev_neto: efecto de los reembolsos (sin IVA); recuperado: coste de las vendibles


def local_date(ts):
    """Fecha en hora de España de una marca de tiempo ISO de Amazon."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(sn.TZ).date()


def period_key(d, group):
    """Clave del periodo: día (AAAA-MM-DD), semana (lunes AAAA-MM-DD) o mes (AAAA-MM)."""
    if group == "dia":
        return d.isoformat()
    if group == "semana":
        return (d - timedelta(days=d.weekday())).isoformat()
    return d.strftime("%Y-%m")


def fee_rates(con, estimated=True):
    """Por SKU: (comisión / venta con IVA, tarifa FBA por unidad) según sus ventas ya liquidadas (3 o más
    unidades); si no las hay, la tarifa que Amazon calcula para su precio (tarifas_estimadas).
    Sirve para estimar los pedidos que Amazon aún no ha liquidado."""
    acc = {}
    for sku, concepto, qty, importe in con.execute(
            "SELECT sku, concepto, cantidad, importe FROM fin_lineas WHERE tipo='venta' AND sku IS NOT NULL"):
        a = acc.setdefault(sku, [0.0, 0.0, 0.0, 0])
        if concepto in INCOME:
            a[0] += importe
        elif concepto in REFERRAL:
            a[1] += importe
        elif concepto in FBA:
            a[2] += importe
        elif concepto == "UNIDADES":
            a[3] += qty
    total = [sum(x[i] for x in acc.values()) for i in range(4)]
    default = (total[1] / total[0] if total[0] else -0.155, total[2] / total[3] if total[3] else -3.0)
    rates = {sku: (a[1] / a[0], a[2] / a[3]) for sku, a in acc.items() if a[0] and a[3] >= 3}
    if estimated:
        for sku, comision, fba in con.execute("SELECT sku, comision, fba FROM tarifas_estimadas"):
            rates.setdefault(sku, (comision, fba))
    return rates, default


# IVA general de cada país de la UE (ventas a distancia por OSS); fuera de la UE la venta va sin IVA
EU_VAT = {"AT": 20, "BE": 21, "BG": 20, "CY": 19, "CZ": 21, "DE": 19, "DK": 25, "EE": 24, "ES": 21, "FI": 25.5,
          "FR": 20, "GR": 24, "HR": 25, "HU": 27, "IE": 23, "IT": 22, "LT": 21, "LU": 17, "LV": 21, "MT": 18,
          "NL": 21, "PL": 23, "PT": 23, "RO": 21, "SE": 25, "SI": 22, "SK": 23}


def vat_rate(country, cp, iva):
    """IVA % de un pedido según su destino; iva es el del producto en España."""
    cp = str(cp or "")
    if not country or country == "ES":
        return 0.0 if cp[:2] in ("35", "38", "51", "52") else iva  # Canarias, Ceuta y Melilla: sin IVA
    if country not in EU_VAT:
        return 0.0
    if iva != EU_VAT["ES"]:  # tipo reducido: se mantiene el del producto
        return iva
    if country == "PT" and cp[:1] == "9":  # Madeira (90-93) y Azores (95-99)
        return 22.0 if cp[:2] < "94" else 16.0
    return float(EU_VAT[country])


def aggregate(con, start=None, end=None, group="mes"):
    """Beneficio por periodo y SKU entre dos fechas (date, incluidas) agrupado por «dia», «semana» o «mes».
    Devuelve ({periodo: {sku: fila}}, {periodo: {concepto: importe}}).
    Las ventas cuentan en la fecha del pedido (como Sellerboard); devoluciones, reembolsos y gastos de la
    cuenta en la fecha en que Amazon los registra. Los pedidos aún sin liquidar se estiman."""
    ensure_tables(con)
    purchase, dest = {}, {}
    for pedido, compra, pais, cp in con.execute("SELECT pedido, compra, pais, cp FROM pedidos"):
        purchase[pedido], dest[pedido] = compra, (pais, cp)
    costs = {}

    def cost(sku, d):
        if (sku, d) not in costs:
            costs[(sku, d)] = cost_on(con, sku, d.isoformat())
        return costs[(sku, d)]

    def take(ts):
        d = local_date(ts)
        return None if (start and d < start) or (end and d > end) else d

    skus, account = {}, {}
    for posted, tipo, pedido, sku, qty, concepto, importe in con.execute(
            "SELECT posted, tipo, pedido, sku, cantidad, concepto, importe FROM fin_lineas WHERE tipo != 'publicidad'"):
        d = take(purchase.get(pedido, posted) if tipo == "venta" else posted)
        if d is None:
            continue
        k = period_key(d, group)
        if tipo in ("venta", "devolucion", "ajuste") and sku:
            r = _sku_row(skus, k, sku)
            c, iva = cost(sku, d)
            iva = vat_rate(*dest.get(pedido, (None, None)), 21.0 if iva is None else iva)
            if concepto == "UNIDADES":
                if tipo == "venta":
                    r["uds"] += qty
                    if c is None:
                        r["sin_coste"] = True
                    else:
                        r["coste"] += qty * c
                else:
                    r["devueltas"] += qty  # reembolsada al cliente (el coste solo se recupera si vuelve vendible)
            elif concepto in INCOME:
                r["ventas" if tipo == "venta" else "devoluciones"] += importe
                r["ingresos"] += importe / (1 + iva / 100)
                if tipo == "devolucion":
                    r["dev_neto"] += importe / (1 + iva / 100)
            elif concepto in REFERRAL:
                r["comisiones"] += importe
                if tipo == "devolucion":
                    r["dev_neto"] += importe
            elif concepto in FBA:
                r["fba"] += importe
                if tipo == "devolucion":
                    r["dev_neto"] += importe
            elif tipo == "ajuste":
                r["reembolsos"] += importe  # Amazon te paga por unidades perdidas, dañadas o no devueltas
            elif concepto in VAT_ONLY or concepto.startswith("Withheld:"):
                pass
            else:
                r["comisiones"] += importe  # cualquier otra tarifa por unidad
                if tipo == "devolucion":
                    r["dev_neto"] += importe
        elif tipo in ("servicio", "ajuste"):
            label = ACCOUNT_LABELS.get(concepto, concepto)
            acc = account.setdefault(k, {})
            acc[label] = acc.get(label, 0.0) + importe

    # Publicidad: Amazon la cobra en facturas (el día 2 de cada mes, o antes si llegas al límite). Cada factura
    # se reparte a partes iguales entre los días que cubre: desde el día siguiente a la factura anterior.
    prev = None
    for posted, concepto, importe in con.execute(
            "SELECT posted, concepto, importe FROM fin_lineas WHERE tipo='publicidad' ORDER BY posted"):
        last = local_date(posted)
        first = max(prev + timedelta(days=1) if prev else last - timedelta(days=30), last - timedelta(days=30))
        first = min(max(first, date.fromisoformat(DEFAULT_FROM)), last)
        prev = last
        days = (last - first).days + 1
        label = ACCOUNT_LABELS.get(concepto, concepto)
        for i in range(days):
            d = first + timedelta(days=i)
            if (start and d < start) or (end and d > end):
                continue
            acc = account.setdefault(period_key(d, group), {})
            acc[label] = acc.get(label, 0.0) + importe / days

    # Pedidos enviados que Amazon aún no ha liquidado: comisiones y FBA estimadas con el histórico del SKU
    settled = {r[0] for r in con.execute("SELECT DISTINCT pedido FROM fin_lineas WHERE tipo='venta'")}
    rates, default = fee_rates(con)
    list_price = {}  # pedidos «Pending» llegan sin precio: se usa tu precio actual del SKU o su media vendida
    try:
        list_price = dict(con.execute("SELECT sku, precio FROM listings WHERE ts=(SELECT MAX(ts) FROM listings) AND precio > 0"))
    except Exception:  # aún no se ha consultado el estado de los listings
        pass
    for sku, gross, uds in con.execute(
            "SELECT sku, SUM(CASE WHEN concepto IN ('Principal','Promotion') THEN importe ELSE 0 END), "
            "SUM(CASE WHEN concepto='UNIDADES' THEN cantidad ELSE 0 END) FROM fin_lineas "
            "WHERE tipo='venta' AND sku IS NOT NULL GROUP BY sku"):
        if uds and sku not in list_price:
            list_price[sku] = gross / uds
    for pedido, compra, sku, qty, precio, promo in con.execute(
            "SELECT p.pedido, p.compra, i.sku, i.cantidad, i.precio, i.promo FROM pedido_items i "
            "JOIN pedidos p ON p.pedido = i.pedido WHERE p.estado != 'Canceled'"):
        d = take(compra)
        if d is None or pedido in settled:
            continue
        r = _sku_row(skus, period_key(d, group), sku)
        c, iva = cost(sku, d)
        iva = vat_rate(*dest.get(pedido, (None, None)), 21.0 if iva is None else iva)
        gross = (precio or 0) - (promo or 0)
        if not precio:
            gross = qty * list_price.get(sku, 0.0)
        comm, fba_ud = rates.get(sku, default)
        r["uds"] += qty
        r["estimadas"] += qty
        r["ventas"] += gross
        r["ingresos"] += gross / (1 + iva / 100)
        r["comisiones"] += gross * comm
        r["fba"] += qty * fba_ud
        if c is None:
            r["sin_coste"] = True
        else:
            r["coste"] += qty * c

    # Unidades devueltas al almacén en estado vendible: vuelven a tu stock, así que su coste se recupera
    for fecha, sku, qty in con.execute("SELECT fecha, sku, cantidad FROM devoluciones_fba WHERE estado='SELLABLE'"):
        d = take(fecha)
        if d is None:
            continue
        r = _sku_row(skus, period_key(d, group), sku)
        r["vendibles"] += qty
        r["coste"] -= qty * (cost(sku, d)[0] or 0)
        r["recuperado"] += qty * (cost(sku, d)[0] or 0)
    for m, concepto, importe in con.execute("SELECT mes, concepto, importe FROM gastos_manual"):
        d = datetime.strptime(m + "-01", "%Y-%m-%d").date()
        if (start and d < start.replace(day=1)) or (end and d > end):
            continue
        acc = account.setdefault(period_key(d, group), {})
        acc[concepto] = acc.get(concepto, 0.0) - abs(importe)
    for (d, concepto), importe in indirect_by_day(con, start, end).items():
        acc = account.setdefault(period_key(d, group), {})
        acc[concepto] = acc.get(concepto, 0.0) - importe
    for by_sku in skus.values():
        for r in by_sku.values():
            r["beneficio"] = r["ingresos"] + r["comisiones"] + r["fba"] + r["reembolsos"] - r["coste"]
            r["margen"] = 100 * r["beneficio"] / r["ingresos"] if r["ingresos"] else None
            r["roi"] = 100 * r["beneficio"] / r["coste"] if r["coste"] > 0 else None
    return skus, account


def indirect_by_day(con, start=None, end=None):
    """{(día, concepto): importe} de los gastos indirectos. Los mensuales se reparten entre los días de cada mes
    (así cuadran los días, semanas y meses sueltos); los puntuales cuentan el día que se pagan."""
    start = start or date.fromisoformat(DEFAULT_FROM)
    end = end or datetime.now(sn.TZ).date()
    out = {}
    for concepto, importe, frecuencia, desde, hasta in con.execute(
            "SELECT concepto, importe, frecuencia, desde, hasta FROM gastos_indirectos"):
        importe = abs(importe or 0)
        first = date.fromisoformat(desde)
        if frecuencia == "puntual":
            if start <= first <= end:
                out[(first, concepto)] = out.get((first, concepto), 0.0) + importe
            continue
        first = max(first.replace(day=1), start)  # el gasto mensual cubre el mes entero en que empieza
        last = min(date.fromisoformat(hasta) if hasta else end, end)
        if hasta:  # y también el mes entero en que termina
            nxt = (last.replace(day=28) + timedelta(days=4)).replace(day=1)
            last = min(nxt - timedelta(days=1), end)
        d = first
        while d <= last:
            nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
            share = importe / (nxt - d.replace(day=1)).days
            out[(d, concepto)] = out.get((d, concepto), 0.0) + share
            d += timedelta(days=1)
    return out


def traffic_by_month(con):
    """{(asin, AAAA-MM): {sesiones, paginas, uds}} del informe de ventas y tráfico de Amazon."""
    ensure_tables(con)
    out = {}
    for asin, mes, ses, pag, uds in con.execute(
            "SELECT asin, substr(fecha, 1, 7), SUM(sesiones), SUM(paginas), SUM(uds) FROM trafico GROUP BY 1, 2"):
        out[(asin, mes)] = dict(sesiones=ses or 0, paginas=pag or 0, uds=uds or 0)
    return out


def monthly(con, mes=None):
    """Atajo de aggregate() por meses (mes = «AAAA-MM» para uno solo)."""
    if not mes:
        return aggregate(con, group="mes")
    first = datetime.strptime(mes + "-01", "%Y-%m-%d").date()
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return aggregate(con, first, last, "mes")


# ---------------------------------------------------------------- costes de producto
def import_costs(path, desde=DEFAULT_FROM):
    """Lee la pestaña «Productos» del Excel de rentabilidad (SKU, ASIN, Producto, VAT %, Coste compra/u)."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = next(wb[s] for s in wb.sheetnames if "product" in s.lower())
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    wb.close()
    header_idx = next(i for i, r in enumerate(rows) if r and str(r[0] or "").strip().upper() == "SKU")
    headers = [str(h or "").lower() for h in rows[header_idx]]

    def col(*words):
        return next(i for i, h in enumerate(headers) if any(w in h for w in words))

    c_sku, c_asin, c_name = col("sku"), col("asin"), col("producto")
    c_vat, c_cost = col("vat", "iva"), col("coste", "cogs")
    out = []
    for r in rows[header_idx + 1:]:
        sku = str(r[c_sku] or "").strip()
        if not sku or sku.upper() == "TOTAL" or r[c_cost] in (None, ""):
            continue
        vat = float(r[c_vat] or 0)
        out.append(dict(sku=sku, asin=str(r[c_asin] or "").strip(), nombre=str(r[c_name] or "").strip(),
                        coste=float(r[c_cost]), iva=vat * 100 if vat < 1 else vat, desde=desde))
    con = sn.db()
    ensure_tables(con)
    con.executemany(
        "INSERT INTO costes(sku, desde, coste, envio_fba, iva, asin, nombre) VALUES (:sku, :desde, :coste, 0, :iva, :asin, :nombre) "
        "ON CONFLICT(sku, desde) DO UPDATE SET coste=excluded.coste, iva=excluded.iva, asin=excluded.asin, nombre=excluded.nombre",
        out)
    con.commit()
    return out


def costs_table(con):
    ensure_tables(con)
    return con.execute("SELECT sku, desde, coste, envio_fba, iva, asin, nombre FROM costes ORDER BY sku, desde").fetchall()


def cost_on(con, sku, day):
    """(coste + envío a FBA, IVA %) vigentes para un SKU en una fecha (AAAA-MM-DD)."""
    r = con.execute("SELECT coste + envio_fba, iva FROM costes WHERE sku=? AND desde<=? ORDER BY desde DESC LIMIT 1",
                    (sku, day)).fetchone()
    return r if r else (None, None)


# ---------------------------------------------------------------- línea de comandos
def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "costes":
        if len(sys.argv) > 2:
            n = import_costs(sys.argv[2])
            print(f"{len(n)} costes importados (desde {DEFAULT_FROM})")
        for sku, desde, coste, envio, iva, asin, nombre in costs_table(sn.db()):
            print(f"  {sku:<26} {asin:<11} desde {desde}  coste {coste:6.2f} €  envío FBA {envio:4.2f} €  IVA {iva:g}%")
        return
    if cmd == "descargar":
        sync(full="--todo" in sys.argv)
        return
    if cmd == "informe":
        con = sn.db()
        skus, account = monthly(con, sys.argv[2] if len(sys.argv) > 2 else None)
        for m in sorted(set(skus) | set(account)):
            rows = sorted(skus.get(m, {}).values(), key=lambda r: -r["beneficio"])
            print(f"\n{m}   {'uds':>4} {'dev':>3} {'ventas':>9} {'ingr.s/IVA':>10} {'comis.':>8} {'FBA':>8} {'reemb.':>7} {'coste':>8} {'BENEF.':>8} {'margen':>7}")
            for r in rows:
                mg = f"{r['margen']:6.1f}%" if r["margen"] is not None else "      -"
                print(f"  {r['sku']:<25} {r['uds']:4} {r['devueltas']:3} {r['ventas']:9.2f} {r['ingresos']:10.2f} {r['comisiones']:8.2f} "
                      f"{r['fba']:8.2f} {r['reembolsos']:7.2f} {r['coste']:8.2f} {r['beneficio']:8.2f} {mg}")
            prod = sum(r["beneficio"] for r in rows)
            acc = account.get(m, {})
            print(f"  {'Beneficio de productos':<62} {prod:39.2f}")
            for k, v in sorted(acc.items()):
                print(f"    {k:<60} {v:39.2f}")
            print(f"  {'BENEFICIO DEL MES':<62} {prod + sum(acc.values()):39.2f}")
        return
    sys.exit(__doc__)


if __name__ == "__main__":
    main()
