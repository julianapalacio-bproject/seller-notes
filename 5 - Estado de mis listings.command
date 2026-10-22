#!/bin/bash
cd "$(dirname "$0")"
echo "=== Seller Notes ==="
python3 -m pip install --quiet --user requests
python3 seller_notes.py listings
echo; read -p "Pulsa Enter para cerrar..."
