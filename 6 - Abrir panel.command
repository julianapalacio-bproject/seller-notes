#!/bin/bash
cd "$(dirname "$0")"
echo "=== Seller Notes · panel web ==="
python3 -m pip install --quiet --user requests flask==3.0.3
echo "El panel se abre en el navegador. Deja esta ventana abierta mientras lo uses. Para cerrar: Ctrl+C"
python3 panel.py
