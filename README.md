# Seller Notes – monitor de Buy Box (Amazon.es)

Vigila **B0G643D8ZG** y **B0CNCL35CH** cada 5 minutos con la SP-API de Amazon y registra:

- quién tiene la **Buy Box** y a qué precio,
- **vendedores que entran y salen**,
- **cuántas veces cambia el precio** (de la Buy Box y de cada vendedor),
- el **% del día** que cada vendedor tuvo la Buy Box.

## 1. Instalar (una vez)

Necesitas Python 3.9 o superior.

```bash
pip install requests
```

## 2. Probar sin claves (modo demo)

```bash
python seller_notes.py demo
```

Genera un día de datos simulados y muestra cómo será el informe.

## 3. Poner las claves

Cuando Amazon apruebe tu app con el rol **Pricing**:

1. Copia `.env.example` como `.env`.
2. Pega el Client ID, el Client Secret y el Refresh Token.
3. Pon tu `MY_SELLER_ID` para que el informe te marque como "YO".

## 4. Usarlo

| Comando | Qué hace |
|---|---|
| `python seller_notes.py once` | Una consulta ahora (para comprobar que las claves funcionan) |
| `python seller_notes.py listings` | Estado de todos tus SKU: activo/inactivo/suprimido, incidencias, precio y stock |
| `python seller_notes.py run` | Vigila sin parar cada 5 min (y tus listings cada hora, avisando si cambia su estado); a medianoche genera el informe del día |
| `python seller_notes.py report` | Informe de hoy |
| `python seller_notes.py report 2026-10-15` | Informe de un día concreto |

Los informes se guardan en la carpeta `informes/`:

- `informe_FECHA.txt`: el resumen del día.
- `eventos_FECHA.csv`: cada movimiento con su hora; se abre en Excel.
- `listings_FECHA.csv`: el último estado de cada uno de tus SKU ese día.

Para que un SKU no se analice, escríbelo (uno por línea) en `skus_ignorados.txt`.

El estado de tus listings necesita el rol **Publicación de productos** (Product Listing) en la app y `MY_SELLER_ID` en `.env`. Estados:

- **ACTIVO**: se puede comprar.
- **VISIBLE, NO COMPRABLE**: aparece en Amazon pero sin oferta comprable (normalmente sin stock o con errores).
- **INACTIVO**: no aparece.
- **SUPRIMIDO**: Amazon lo ha ocultado por alguna incidencia.

## Panel web

Doble clic en **`6 - Abrir panel.command`** (o `python panel.py`) y se abre `http://127.0.0.1:5050` en el navegador:

- **Mis listings**: estado de cada SKU, incidencias, precio y stock, con botón para actualizar desde Amazon.
- **Buy Box**: por cada ASIN vigilado, quién la tiene, precio a lo largo del día, reparto, ofertas actuales y movimientos. Se puede ver cualquier día anterior.

El panel solo lee lo que guarda el monitor: para que la Buy Box se actualice, deja también en marcha `2 - Iniciar monitor`. Solo es accesible desde tu ordenador.

## 5. Ponerle nombre a los vendedores

La API solo da el ID del vendedor. Para ver quién es, abre `amazon.es/sp?seller=ID` y apunta el nombre en `sellers.csv`:

```
seller_id,nombre
A2XXXXXXXXXXX,Farmacia Ejemplo
```

## Limitaciones

- Consulta cada 5 minutos: si alguien cambia el precio varias veces dentro de ese intervalo, solo se ve el último cambio.
- Solo vigila mientras el ordenador esté encendido y el script en marcha.
- Amazon devuelve como máximo las 20 mejores ofertas nuevas.
