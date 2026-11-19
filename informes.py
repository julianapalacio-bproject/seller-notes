"""Informes descargables en Excel (página Informes del panel).

Cada informe es una función (con, desde, hasta) → lista de hojas [(nombre, cabeceras, filas, formatos)].
Usa los mismos cálculos que Profit (rentabilidad.aggregate), así que las cifras cuadran con el panel.
"""
import io
from collections import defaultdict
from datetime import date, datetime, timedelta

import rentabilidad as rt
import seller_notes as sn

EUR, INT, PCT, DAY = '#,##0.00 "€"', "0", '0.0" %"', "DD/MM/YYYY"
MONTHS = ["", "Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
REASONS = {"ORDERED_WRONG_ITEM": "Pidió el producto equivocado", "UNWANTED_ITEM": "Ya no lo quiere",
           "NOT_AS_DESCRIBED": "No es como se describe", "FOUND_BETTER_PRICE": "Encontró mejor precio",
           "NO_REASON_GIVEN": "Sin motivo", "SWITCHEROO": "Devolvió otro artículo", "NOT_COMPATIBLE": "No le sirve",
           "QUALITY_UNACCEPTABLE": "Mala calidad", "DEFECTIVE": "Defectuoso", "DAMAGED_BY_CARRIER": "Dañado en el envío",
           "UNDELIVERABLE_UNKNOWN": "No se pudo entregar", "UNDELIVERABLE_REFUSED": "Rechazado en la entrega",
           "MISSED_ESTIMATED_DELIVERY": "Llegó tarde"}
STATES = {"SELLABLE": "Vendible", "CUSTOMER_DAMAGED": "Dañado por el cliente", "DEFECTIVE": "Defectuoso",
          "CARRIER_DAMAGED": "Dañado en el envío", "DISTRIBUTOR_DAMAGED": "Dañado en el almacén", "EXPIRED": "Caducado"}


def names(con):
    """Nombre de cada SKU: el de tus costes o, si no, el de tus listings."""
    out = {}
    try:
        out.update({sku: n for sku, n in con.execute(
            "SELECT sku, nombre FROM listings WHERE ts=(SELECT MAX(ts) FROM listings)") if n})
    except Exception:
        pass
    out.update({r[0]: r[6] for r in rt.costs_table(con) if r[6]})
    return out


def _days(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _period_keys(start, end, group):
    return sorted({rt.period_key(d, group) for d in _days(start, end)})


def _label(key, group):
    return f"{MONTHS[int(key[5:7])]} {key[:4]}" if group == "mes" else date.fromisoformat(key)


# ---------------------------------------------------------------- Profit por día / mes / producto
SKU_COLS = [("uds", "Uds vendidas", INT), ("estimadas", "Uds sin liquidar", INT), ("ventas", "Ventas con IVA", EUR),
            ("devueltas", "Uds reembolsadas", INT), ("devoluciones", "Reembolsado a clientes", EUR),
            ("ingresos", "Ingresos sin IVA", EUR), ("comisiones", "Comisiones", EUR), ("fba", "Tarifas FBA", EUR),
            ("reembolsos", "Reembolsos de Amazon", EUR), ("coste", "COG", EUR), ("beneficio", "Beneficio de productos", EUR)]


def _sum(rows, k):
    return sum(r[k] for r in rows)


def profit_by_period(con, start, end, group):
    skus, account = rt.aggregate(con, start, end, group)
    concepts = sorted({c for by in account.values() for c in by})
    head = ["Día" if group == "dia" else "Mes"] + [h for _, h, _ in SKU_COLS] + concepts + \
        ["Gastos de la cuenta", "Beneficio neto", "Margen neto"]
    fmts = [DAY if group == "dia" else None] + [f for *_, f in SKU_COLS] + [EUR] * len(concepts) + [EUR, EUR, PCT]
    rows, tot = [], defaultdict(float)
    for k in _period_keys(start, end, group):
        by = list(skus.get(k, {}).values())
        vals = {key: _sum(by, key) for key, _, _ in SKU_COLS}
        vals["coste"] = -vals["coste"]
        acc = account.get(k, {})
        cuenta = sum(acc.values())
        neto = vals["beneficio"] + cuenta
        for key in vals:
            tot[key] += vals[key]
        for c in concepts:
            tot[c] += acc.get(c, 0.0)
        tot["cuenta"] += cuenta
        rows.append([_label(k, group)] + [vals[key] for key, _, _ in SKU_COLS] + [acc.get(c, 0.0) for c in concepts] +
                    [cuenta, neto, 100 * neto / vals["ingresos"] if vals["ingresos"] else None])
    neto = tot["beneficio"] + tot["cuenta"]
    rows.append(["TOTAL"] + [tot[key] for key, _, _ in SKU_COLS] + [tot[c] for c in concepts] +
                [tot["cuenta"], neto, 100 * neto / tot["ingresos"] if tot["ingresos"] else None])
    return [("Profit", head, rows, fmts)]


def profit_by_day(con, start, end):
    return profit_by_period(con, start, end, "dia")


def profit_by_month(con, start, end):
    return profit_by_period(con, start, end, "mes")


def profit_by_product(con, start, end):
    skus, _ = rt.aggregate(con, start, end, "dia")
    nm = names(con)
    head = ["Día", "SKU", "Producto"] + [h for _, h, _ in SKU_COLS] + ["Margen"]
    fmts = [DAY, None, None] + [f for *_, f in SKU_COLS] + [PCT]
    rows = []
    for k in sorted(skus):
        for sku, r in sorted(skus[k].items()):
            rows.append([date.fromisoformat(k), sku, nm.get(sku, "")] +
                        [-r[key] if key == "coste" else r[key] for key, _, _ in SKU_COLS] + [r["margen"]])
    return [("Por producto y día", head, rows, fmts)]


# ---------------------------------------------------------------- pedidos
def orders(con, start, end):
    nm = names(con)
    lo, hi = start.isoformat(), (end + timedelta(days=1)).isoformat()
    lines = defaultdict(lambda: dict(uds=0, ventas=0.0, liquidado=False, reemb=0))
    for pedido, tipo, sku, qty, concepto, importe in con.execute(
            "SELECT pedido, tipo, sku, cantidad, concepto, importe FROM fin_lineas "
            "WHERE tipo IN ('venta', 'devolucion') AND sku IS NOT NULL AND pedido IN "
            "(SELECT pedido FROM pedidos WHERE compra >= ? AND compra < ?)", (lo, hi)):
        l = lines[(pedido, sku)]
        if tipo == "venta":
            l["liquidado"] = True
            if concepto == "UNIDADES":
                l["uds"] += qty
            elif concepto in rt.INCOME:
                l["ventas"] += importe
        elif concepto == "UNIDADES":
            l["reemb"] += qty
    for pedido, sku, qty, precio, promo in con.execute(
            "SELECT i.pedido, i.sku, i.cantidad, i.precio, i.promo FROM pedido_items i JOIN pedidos p ON p.pedido=i.pedido "
            "WHERE p.compra >= ? AND p.compra < ?", (lo, hi)):
        l = lines[(pedido, sku)]
        if not l["liquidado"]:
            l["uds"] += qty
            l["ventas"] += (precio or 0) - (promo or 0)
    head = ["Fecha", "Hora", "Pedido", "Estado", "País", "SKU", "Producto", "Uds", "Importe con IVA", "Liquidado por Amazon",
            "Uds reembolsadas"]
    fmts = [DAY, None, None, None, None, None, None, INT, EUR, None, INT]
    status = {"Shipped": "Enviado", "Pending": "Pendiente", "Unshipped": "Sin enviar", "Canceled": "Cancelado"}
    rows = []
    for pedido, compra, estado, pais in con.execute(
            "SELECT pedido, compra, estado, pais FROM pedidos WHERE compra >= ? AND compra < ? ORDER BY compra DESC", (lo, hi)):
        local = datetime.fromisoformat(compra.replace("Z", "+00:00")).astimezone(sn.TZ)
        items = [(sku, l) for (p, sku), l in lines.items() if p == pedido] or [("", dict(uds=0, ventas=0.0, liquidado=False, reemb=0))]
        for sku, l in items:
            rows.append([local.date(), local.strftime("%H:%M"), pedido, status.get(estado, estado), pais or "", sku,
                         nm.get(sku, ""), l["uds"], l["ventas"] or None, "Sí" if l["liquidado"] else "No",
                         l["reemb"] or None])
    return [("Pedidos", head, rows, fmts)]


# ---------------------------------------------------------------- ventas por producto y mes
def sales_by_product_month(con, start, end):
    skus, _ = rt.aggregate(con, start, end, "mes")
    nm = names(con)
    months = _period_keys(start, end, "mes")
    all_skus = sorted({s for by in skus.values() for s in by})
    sheets = []
    for title, key, fmt in [("Unidades", "uds", INT), ("Ventas con IVA", "ventas", EUR), ("Beneficio", "beneficio", EUR)]:
        head = ["SKU", "Producto"] + [_label(m, "mes") for m in months] + ["Total"]
        rows = []
        for sku in all_skus:
            vals = [skus.get(m, {}).get(sku, {}).get(key, 0) for m in months]
            rows.append([sku, nm.get(sku, "")] + vals + [sum(vals)])
        totals = [sum(r[i] for r in rows) for i in range(2, len(head))]
        rows.append(["TOTAL", ""] + totals)
        sheets.append((title, head, rows, [None, None] + [fmt] * (len(months) + 1)))
    return sheets


# ---------------------------------------------------------------- historial de stock
def stock_history(con, start, end):
    nm = names(con)
    last = {}  # (día, sku) → stock de la última consulta de ese día
    for ts, sku, stock in con.execute("SELECT ts, sku, stock FROM listings WHERE ts >= ? AND ts < ? ORDER BY ts",
                                      ((start - timedelta(days=1)).isoformat(), (end + timedelta(days=1)).isoformat())):
        d = rt.local_date(ts)
        if start <= d <= end:
            last[(d, sku)] = stock
    days = sorted({d for d, _ in last})
    all_skus = sorted({s for _, s in last})
    head = ["SKU", "Producto"] + days
    rows = [[sku, nm.get(sku, "")] + [last.get((d, sku)) for d in days] for sku in all_skus]
    fmts = [None, None] + [INT] * len(days)
    return [("Stock por día", head, rows, fmts)], {i + 3 for i in range(len(days))}  # columnas con fecha en la cabecera


# ---------------------------------------------------------------- COG
def cogs(con, start=None, end=None):
    head = ["SKU", "ASIN", "Producto", "Desde", "COG por unidad (sin IVA)", "Envío FBA por unidad", "COG + envío", "IVA", "Vigente"]
    fmts = [None, None, None, DAY, EUR, EUR, EUR, '0" %"', None]
    table = rt.costs_table(con)
    latest = {}
    for sku, desde, *_ in table:
        latest[sku] = max(latest.get(sku, ""), desde)
    rows = [[sku, asin, nombre, date.fromisoformat(desde), coste, envio, coste + (envio or 0), iva,
             "Sí" if latest[sku] == desde else "No"] for sku, desde, coste, envio, iva, asin, nombre in table]
    return [("COG", head, rows, fmts)]


# ---------------------------------------------------------------- reembolsos
def refunds(con, start, end):
    nm = names(con)
    lo, hi = start.isoformat(), (end + timedelta(days=2)).isoformat()
    lines = defaultdict(lambda: dict(fecha=None, uds=0, importe=0.0, comision=0.0))
    for posted, pedido, sku, qty, concepto, importe in con.execute(
            "SELECT posted, pedido, sku, cantidad, concepto, importe FROM fin_lineas "
            "WHERE tipo='devolucion' AND sku IS NOT NULL AND posted >= ? AND posted < ?", ((start - timedelta(days=1)).isoformat(), hi)):
        d = rt.local_date(posted)
        if not start <= d <= end:
            continue
        l = lines[(pedido, sku)]
        l["fecha"] = min(l["fecha"] or d, d)
        if concepto == "UNIDADES":
            l["uds"] += qty
        elif concepto in rt.INCOME:
            l["importe"] += importe
        elif concepto in rt.REFERRAL:
            l["comision"] += importe
    phys = defaultdict(list)
    for pedido, sku, estado, motivo, fecha in con.execute("SELECT pedido, sku, estado, motivo, fecha FROM devoluciones_fba"):
        phys[(pedido, sku)].append((REASONS.get(motivo, (motivo or "").replace("_", " ").capitalize()),
                                    STATES.get(estado, (estado or "").replace("_", " ").capitalize()), rt.local_date(fecha)))
    detail = []
    by_sku = defaultdict(lambda: dict(uds=0, importe=0.0, comision=0.0, pedidos=0, vendibles=0))
    for (pedido, sku), l in sorted(lines.items(), key=lambda x: x[1]["fecha"], reverse=True):
        p = phys.get((pedido, sku), [])
        detail.append([l["fecha"], pedido, sku, nm.get(sku, ""), l["uds"], -l["importe"], l["comision"],
                       "; ".join(sorted({m for m, _, _ in p})) or "Aún no ha vuelto",
                       "; ".join(sorted({e for _, e, _ in p})), p[0][2] if p else None])
        s = by_sku[sku]
        s["uds"] += l["uds"]
        s["importe"] += -l["importe"]
        s["comision"] += l["comision"]
        s["pedidos"] += 1
        s["vendibles"] += sum(1 for _, e, _ in p if e == "Vendible")
    sold = defaultdict(int)
    skus, _ = rt.aggregate(con, start, end, "mes")
    for by in skus.values():
        for sku, r in by.items():
            sold[sku] += r["uds"]
    summary = [[sku, nm.get(sku, ""), s["pedidos"], s["uds"], sold[sku], 100 * s["uds"] / sold[sku] if sold[sku] else None,
                s["importe"], s["comision"], s["vendibles"]]
               for sku, s in sorted(by_sku.items(), key=lambda x: -x[1]["uds"])]
    return [
        ("Por producto", ["SKU", "Producto", "Pedidos reembolsados", "Uds reembolsadas", "Uds vendidas en el periodo",
                          "% reembolsado", "Reembolsado con IVA", "Comisión devuelta por Amazon", "Volvieron vendibles"],
         summary, [None, None, INT, INT, INT, PCT, EUR, EUR, INT]),
        ("Detalle", ["Fecha del reembolso", "Pedido", "SKU", "Producto", "Uds", "Reembolsado con IVA",
                     "Comisión devuelta", "Motivo", "Cómo llegó al almacén", "Fecha de la devolución"],
         detail, [DAY, None, None, None, INT, EUR, EUR, None, None, DAY]),
    ]


# ---------------------------------------------------------------- catálogo de informes
REPORTS = {
    "profit-dia": ("Profit por día", "Ventas, reembolsos, comisiones, FBA, COG, gastos de la cuenta y beneficio neto, día a día.",
                   profit_by_day, True),
    "profit-mes": ("Profit por mes", "Los mismos datos que Profit, mes a mes.", profit_by_month, True),
    "profit-producto": ("Profit por producto y día", "Cada producto, cada día: unidades, ventas, comisiones, FBA, COG y beneficio.",
                        profit_by_product, True),
    "pedidos": ("Pedidos", "Tus pedidos con fecha, estado, país, productos, unidades, importe y reembolsos.", orders, True),
    "ventas-producto-mes": ("Ventas por producto y mes", "Unidades, ventas y beneficio de cada producto por mes, para ver tendencias.",
                            sales_by_product_month, True),
    "stock": ("Historial de stock", "Tu stock de cada producto al final de cada día (desde el 22/10/2026).", stock_history, True),
    "cog": ("COG", "El coste de cada producto, con su envío a FBA, IVA y desde qué fecha se aplica.", cogs, False),
    "reembolsos": ("Reembolsos", "Pedidos reembolsados agrupados por producto, con motivo y estado de la devolución.", refunds, True),
}


def build(kind, start, end):
    """(nombre de archivo, bytes del Excel) de un informe."""
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    title, _, fn, uses_dates = REPORTS[kind]
    con = sn.db()
    rt.ensure_tables(con)
    result = fn(con, start, end)
    date_headers = set()
    if isinstance(result, tuple):
        result, date_headers = result
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, head, rows, fmts in result:
        ws = wb.create_sheet(name[:31])
        ws.append(head)
        for c in ws[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="111111")
            c.alignment = Alignment(wrap_text=True, vertical="center")
            if c.column in date_headers:
                c.number_format = "DD/MM"
        for r in rows:
            ws.append([round(v, 2) if isinstance(v, float) else v for v in r])
        if rows and rows[-1] and rows[-1][0] == "TOTAL":
            for c in ws[ws.max_row]:
                c.font = Font(bold=True)
        for i, fmt in enumerate(fmts, start=1):
            letter = get_column_letter(i)
            width = max([len(str(head[i - 1]))] + [len(str(r[i - 1])) for r in rows[:300] if i - 1 < len(r) and r[i - 1] is not None])
            ws.column_dimensions[letter].width = min(max(width + 2, 10 if fmt else 8), 48)
            if fmt:
                for (cell,) in ws.iter_rows(min_row=2, min_col=i, max_col=i):
                    cell.number_format = fmt
        ws.freeze_panes = "B2" if fmts and fmts[0] in (DAY, None) else "A2"
        if rows:
            ws.auto_filter.ref = ws.dimensions
    info = wb.create_sheet("Info")
    info.append(["Seller Notes", title])
    info.append(["Periodo", f"{start:%d/%m/%Y} – {end:%d/%m/%Y}" if uses_dates else "Todo"])
    info.append(["Generado", datetime.now(sn.TZ).strftime("%d/%m/%Y %H:%M")])
    info.append(["Cálculo", "Las ventas cuentan el día del pedido; reembolsos y gastos de la cuenta, el día en que Amazon los "
                            "registra. Los pedidos aún sin liquidar usan comisiones y FBA estimadas. Importes en euros."])
    info.column_dimensions["A"].width = 14
    info.column_dimensions["B"].width = 110
    buf = io.BytesIO()
    wb.save(buf)
    period = f"_{start:%Y-%m-%d}_{end:%Y-%m-%d}" if uses_dates else f"_{date.today():%Y-%m-%d}"
    return f"{kind}{period}.xlsx", buf.getvalue()
