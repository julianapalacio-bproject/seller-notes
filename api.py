"""
Seller Notes — API JSON para la app de iPhone.

Usa exactamente los mismos cálculos que las páginas del panel: cada endpoint llama a la vista del panel
y, en lugar de dibujar la plantilla HTML, devuelve en JSON los datos que la plantilla iba a recibir.
El inicio de sesión es el mismo (usuarios.json y la cookie de sesión de 30 días).

  POST /api/entrar                  usuario, contrasena (formulario) → {ok, usuario}
  POST /api/salir
  GET  /api/sesion                  ¿hay sesión? (y si el servidor pide contraseña)
  GET  /api/profit                  ?rango=hoy|ayer|7d|30d|este_mes|mes_pasado|todo  o  ?desde=&hasta=
  GET  /api/listings
  GET  /api/productos               ?asin=&periodo=dia|7d|30d&dia=AAAA-MM-DD
  GET  /api/repricer                productos con stock, qué haría el repricer y el historial
  POST /api/repricer/guardar        sku, asin, estrategia, minimo, maximo, margen, activo (formulario)
"""
from collections import Counter
from datetime import date, datetime

from flask import Blueprint, Response, g, jsonify, request, session

bp = Blueprint("api", __name__, url_prefix="/api")
P = None  # el módulo panel, que lo pasa en init()


def init(app, panel):
    """Registra la API en el panel. Se llama una vez desde panel.py."""
    global P
    P = panel
    real_render = panel.render_template

    def render_or_data(name, **ctx):  # en las peticiones de la API, la vista devuelve sus datos en vez del HTML
        return ctx if g.get("api") else real_render(name, **ctx)

    panel.render_template = render_or_data
    panel.PUBLIC.add("api.entrar")
    panel.PUBLIC.add("api.sesion")

    @app.before_request
    def api_needs_login():  # la API responde 401 en JSON en vez de mandar a la página de entrar
        if request.path.startswith("/api/"):
            g.api = True
            if panel.USERS and request.endpoint not in panel.PUBLIC and session.get("usuario") not in panel.USERS:
                return jsonify(error="Sesión caducada: vuelve a entrar"), 401
        return None

    app.before_request_funcs.setdefault(None, []).insert(0, app.before_request_funcs[None].pop())
    app.register_blueprint(bp)


def clean(v):
    """Pasa los datos de las plantillas a JSON: fechas en ISO, sin funciones."""
    if isinstance(v, dict):
        return {str(k): clean(x) for k, x in v.items() if not callable(x)}
    if isinstance(v, (list, tuple, set)):
        return [clean(x) for x in v if not callable(x)]
    if isinstance(v, Counter):
        return clean(dict(v))
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, float) and v != v:  # NaN no existe en JSON
        return None
    return v


def view(fn, *args):
    """Llama a una vista del panel y devuelve sus datos (o None si la vista redirige a otra página)."""
    out = fn(*args)
    return None if isinstance(out, Response) else out


def pick(ctx, *keys):
    return clean({k: ctx.get(k) for k in keys})


# ---------------------------------------------------------------- sesión
@bp.post("/entrar")
def entrar():
    if not P.USERS:
        return jsonify(ok=True, usuario=None)
    out = P.login()  # mismas reglas: bloqueo tras 5 fallos, espera de 1 s en cada fallo
    if isinstance(out, Response):
        return jsonify(ok=True, usuario=session.get("usuario"))
    return jsonify(ok=False, error=out.get("error") or "No se pudo entrar"), 401


@bp.post("/salir")
def salir():
    session.clear()
    return jsonify(ok=True)


@bp.get("/sesion")
def sesion():
    return jsonify(pide_contrasena=bool(P.USERS), usuario=session.get("usuario"),
                   dentro=not P.USERS or session.get("usuario") in P.USERS)


