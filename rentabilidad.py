#!/usr/bin/env python3
"""
Seller Notes — rentabilidad real por SKU (Amazon.es).

Costes de producto por SKU (con fecha «desde» para cuando cambian) + movimientos de dinero de
Amazon (Finances API, rol «Finanzas y contabilidad») → beneficio, margen y ROI por SKU y mes.

Uso:
  python rentabilidad.py costes "Profit report.xlsx"   # importa la pestaña Productos
  python rentabilidad.py costes                        # muestra los costes cargados
"""
import sys
from datetime import date

import seller_notes as sn

DEFAULT_FROM = "2026-07-01"  # primera venta


# ---------------------------------------------------------------- tablas
def ensure_tables(con):
    con.executescript("""
        CREATE TABLE IF NOT EXISTS costes (
            sku TEXT, desde TEXT, coste REAL, envio_fba REAL DEFAULT 0, iva REAL,
            asin TEXT, nombre TEXT, PRIMARY KEY (sku, desde));
    """)


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
    sys.exit(__doc__)


if __name__ == "__main__":
    main()
