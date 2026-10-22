#!/bin/bash
cd "$(dirname "$0")"
echo "=== Seller Notes ==="
python3 seller_notes.py nombres --abrir
echo; echo "Escribe cada nombre después de la coma en sellers.csv y guarda (Cmd + S)."
read -p "Pulsa Enter para cerrar..."