# ---------------------------------------------------------------- pantallas
@bp.get("/profit")
def profit():
    c = view(P.rentabilidad)
    campos = ("ventas", "pedidos", "uds", "devoluciones", "ingresos", "comisiones", "fba", "coste", "publicidad",
              "otros", "neto", "iva", "amazon", "beneficio", "reembolsos", "devueltas", "estimadas")
    tiles = [dict({f: 0.0 for f in campos}, **t["t"], key=t["key"], label=t["label"], fechas=t["dates"],
                  prevision=t["forecast"]) for t in c["tiles"]]  # un periodo sin ventas no trae todos los campos
    presets = [dict(key=k, label=v[0], desde=v[1], hasta=v[2]) for k, v in c["presets"].items()]
    acc = [dict(concepto=k, importe=v) for k, v in c["acc"]]
    data = pick(c, "rows", "tot", "periods", "refunds", "start", "end", "group", "preset", "last", "sync")
    data.update(clean(dict(tarjetas=tiles, rangos=presets, cuenta=acc)))
    return jsonify(data)


@bp.get("/listings")
def listings():
    c = view(P.listings)
    tiles = [dict(label=a, valor=b, tono=t) for a, b, t in c["tiles"]]
    data = pick(c, "ts", "rows", "fresh", "ignored", "dias_aviso")
    data["resumen"] = clean(tiles)
    return jsonify(data)


@bp.get("/productos")
def productos():
    c = view(P.buybox)
    a = c.get("a")
    if a:
        a = dict(a)
        a.pop("chart", None)  # el gráfico va aparte, como lista de puntos
        if a.get("econ"):
            a["econ"] = {k: v for k, v in a["econ"].items() if not callable(v)}
        if a.get("share"):
            a["share"] = [dict(vendedor=n, pct=pct, yo=me) for n, pct, me in a["share"]]
        if a.get("segments"):
            a["segments"] = [dict(tipo=t, vendedor=n, yo=me) for t, n, me in a["segments"]]
        if a.get("war"):
            a["war"] = dict(a["war"], bots=[dict(vendedor=n, cambios=k, yo=me) for n, k, me in a["war"]["bots"]])
        a["puntos"] = chart_points(a["asin"], c)
    return jsonify(clean(dict(menu=c["menu"], asin=c["asin"], periodo=c["period"],
                              periodos=[dict(key=k, label=v[0]) for k, v in c["periods"].items()],
                              dia=c["day"], desde=c["first_day"], anterior=c["prev_day"], siguiente=c["next_day"],
                              es_hoy=c["is_today"], producto=a)))


def chart_points(asin, c):
    """Precio de la Buy Box y número de ofertas en cada consulta del periodo (para el gráfico)."""
    start, _, _, _ = P.day_range(c["first_day"])
    _, end, _, _ = P.day_range(c["day"])
    con = P.sn.db()
    rows = con.execute("SELECT ts, bb_price, total_offers FROM snapshots WHERE asin=? AND ts>=? AND ts<? ORDER BY ts",
                       (asin, start.astimezone(P.timezone.utc).isoformat(),
                        end.astimezone(P.timezone.utc).isoformat())).fetchall()
    return [dict(ts=ts, precio=price, ofertas=n) for ts, price, n in rows]


# ---------------------------------------------------------------- repricer
@bp.get("/repricer")
def repricer():
    c = view(P.repricer)
    campos = ("sku", "asin", "nombre", "precio", "stock", "conf", "d", "vigilado", "bb_nombre", "bb_soy_yo")
    items = [{k: i.get(k) for k in campos} for i in c["items"]]
    log = [dict(ts=ts, sku=sku, antes=a, nuevo=n, bb_precio=bb, motivo=m, aplicado=ap, error=e)
           for ts, sku, a, n, bb, m, ap, e in c["log"]]
    return jsonify(clean(dict(items=items, historial=log, activos=c["activos"], sin_stock=c["sin_stock"],
                              margen_defecto=c["default_margin"],
                              estrategias=[dict(key=k, label=v) for k, v in c["strategies"].items()])))


@bp.post("/repricer/guardar")
def repricer_guardar():
    if not request.form.get("sku"):
        return jsonify(ok=False, error="Falta el SKU"), 400
    P.repricer_guardar()  # mismas reglas que el panel; la respuesta HTML (redirección) no hace falta
    return jsonify(ok=True)
