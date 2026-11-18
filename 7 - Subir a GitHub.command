#!/bin/bash
cd "$(dirname "$0")"
echo "Subiendo Seller Notes a GitHub..."
git push
echo
echo "Listo. Puedes cerrar esta ventana."
read -n 1
