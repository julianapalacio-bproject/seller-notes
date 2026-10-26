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
from datetime import datetime, timedelta, timezone

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
    """)


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


def sync_orders(full=False, progress=print):
    """Pedidos con su fecha de compra (Orders API) y artículos de los que aún no se han liquidado."""
    con = sn.db()
    ensure_tables(con)
    has = con.execute("SELECT COUNT(*) FROM pedidos").fetchone()[0]
    after = DEFAULT_FROM + "T00:00:00Z"
    if has and not full:
        after = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    progress("Descargando pedidos…")
    params, orders = {"MarketplaceIds": sn.MARKETPLACE_ID, "CreatedAfter": after, "MaxResultsPerPage": 100}, []
    while True:
        p = _get("/orders/v0/orders", params)
        orders += p.get("Orders", [])
        if not p.get("NextToken"):
            break
        params = {"MarketplaceIds": sn.MARKETPLACE_ID, "NextToken": p["NextToken"]}
        time.sleep(1)
    with con:
        con.executemany("INSERT OR REPLACE INTO pedidos VALUES (?,?,?,?,?)", [
            (o["AmazonOrderId"], o["PurchaseDate"], o["OrderStatus"],
             float((o.get("OrderTotal") or {}).get("Amount") or 0), o.get("LastUpdateDate")) for o in orders])
    settled = {r[0] for r in con.execute("SELECT DISTINCT pedido FROM fin_lineas WHERE tipo='venta'")}
    pending = [r[0] for r in con.execute("SELECT pedido FROM pedidos WHERE estado != 'Canceled'") if r[0] not in settled]
    items = []
    for i, pedido in enumerate(pending):
        for it in _get(f"/orders/v0/orders/{pedido}/orderItems").get("OrderItems", []):
            items.append((pedido, it.get("SellerSKU"), int(it.get("QuantityOrdered") or 0),
                          float((it.get("ItemPrice") or {}).get("Amount") or 0),
                          float((it.get("PromotionDiscount") or {}).get("Amount") or 0)))
        time.sleep(2.1)  # 0,5 peticiones/segundo
    with con:
        con.execute("DELETE FROM pedido_items")
        con.executemany("INSERT INTO pedido_items VALUES (?,?,?,?,?)", items)
    progress(f"{len(orders)} pedidos · {len(pending)} aún sin liquidar")
    return len(orders)


def sync(full=False, progress=print):
    """Descarga los movimientos nuevos (los últimos 45 días, o todo desde julio) y reemplaza ese tramo."""
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
    sync_returns(progress)
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
        comisiones=0.0, fba=0.0, reembolsos=0.0, coste=0.0, sin_coste=False))


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


def fee_rates(con):
    """Por SKU: (comisión / venta con IVA, tarifa FBA por unidad) según sus ventas ya liquidadas.
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
    rates = {sku: (a[1] / a[0], a[2] / a[3]) for sku, a in acc.items() if a[0] and a[3]}
    return rates, default


def aggregate(con, start=None, end=None, group="mes"):
    """Beneficio por periodo y SKU entre dos fechas (date, incluidas) agrupado por «dia», «semana» o «mes».
    Devuelve ({periodo: {sku: fila}}, {periodo: {concepto: importe}}).
    Las ventas cuentan en la fecha del pedido (como Sellerboard); devoluciones, reembolsos y gastos de la
    cuenta en la fecha en que Amazon los registra. Los pedidos aún sin liquidar se estiman."""
    ensure_tables(con)
    purchase = dict(con.execute("SELECT pedido, compra FROM pedidos"))
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
            "SELECT posted, tipo, pedido, sku, cantidad, concepto, importe FROM fin_lineas"):
        d = take(purchase.get(pedido, posted) if tipo == "venta" else posted)
        if d is None:
            continue
        k = period_key(d, group)
        if tipo in ("venta", "devolucion", "ajuste") and sku:
            r = _sku_row(skus, k, sku)
            c, iva = cost(sku, d)
            iva = 21.0 if iva is None else iva
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
            elif concepto in REFERRAL:
                r["comisiones"] += importe
            elif concepto in FBA:
                r["fba"] += importe
            elif tipo == "ajuste":
                r["reembolsos"] += importe  # Amazon te paga por unidades perdidas, dañadas o no devueltas
            elif concepto in VAT_ONLY or concepto.startswith("Withheld:"):
                pass
            else:
                r["comisiones"] += importe  # cualquier otra tarifa por unidad
        elif tipo in ("servicio", "publicidad", "ajuste"):
            label = ACCOUNT_LABELS.get(concepto, concepto)
            acc = account.setdefault(k, {})
            acc[label] = acc.get(label, 0.0) + importe

    # Pedidos enviados que Amazon aún no ha liquidado: comisiones y FBA estimadas con el histórico del SKU
    settled = {r[0] for r in con.execute("SELECT DISTINCT pedido FROM fin_lineas WHERE tipo='venta'")}
    rates, default = fee_rates(con)
    for pedido, compra, sku, qty, precio, promo in con.execute(
            "SELECT p.pedido, p.compra, i.sku, i.cantidad, i.precio, i.promo FROM pedido_items i "
            "JOIN pedidos p ON p.pedido = i.pedido WHERE p.estado != 'Canceled'"):
        d = take(compra)
        if d is None or pedido in settled:
            continue
        r = _sku_row(skus, period_key(d, group), sku)
        c, iva = cost(sku, d)
        iva = 21.0 if iva is None else iva
        gross = (precio or 0) - (promo or 0)
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
    for m, concepto, importe in con.execute("SELECT mes, concepto, importe FROM gastos_manual"):
        d = datetime.strptime(m + "-01", "%Y-%m-%d").date()
        if (start and d < start.replace(day=1)) or (end and d > end):
            continue
        acc = account.setdefault(period_key(d, group), {})
        acc[concepto] = acc.get(concepto, 0.0) - abs(importe)
    for by_sku in skus.values():
        for r in by_sku.values():
            r["beneficio"] = r["ingresos"] + r["comisiones"] + r["fba"] + r["reembolsos"] - r["coste"]
            r["margen"] = 100 * r["beneficio"] / r["ingresos"] if r["ingresos"] else None
            r["roi"] = 100 * r["beneficio"] / r["coste"] if r["coste"] > 0 else None
    return skus, account


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
