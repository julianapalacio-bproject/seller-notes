#!/bin/bash
cd "$(dirname "$0")"
echo "=== Seller Notes ==="
python3 seller_notes.py report
open informes 2>/dev/null
echo; read -p "Pulsa Enter para cerrar..."
