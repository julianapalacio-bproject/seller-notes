#!/bin/bash
cd "$(dirname "$0")"
echo "=== Seller Notes ==="
python3 -m pip install --quiet --user requests
echo "Deja esta ventana abierta mientras quieras vigilar. Para parar: Ctrl+C"
caffeinate -i python3 seller_notes.py run
