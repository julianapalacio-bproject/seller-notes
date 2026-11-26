"""Alertas de Seller Notes por Telegram: te escribe cuando cambia algo en un producto que vigilas.

Avisa de:
  - Pierdes o recuperas la Buy Box (en los que vendes).
  - El precio de la Buy Box sube o baja un 5 % o más.
  - El repricer cambia tu precio o no puede cambiarlo.
  - Entra por primera vez un vendedor nuevo.
Como mucho un mensaje por producto cada hora: lo que pase entre medias se junta en el siguiente. Todo se compara con
lo último que te avisó, así que si algo cambia y vuelve a estar igual dentro de esa hora, no te llega nada.

Se ejecuta en el monitor justo después del repricer (cada 5 min). Necesita en el .env:
  TELEGRAM_TOKEN    el token que te da @BotFather al crear el bot
  TELEGRAM_CHAT_ID  tu chat con el bot (lo rellena «python alertas.py vincular»)
  PANEL_URL         opcional: dirección del panel para el enlace de cada aviso

Uso:
  python alertas.py vincular   busca tu chat (escríbele antes algo al bot) y lo guarda en el .env
  python alertas.py prueba     envía un mensaje de prueba
  python alertas.py simular    muestra qué avisaría ahora, sin enviar nada ni guardar
"""
import html
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

import seller_notes as sn

PRICE_PCT = 5.0                    # % de cambio del precio de la Buy Box que merece aviso
EVERY = timedelta(hours=1)         # como mucho un aviso por producto en este tiempo
PANEL_URL = os.getenv("PANEL_URL", "https://143-47-53-38.sslip.io").rstrip("/")


def configured():
    return bool(os.getenv("TELEGRAM_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))


def send(text):
    """Envía un mensaje (HTML de Telegram). Lanza una excepción si Telegram no lo acepta."""
    r = requests.post(f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}/sendMessage", timeout=20,
                      json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text, "parse_mode": "HTML",
                            "disable_web_page_preview": True})
    if not r.ok:
        raise RuntimeError(f"Telegram: {r.json().get('description', r.status_code)}")


def ensure_tables(con):
    con.execute("""CREATE TABLE IF NOT EXISTS alertas_estado (
        asin TEXT PRIMARY KEY, bb_mio INTEGER, bb_precio REAL, desde TEXT, enviado TEXT)""")


def eur(v):
    return "—" if v is None else f"{v:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def product_names(con):
    names = dict(con.execute("SELECT asin, titulo FROM titulos"))
    try:
        names.update(con.execute("SELECT asin, nombre FROM listings WHERE ts=(SELECT MAX(ts) FROM listings) "
                                 "AND nombre IS NOT NULL"))
    except Exception:
        pass
    return names


