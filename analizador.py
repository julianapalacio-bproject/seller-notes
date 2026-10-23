#!/usr/bin/env python3
"""
Seller Notes — analizador de catálogos de proveedor (Amazon.es).

A partir de un Excel/CSV con EAN y precio de coste busca cada producto en Amazon.es y calcula:
ASIN, BSR, nº de vendedores, precio de la Buy Box, comisiones de Amazon + FBA, beneficio, ROI y
margen (todo sin IVA), y si tu cuenta puede venderlo (marcas que requieren aprobación).

Uso:
  python analizador.py catalogo.xlsx    # detecta las columnas solo y guarda el Excel en informes/catalogos/
Desde el panel: menú «Catálogos».

APIs (roles Precios + Publicación de productos): Catalog Items, Product Pricing (lotes),
Product Fees (lotes) y Listings Restrictions.
"""
import csv
import io
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import requests

import seller_notes as sn

AMAZON_SELLER_ID = "A1AT7YVPFBWXBL"  # Amazon como vendedor en Amazon.es
IVA_VENTA = float(os.getenv("IVA_VENTA", "21"))  # % de IVA de venta en España por defecto
OUT_DIR = sn.BASE / "informes" / "catalogos"


# ---------------------------------------------------------------- leer el archivo del proveedor
def read_table(path):
    """Filas (listas) de la primera hoja de un .xlsx o de un .csv."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        rows = [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
        wb.close()
    elif suffix in (".csv", ".txt"):
        raw = path.read_bytes()
        for enc in ("utf-8-sig", "cp1252", "mac_roman"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        try:
            dialect = csv.Sniffer().sniff(text[:5000], delimiters=";,\t|")
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.reader(io.StringIO(text), dialect))
    elif suffix == ".xls":
        raise ValueError("Los .xls antiguos no se pueden leer: ábrelo en Excel o Numbers y guárdalo como .xlsx")
    else:
        raise ValueError(f"Formato no soportado ({suffix}): usa .xlsx o .csv")
    return [r for r in rows if any(c not in (None, "") for c in r)]


def _norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


# Palabras que identifican cada columna, por orden de preferencia
FIELDS = {
    "ean": ["ean", "ean13", "ean 13", "gtin", "barcode", "codigo de barras", "cod barras", "upc"],
    "coste": ["coste", "costo", "pvd", "precio neto", "net price", "neto", "wholesale", "precio distribuidor",
              "precio unitario", "unit price", "precio", "price", "tarifa", "cost"],
    "nombre": ["descripcion", "nombre", "producto", "product", "description", "articulo", "designacao",
               "denominacion", "item name", "titulo", "title"],
    "marca": ["marca", "brand", "fabricante", "manufacturer"],
    "referencia": ["referencia", "ref", "sku", "codigo", "code", "cod"],
    "iva": ["iva", "vat", "tax"],
}
FIELD_LABELS = {"ean": "EAN", "coste": "Precio de coste (sin IVA)", "nombre": "Nombre / descripción",
                "marca": "Marca", "referencia": "Referencia del proveedor", "iva": "IVA de venta (%)"}
EXCLUDE = {"coste": ["pvp", "rrp", "retail", "recomendado", "msrp", "con iva", "iva incl"],
           "ean": ["caja", "box", "case", "carton", "outer", "pack"]}


def detect_columns(rows):
    """Busca la fila de cabecera y qué columna es cada campo. Devuelve (fila_cabecera, {campo: columna})."""
    best = (0, {}, -1)
    for idx, row in enumerate(rows[:20]):
        headers = [_norm(c) for c in row]
        mapping, used = {}, set()
        for field, keywords in FIELDS.items():
            for kw in keywords:
                col = next((i for i, h in enumerate(headers) if i not in used and h
                            and re.search(rf"\b{kw}\b", h)
                            and not any(x in h for x in EXCLUDE.get(field, []))), None)
                if col is not None:
                    mapping[field] = col
                    used.add(col)
                    break
        score = len(mapping) + (2 if "ean" in mapping else 0)
        if score > best[2]:
            best = (idx, mapping, score)
    return best[0], best[1]


def parse_ean(v):
    if v is None:
        return ""
    if isinstance(v, float):  # Excel guarda los EAN como número
        v = int(v)
    s = str(v).strip()
    if re.fullmatch(r"\d+\.0+", s):
        s = s.split(".")[0]
    if "e+" in s.lower():  # notación científica: el EAN ya se perdió en el Excel
        return ""
    d = re.sub(r"\D", "", s)
    if len(d) == 14 and d.startswith("0"):
        d = d[1:]
    return d if len(d) in (8, 12, 13, 14) else ""


def parse_money(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d,.\-]", "", str(v))
    if "," in s and "." in s:  # el último separador es el decimal
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_vat(v):
    x = parse_money(v)
    if x is None:
        return None
    return x * 100 if 0 < x < 1 else x


def extract_items(rows, header_idx, mapping, iva_default):
    """Convierte las filas del proveedor en productos a analizar."""
    def cell(row, field):
        col = mapping.get(field)
        return row[col] if col is not None and col < len(row) else None

    items = []
    for n, row in enumerate(rows[header_idx + 1:], start=header_idx + 2):
        ean, coste, nombre = parse_ean(cell(row, "ean")), parse_money(cell(row, "coste")), cell(row, "nombre")
        if not ean and coste is None and not nombre:
            continue
        it = dict(fila=n, ean=ean, nombre=str(nombre or "").strip(), marca=str(cell(row, "marca") or "").strip(),
                  referencia=str(cell(row, "referencia") or "").strip(), coste=coste,
                  iva=parse_vat(cell(row, "iva")) or iva_default, nota="")
        if not ean:
            it["nota"] = "EAN vacío o no válido"
        elif coste is None:
            it["nota"] = "Sin precio de coste"
        items.append(it)
    return items


# ---------------------------------------------------------------- llamadas a Amazon
def call(method, path, **kw):
    for attempt in range(7):
        r = requests.request(method, f"{sn.ENDPOINT}{path}", timeout=60,
                             headers={"x-amz-access-token": sn.access_token(), "content-type": "application/json"}, **kw)
        if r.status_code == 429:  # límite de peticiones: esperar y reintentar
            time.sleep(min(2 ** attempt, 30))
            continue
        if r.status_code in (401, 403):
            raise PermissionError(f"Amazon denegó el acceso a {path.split('?')[0]} ({r.status_code}). Revisa los roles de la app.")
        r.raise_for_status()
        return r.json()
    raise RuntimeError("Amazon sigue limitando las peticiones (429). Prueba de nuevo en unos minutos.")


def chunks(seq, n):
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def catalog_by_ean(eans, progress):
    """{ean: [productos de Amazon.es con ese EAN]} (Catalog Items API, hasta 2 peticiones/s)."""
    found = {e: [] for e in eans}
    key = {e.lstrip("0"): e for e in eans}
    groups = chunks(eans, 10)
    for gi, group in enumerate(groups):
        params = {"identifiers": ",".join(group), "identifiersType": "EAN", "marketplaceIds": sn.MARKETPLACE_ID,
                  "includedData": "summaries,salesRanks,identifiers", "pageSize": 20}
        while True:
            j = call("GET", "/catalog/2022-04-01/items", params=params)
            for item in j.get("items", []):
                ids = {i.get("identifier", "").lstrip("0")
                       for g in item.get("identifiers", []) for i in g.get("identifiers", [])}
                s = next((x for x in item.get("summaries", []) if x.get("marketplaceId") == sn.MARKETPLACE_ID), {})
                ranks = next((x for x in item.get("salesRanks", []) if x.get("marketplaceId") == sn.MARKETPLACE_ID), {})
                main = (ranks.get("displayGroupRanks") or ranks.get("classificationRanks") or [{}])[0]
                info = dict(asin=item.get("asin"), titulo=s.get("itemName", ""), marca_amazon=s.get("brand", ""),
                            categoria=main.get("title", ""), bsr=main.get("rank"))
                for i in ids & key.keys():
                    found[key[i]].append(info)
            nxt = (j.get("pagination") or {}).get("nextToken")
            time.sleep(0.6)
            if not nxt:
                break
            params = {**params, "pageToken": nxt}
        progress((gi + 1) / len(groups))
    return found


def offers_batch(asins, progress):
    """{asin: payload de getItemOffers} en lotes de 20 (límite: 1 lote cada 10 s)."""
    out = {}
    groups = chunks(asins, 20)
    for gi, group in enumerate(groups):
        if gi:
            time.sleep(10)
        body = {"requests": [{"uri": f"/products/pricing/v0/items/{a}/offers", "method": "GET",
                              "MarketplaceId": sn.MARKETPLACE_ID, "ItemCondition": "New", "CustomerType": "Consumer"}
                             for a in group]}
        j = call("POST", "/batches/products/pricing/v0/itemOffers", json=body)
        for resp in j.get("responses", []):
            payload = (resp.get("body") or {}).get("payload") or {}
            asin = (resp.get("request") or {}).get("Asin") or payload.get("ASIN")
            if asin:
                out[asin] = payload if (resp.get("status") or {}).get("statusCode") == 200 else None
        progress((gi + 1) / len(groups))
    return out


def fees_batch(prices, progress):
    """{asin: (total, comisión de venta, tarifa FBA)} para vender por FBA al precio indicado."""
    out = {}
    pairs = list(prices.items())
    groups = chunks(pairs, 20)
    for gi, group in enumerate(groups):
        if gi:
            time.sleep(2)
        body = [{"FeesEstimateRequest": {"MarketplaceId": sn.MARKETPLACE_ID, "IsAmazonFulfilled": True, "Identifier": asin,
                                         "PriceToEstimateFees": {"ListingPrice": {"CurrencyCode": "EUR", "Amount": round(p, 2)}}},
                 "IdType": "ASIN", "IdValue": asin} for asin, p in group]
        for res in call("POST", "/products/fees/v0/feesEstimate", json=body) or []:
            asin = (res.get("FeesEstimateIdentifier") or {}).get("IdValue")
            fe = res.get("FeesEstimate") or {}
            if res.get("Status") != "Success" or not fe:
                continue
            det = {d.get("FeeType"): float((d.get("FinalFee") or {}).get("Amount") or 0) for d in fe.get("FeeDetailList", [])}
            out[asin] = (float(fe["TotalFeesEstimate"]["Amount"]), det.get("ReferralFee", 0.0), det.get("FBAFees", 0.0))
        progress((gi + 1) / len(groups))
    return out


def restriction(asin):
    """('OK' | 'Requiere aprobación' | 'No disponible', motivo) para tu cuenta."""
    j = call("GET", "/listings/2021-08-01/restrictions",
             params={"asin": asin, "sellerId": sn.MY_SELLER_ID, "marketplaceIds": sn.MARKETPLACE_ID,
                     "conditionType": "new_new", "reasonLocale": "es_ES"})
    reasons = [r for x in j.get("restrictions", []) for r in x.get("reasons", [])]
    if not reasons:
        return "OK", ""
    codes = {r.get("reasonCode") for r in reasons}
    label = "Requiere aprobación" if "APPROVAL_REQUIRED" in codes else "No disponible"
    return label, "; ".join(r.get("message", "") for r in reasons)


def buybox_from_offers(payload):
    """(precio Buy Box con envío, de dónde sale, nº ofertas, nº FBA, Amazon vende, yo vendo)."""
    total, offers = sn.parse_payload(payload)
    sellers = {o["seller_id"] for o in offers}
    fba = sum(o["fba"] for o in offers)
    bb = next((o for o in offers if o["is_bb"]), None)
    price, source = (bb["landed"], "Buy Box") if bb else (None, "")
    if price is None:
        for b in (payload.get("Summary") or {}).get("BuyBoxPrices") or []:
            if str(b.get("condition", "")).lower() == "new":
                price, source = float((b.get("LandedPrice") or {}).get("Amount") or 0) or None, "Buy Box"
    if price is None and offers:
        cands = [o for o in offers if o["fba"]] or offers
        price, source = min(o["landed"] for o in cands), "Oferta FBA más barata" if any(o["fba"] for o in offers) else "Oferta más barata"
    return price, source, total, fba, AMAZON_SELLER_ID in sellers, bool(sn.MY_SELLER_ID) and sn.MY_SELLER_ID in sellers


# ---------------------------------------------------------------- análisis completo
def analyze(items, envio_unidad=0.0, progress=lambda pct, msg: None):
    """Completa cada producto con los datos de Amazon.es y calcula beneficio, ROI y margen."""
    eans = sorted({it["ean"] for it in items if it["ean"]})
    progress(2, f"Buscando {len(eans)} EAN en Amazon.es…")
    cat = catalog_by_ean(eans, lambda f: progress(2 + 23 * f, f"Buscando EAN en Amazon.es… {int(f * 100)}%"))

    for it in items:
        matches = sorted(cat.get(it["ean"], []), key=lambda m: m["bsr"] or 10 ** 9)
        if it["ean"] and not matches:
            it["nota"] = it["nota"] or "No está en Amazon.es"
        if matches:
            it.update(matches[0])
            it["otros_asins"] = ", ".join(m["asin"] for m in matches[1:])
            a, b = _norm(it["marca"]).replace(" ", ""), _norm(it["marca_amazon"]).replace(" ", "")
            if a and b and a not in b and b not in a:
                it["nota"] = f"Revisar: la marca en Amazon es «{it['marca_amazon']}»"

    asins = sorted({it["asin"] for it in items if it.get("asin")})
    secs = max(len(asins) - 20, 0) // 20 * 10
    progress(25, f"Consultando ofertas de {len(asins)} productos (≈{secs // 60 + 1} min)…")
    offers = offers_batch(asins, lambda f: progress(25 + 45 * f, f"Consultando ofertas y Buy Box… {int(f * 100)}%"))

    prices = {}
    for it in items:
        payload = offers.get(it.get("asin"))
        if it.get("asin") and payload:
            (it["precio"], it["precio_fuente"], it["ofertas"], it["ofertas_fba"],
             it["amazon_vende"], it["yo_vendo"]) = buybox_from_offers(payload)
            if it["precio"]:
                prices[it["asin"]] = it["precio"]
            else:
                it["nota"] = it["nota"] or "Sin ofertas nuevas ahora mismo"
        elif it.get("asin"):
            it["nota"] = it["nota"] or "No se pudieron leer las ofertas"

    progress(70, "Calculando comisiones de Amazon y FBA…")
    fees = fees_batch(prices, lambda f: progress(70 + 15 * f, f"Calculando comisiones… {int(f * 100)}%"))

    progress(85, "Comprobando si tu cuenta puede vender cada producto…")
    restr = {}
    if sn.MY_SELLER_ID:
        for i, asin in enumerate(asins):
            try:
                restr[asin] = restriction(asin)
            except PermissionError:
                restr = {}
                break
            except Exception as e:
                restr[asin] = ("?", str(e)[:200])
            time.sleep(0.25)
            progress(85 + 14 * (i + 1) / len(asins), f"Comprobando restricciones… {i + 1}/{len(asins)}")

    for it in items:
        asin = it.get("asin")
        if asin in restr:
            it["restriccion"], it["restriccion_motivo"] = restr[asin]
        if asin in fees and it.get("precio"):
            it["comisiones"], it["comision_venta"], it["comision_fba"] = fees[asin]
            it["venta_sin_iva"] = round(it["precio"] / (1 + it["iva"] / 100), 2)
            if it["coste"] is not None:
                coste_total = it["coste"] + envio_unidad
                it["beneficio"] = round(it["venta_sin_iva"] - it["comisiones"] - coste_total, 2)
                it["roi"] = round(100 * it["beneficio"] / coste_total, 1) if coste_total else None
                it["margen"] = round(100 * it["beneficio"] / it["venta_sin_iva"], 1) if it["venta_sin_iva"] else None
            if not it["comision_fba"]:
                it["nota"] = it["nota"] or "Amazon no dio la tarifa FBA (faltan medidas): beneficio sobrestimado"
        elif asin and it.get("precio") and not it["nota"]:
            it["nota"] = "Amazon no calculó las comisiones"
    progress(100, "Terminado")
    return items


# ---------------------------------------------------------------- exportar a Excel
COLUMNS = [  # (campo, cabecera, ancho, formato)
    ("fila", "Fila", 6, None), ("referencia", "Ref. proveedor", 14, None), ("ean", "EAN", 15, "@"),
    ("nombre", "Producto (proveedor)", 40, None), ("marca", "Marca", 16, None),
    ("coste", "Coste sin IVA", 11, "#,##0.00 €"), ("asin", "ASIN", 12, None),
    ("titulo", "Título en Amazon", 45, None), ("categoria", "Categoría", 18, None), ("bsr", "BSR", 9, "#,##0"),
    ("ofertas", "Ofertas", 8, None), ("ofertas_fba", "Ofertas FBA", 9, None),
    ("amazon_vende", "Amazon vende", 9, None), ("yo_vendo", "Ya lo vendo", 9, None),
    ("precio", "Precio Buy Box", 11, "#,##0.00 €"), ("precio_fuente", "Origen del precio", 18, None),
    ("iva", "IVA %", 6, None), ("venta_sin_iva", "Venta sin IVA", 11, "#,##0.00 €"),
    ("comision_venta", "Comisión venta", 11, "#,##0.00 €"), ("comision_fba", "Tarifa FBA", 10, "#,##0.00 €"),
    ("comisiones", "Comisiones total", 11, "#,##0.00 €"), ("beneficio", "Beneficio/ud", 11, "#,##0.00 €"),
    ("roi", "ROI %", 8, "0.0"), ("margen", "Margen %", 8, "0.0"),
    ("restriccion", "¿Puedo venderlo?", 18, None), ("restriccion_motivo", "Motivo restricción", 40, None),
    ("otros_asins", "Otros ASIN con ese EAN", 20, None), ("nota", "Nota", 28, None),
]


def to_excel(items, title):
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Análisis"
    ws.append([h for _, h, _, _ in COLUMNS])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1A56DB")
        c.alignment = Alignment(wrap_text=True, vertical="center")
    for it in items:
        row = []
        for key, _, _, _ in COLUMNS:
            v = it.get(key)
            row.append("Sí" if v is True else "No" if v is False else v)
        ws.append(row)
        if it.get("asin"):
            cell = ws.cell(row=ws.max_row, column=[k for k, *_ in COLUMNS].index("asin") + 1)
            cell.hyperlink = f"https://www.amazon.es/dp/{it['asin']}"
            cell.font = Font(color="1A56DB", underline="single")
    for i, (_, _, width, fmt) in enumerate(COLUMNS, start=1):
        letter = openpyxl.utils.get_column_letter(i)
        ws.column_dimensions[letter].width = width
        if fmt:
            for (cell,) in ws.iter_rows(min_row=2, min_col=i, max_col=i):
                cell.number_format = fmt
    ws.freeze_panes = "E2"
    ws.auto_filter.ref = ws.dimensions
    info = wb.create_sheet("Info")
    info.append(["Seller Notes · análisis de catálogo", title])
    info.append(["Fecha", datetime.now(sn.TZ).strftime("%d/%m/%Y %H:%M")])
    info.append(["Cálculo", "Beneficio = precio Buy Box sin IVA − comisiones Amazon/FBA − coste (sin IVA) − envío a FBA por unidad"])
    info.append(["", "ROI = beneficio / (coste + envío). Margen = beneficio / precio sin IVA."])
    info.column_dimensions["A"].width = 34
    info.column_dimensions["B"].width = 100
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- guardar análisis (para el panel)
def ensure_tables(con):
    con.executescript("""
        CREATE TABLE IF NOT EXISTS catalogos (
            id INTEGER PRIMARY KEY, ts TEXT, archivo TEXT, proveedor TEXT, estado TEXT,
            progreso REAL, mensaje TEXT, opciones TEXT, n INTEGER);
        CREATE TABLE IF NOT EXISTS catalogo_items (catalogo_id INTEGER, fila INTEGER, data TEXT);
        CREATE INDEX IF NOT EXISTS ix_cat_items ON catalogo_items(catalogo_id);
    """)


def run_job(job_id, path, header_idx, mapping, iva, envio_unidad):
    """Ejecuta un análisis guardando el progreso en la base de datos (se llama en un hilo del panel)."""
    con = sn.db()
    ensure_tables(con)

    def progress(pct, msg):
        con.execute("UPDATE catalogos SET progreso=?, mensaje=? WHERE id=?", (round(pct, 1), msg, job_id))
        con.commit()

    try:
        items = extract_items(read_table(path), header_idx, mapping, iva)
        if not items:
            raise ValueError("No se encontraron productos en el archivo.")
        analyze(items, envio_unidad, progress)
        con.executemany("INSERT INTO catalogo_items VALUES (?,?,?)",
                        [(job_id, it["fila"], json.dumps(it, ensure_ascii=False)) for it in items])
        con.execute("UPDATE catalogos SET estado='terminado', progreso=100, mensaje='Terminado', n=? WHERE id=?",
                    (len(items), job_id))
    except Exception as e:
        con.execute("UPDATE catalogos SET estado='error', mensaje=? WHERE id=?", (str(e)[:500], job_id))
    con.commit()


# ---------------------------------------------------------------- línea de comandos
def main():
    if len(sys.argv) < 2:
        sys.exit("Uso: python analizador.py catalogo.xlsx")
    path = Path(sys.argv[1])
    rows = read_table(path)
    header_idx, mapping = detect_columns(rows)
    print("Columnas detectadas:", ", ".join(f"{FIELD_LABELS[f]} = «{rows[header_idx][c]}»" for f, c in mapping.items()))
    if "ean" not in mapping or "coste" not in mapping:
        sys.exit("No encuentro la columna de EAN o de coste. Usa el panel (menú «Catálogos») para elegirlas a mano.")
    items = extract_items(rows, header_idx, mapping, IVA_VENTA)
    analyze(items, float(os.getenv("ENVIO_FBA_UNIDAD", "0")), lambda p, m: print(f"  {p:5.1f}%  {m}", flush=True))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{path.stem}_{datetime.now(sn.TZ):%Y-%m-%d_%H%M}.xlsx"
    out.write_bytes(to_excel(items, path.name))
    ok = [it for it in items if it.get("roi") is not None]
    print(f"\n{len(items)} productos · {sum(1 for it in items if it.get('asin'))} en Amazon.es · {len(ok)} con ROI calculado")
    for it in sorted(ok, key=lambda x: -x["roi"])[:15]:
        print(f"  ROI {it['roi']:6.1f}%  {it['beneficio']:6.2f} €/ud  BSR {it.get('bsr') or '-':>7}  "
              f"{it['asin']}  {it.get('restriccion', '')}  {it['nombre'][:45]}")
    print(f"\nGuardado en {out}")


if __name__ == "__main__":
    main()