def pending(con, now=None):
    """Qué habría que avisar ahora: lista de (asin, texto o None, estado nuevo). Texto None = solo actualizar estado."""
    ensure_tables(con)
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    me = sn.MY_SELLER_ID
    sellers = sn.seller_names()
    who = lambda s: html.escape((sellers.get(s) or s).split(" [")[0])
    titles = product_names(con)
    state = {r[0]: r for r in con.execute("SELECT asin, bb_mio, bb_precio, desde, enviado FROM alertas_estado")}
    out = []
    for asin in sn.read_asins():
        snap = con.execute("SELECT id, ts, bb_seller, bb_price FROM snapshots WHERE asin=? ORDER BY ts DESC, id DESC LIMIT 1",
                           (asin,)).fetchone()
        if not snap:
            continue
        offers = {s: l for s, l in con.execute("SELECT seller_id, landed FROM offers WHERE snapshot_id=?", (snap[0],))}
        mine_bb = 1 if (me and snap[2] == me) else 0
        price = snap[3]
        st = state.get(asin)
        if not st:  # primera vez: se toma como punto de partida, sin avisar
            out.append((asin, None, (mine_bb, price, stamp, None)))
            continue
        _, was_bb, was_price, since, sent = st
        if sent and now - datetime.fromisoformat(sent) < EVERY:
            continue  # ya se avisó hace poco: lo que haya cambiado irá en el próximo aviso
        lines = []
        if was_bb and not mine_bb:
            winner = f"La tiene <b>{who(snap[2])}</b> a {eur(price)}." if snap[2] else "Ahora nadie la tiene."
            lines.append(f"❌ <b>Has perdido la Buy Box.</b> {winner} Tu oferta: {eur(offers.get(me))}.")
        elif mine_bb and not was_bb:
            lines.append(f"✅ <b>Has recuperado la Buy Box</b> a {eur(price)}.")
        if price and was_price and abs(price - was_price) / was_price * 100 >= PRICE_PCT:
            pct = (price - was_price) / was_price * 100
            lines.append(f"{'📈' if pct > 0 else '📉'} Buy Box: {eur(was_price)} → <b>{eur(price)}</b> "
                         f"({'+' if pct > 0 else '−'}{abs(pct):.0f} %).")
        for sid, first in con.execute("SELECT seller_id, primera FROM vendedores_vistos WHERE asin=? AND primera>? "
                                      "AND seller_id!=? ORDER BY primera", (asin, since, me or "")):
            here = f" a {eur(offers[sid])}" if sid in offers else ""
            lines.append(f"🆕 Vendedor nuevo: <b>{who(sid)}</b>{here}.")
        log = con.execute("SELECT antes, nuevo, aplicado, error FROM repricer_log WHERE asin=? AND ts>? AND aplicado!=0 "
                          "ORDER BY ts", (asin, since)).fetchall()
        done = [r for r in log if r[2] == 1]
        if done:
            n = f" ({len(done)} cambios)" if len(done) > 1 else ""
            lines.append(f"🤖 El repricer cambió tu precio: {eur(done[0][0])} → <b>{eur(done[-1][1])}</b>{n}.")
        failed = [r for r in log if r[2] == -1]
        if failed:
            lines.append(f"⚠️ El repricer no pudo poner {eur(failed[-1][1])}: {html.escape(failed[-1][3] or '')}")
        if not lines:
            if was_price is None and price is not None:
                out.append((asin, None, (was_bb, price, stamp, sent)))  # aún no había precio de referencia
            else:
                out.append((asin, None, (was_bb, was_price, stamp, sent)))  # nada que contar: avanza el punto de corte
            continue
        title = html.escape((titles.get(asin) or asin)[:70])
        text = (f"<b>{title}</b>\n" + "\n".join(lines)
                + f"\n<a href=\"{PANEL_URL}/buybox?asin={asin}\">Ver en Seller Notes</a>")
        out.append((asin, text, (mine_bb, price if price is not None else was_price, stamp, stamp)))
    return out


def save_state(con, asin, st):
    con.execute("INSERT OR REPLACE INTO alertas_estado(asin, bb_mio, bb_precio, desde, enviado) VALUES (?,?,?,?,?)",
                (asin, *st))
    con.commit()


def run(con, quiet=False):
    """Una ronda: envía los avisos pendientes. Sin Telegram configurado no hace nada."""
    if not configured():
        return
    for asin, text, st in pending(con):
        if text:
            try:
                send(text)
            except Exception as e:  # no se guarda: se reintenta en la próxima ronda
                if not quiet:
                    print(f"    (Error enviando aviso de {asin} por Telegram: {e})", flush=True)
                continue
            if not quiet:
                print(f"    Aviso por Telegram: {asin}", flush=True)
        save_state(con, asin, st)


def link_chat():
    """Lee los últimos mensajes que ha recibido el bot y guarda el chat en el .env."""
    r = requests.get(f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}/getUpdates", timeout=20).json()
    if not r.get("ok"):
        sys.exit(f"Telegram no acepta el token: {r.get('description')}")
    chats = {}
    for u in r.get("result", []):
        c = (u.get("message") or u.get("my_chat_member") or {}).get("chat") or {}
        if c.get("type") == "private":
            chats[c["id"]] = c.get("first_name") or c.get("username") or ""
    if not chats:
        sys.exit("El bot aún no ha recibido mensajes: abre el bot en Telegram, pulsa «Iniciar» y vuelve a probar.")
    if len(chats) > 1:
        sys.exit("Varias personas han escrito al bot: " + ", ".join(f"{n} ({i})" for i, n in chats.items())
                 + ". Pon el número correcto en TELEGRAM_CHAT_ID del .env.")
    chat_id, who = next(iter(chats.items()))
    env = sn.BASE / ".env"
    text = env.read_text(encoding="utf-8")
    lines = [l for l in text.splitlines() if not l.startswith("TELEGRAM_CHAT_ID=")]
    env.write_text("\n".join(lines) + f"\nTELEGRAM_CHAT_ID={chat_id}\n", encoding="utf-8")
    print(f"Vinculado con el chat de {who} ({chat_id}).")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "simular"
    if cmd == "vincular":
        link_chat()
    elif cmd == "prueba":
        send("👋 <b>Seller Notes</b>\nLas alertas de precios ya funcionan. Te escribiré aquí cuando cambie algo.")
        print("Enviado.")
    else:
        con = sn.db()
        for asin, text, st in pending(con):
            print(f"--- {asin}: " + (text or "(sin aviso)"))
