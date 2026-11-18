import Charts
import SwiftUI

/// Análisis Productos: los ASIN vigilados y quién tiene la Buy Box.
struct ProductosView: View {
    @EnvironmentObject var api: API

    var body: some View {
        Cargando { () async throws -> ProductosData in
            try await api.get("/api/productos")
        } content: { d in
            List(d.menu) { p in
                NavigationLink(value: p.asin) {
                    HStack(spacing: 12) {
                        Circle()
                            .fill(!p.hasData ? Color.secondary : p.bbIsMe ? Color.beneficio : Color.perdida)
                            .frame(width: 10, height: 10)
                        VStack(alignment: .leading, spacing: 3) {
                            Text(p.nombre.isEmpty ? p.asin : p.nombre).font(.subheadline.weight(.medium)).lineLimit(2)
                            Text(p.sku.isEmpty ? p.asin : "\(p.sku) · \(p.asin)").font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        VStack(alignment: .trailing, spacing: 3) {
                            Text(Fmt.eur(p.bbPrice)).font(.subheadline.monospacedDigit())
                            Text(!p.hasData ? "sin datos" : p.bbIsMe ? "Buy Box tuya" : "Buy Box de otro")
                                .font(.caption2).foregroundStyle(p.bbIsMe ? Color.beneficio : Color.secondary)
                        }
                    }
                }
            }
            .listStyle(.plain)
            .navigationDestination(for: String.self) { asin in ProductoDetalle(asin: asin) }
        }
        .navigationTitle("Productos")
        .toolbar { ToolbarItem(placement: .primaryAction) { MenuCuenta() } }
    }
}

struct ProductoDetalle: View {
    @EnvironmentObject var api: API
    let asin: String
    @State private var periodo = "dia"

    var body: some View {
        Cargando(id: periodo) { () async throws -> ProductosData in
            try await api.get("/api/productos", ["asin": asin, "periodo": periodo])
        } content: { d in
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    Segmentos(opciones: d.periodos.map { ($0.key, $0.label) }, seleccion: $periodo)
                    if let p = d.producto, p.snaps > 0 {
                        cabecera(p)
                        if let puntos = p.puntos, puntos.count > 1 { grafico(puntos) }
                        if let e = p.econ { economia(e) }
                        if let w = p.war, w.active { guerra(w) }
                        if let s = p.share, !s.isEmpty { reparto(s) }
                        if let o = p.offers, !o.isEmpty { ofertas(o) }
                        if let ev = p.events, !ev.isEmpty { eventos(ev) }
                    } else {
                        ContentUnavailableView("Sin consultas en este periodo", systemImage: "clock",
                                               description: Text("El monitor aún no ha guardado datos de este producto."))
                    }
                }
                .padding()
            }
            .navigationTitle(d.producto?.sku ?? asin)
        }
        .fondoPanel()
        .tituloPequeno()
    }

    func cabecera(_ p: Producto) -> some View {
        Tarjeta2 {
            VStack(alignment: .leading, spacing: 10) {
                Text(p.nombre ?? asin).font(.headline)
                HStack(alignment: .firstTextBaseline) {
                    VStack(alignment: .leading, spacing: 2) {
                        Etiqueta(texto: "Buy Box")
                        Text(p.bbSeller ?? "—").font(.anton(24))
                            .foregroundStyle(p.bbIsMe == true ? Color.beneficio : Color.primary)
                            .lineLimit(1)
                    }
                    Spacer()
                    Text(Fmt.eur(p.bbPrice)).font(.anton(30).monospacedDigit())
                }
                if let segs = p.segments {
                    HStack(spacing: 6) {
                        ForEach(segs) { s in Pildora(texto: "\(s.tipo): \(s.vendedor.components(separatedBy: " [").first ?? "")",
                                                     tono: s.yo ? "ok" : "") }
                    }
                }
                HStack {
                    dato("Ofertas", Fmt.num(p.offersN.map(Double.init)))
                    dato("Cambios BB", Fmt.num(p.bbChanges.map(Double.init)))
                    dato("Mi stock", Fmt.num(p.myStock))
                    dato("Rango", "\(Fmt.num(p.priceMin, decimales: 2))–\(Fmt.num(p.priceMax, decimales: 2))")
                }
                if let ts = p.ts { Text("Última consulta \(Fmt.hace(ts))").font(.caption).foregroundStyle(.secondary) }
            }
        }
    }

    func dato(_ t: String, _ v: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(t).font(.caption2).foregroundStyle(.secondary)
            Text(v).font(.subheadline.weight(.semibold).monospacedDigit()).lineLimit(1).minimumScaleFactor(0.7)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    func grafico(_ ps: [Producto.Punto]) -> some View {
        Tarjeta2 {
            VStack(alignment: .leading, spacing: 8) {
                Etiqueta(texto: "Precio de la Buy Box")
                Chart(ps.filter { $0.precio != nil }) { p in
                    LineMark(x: .value("Hora", Fmt.fecha(p.ts) ?? .now), y: .value("Precio", p.precio ?? 0))
                        .interpolationMethod(.stepEnd)
                        .foregroundStyle(Color.azulPuente)
                }
                .chartYScale(domain: .automatic(includesZero: false))
                .frame(height: 160)
            }
        }
    }

    func economia(_ e: Producto.Economia) -> some View {
        Tarjeta2 {
            VStack(alignment: .leading, spacing: 8) {
                Etiqueta(texto: "Lo que ganas por unidad")
                fila("A tu precio (\(Fmt.eur(e.myPrice)))", e.myProfit)
                fila("Al precio de la Buy Box", e.bbProfit)
                Divider()
                LabeledContent("Precio mínimo sin perder", value: Fmt.eur(e.breakeven))
                LabeledContent("Coste del producto", value: Fmt.eur(e.cost))
                Text("Comisiones: \(e.source)").font(.caption).foregroundStyle(.secondary)
            }
            .font(.subheadline)
        }
    }

    func fila(_ t: String, _ v: Double?) -> some View {
        HStack {
            Text(t)
            Spacer()
            Text(Fmt.eur(v)).fontWeight(.semibold).monospacedDigit()
                .foregroundStyle((v ?? 0) < 0 ? Color.perdida : Color.beneficio)
        }
    }

    func guerra(_ w: Producto.Guerra) -> some View {
        Tarjeta2 {
            VStack(alignment: .leading, spacing: 6) {
                Label("Guerra de precios", systemImage: "flame.fill").font(.subheadline.weight(.semibold))
                    .foregroundStyle(Color.cuidado)
                if w.drop >= 5 { Text("El precio de la Buy Box ha bajado un \(Fmt.pct(w.drop)) en 7 días.").font(.footnote) }
                ForEach(w.bots.filter { !$0.yo }) { b in
                    Text("\(b.vendedor.components(separatedBy: " [").first ?? b.vendedor): \(b.cambios) cambios en 24 h")
                        .font(.footnote).foregroundStyle(.secondary)
                }
            }
        }
    }

    func reparto(_ s: [Producto.Parte]) -> some View {
        Tarjeta2 {
            VStack(alignment: .leading, spacing: 8) {
                Etiqueta(texto: "Tiempo con la Buy Box")
                ForEach(s) { p in
                    VStack(alignment: .leading, spacing: 3) {
                        HStack {
                            Text(p.vendedor.components(separatedBy: " [").first ?? p.vendedor).font(.footnote)
                                .fontWeight(p.yo ? .semibold : .regular)
                            Spacer()
                            Text(Fmt.pct(p.pct)).font(.footnote.monospacedDigit())
                        }
                        GeometryReader { g in
                            Rectangle().fill(Color.secondary.opacity(0.12))
                                .overlay(alignment: .leading) {
                                    Rectangle().fill(p.yo ? Color.beneficio : Color.azulPuente)
                                        .frame(width: max(4, g.size.width * p.pct / 100))
                                }
                        }
                        .frame(height: 6)
                    }
                }
            }
        }
    }

    func ofertas(_ os: [Producto.Oferta]) -> some View {
        Tarjeta2 {
            VStack(alignment: .leading, spacing: 8) {
                Etiqueta(texto: "Ofertas ahora")
                ForEach(os) { o in
                    HStack {
                        Text(o.seller.components(separatedBy: " [").first ?? o.seller)
                            .font(.footnote.weight(o.isMe ? .semibold : .regular))
                            .foregroundStyle(o.isMe ? Color.azulPuente : Color.primary)
                            .lineLimit(1)
                        if o.isBb { Pildora(texto: "Buy Box", tono: "ok") }
                        if o.fba == 1 { Text("FBA").font(.caption2).foregroundStyle(.secondary) }
                        Spacer()
                        Text(Fmt.eur(o.landed)).font(.footnote.monospacedDigit())
                    }
                }
            }
        }
    }

    func eventos(_ es: [Producto.Evento]) -> some View {
        Tarjeta2 {
            VStack(alignment: .leading, spacing: 8) {
                Etiqueta(texto: "Movimientos")
                ForEach(es.prefix(30)) { e in
                    HStack(alignment: .firstTextBaseline) {
                        Text(Fmt.diaHora(e.ts)).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                            .frame(width: 92, alignment: .leading)
                        VStack(alignment: .leading, spacing: 1) {
                            Text("\(e.label) · \(e.seller.components(separatedBy: " [").first ?? e.seller)").font(.footnote)
                            if e.type.hasPrefix("cambio_precio"), let o = e.old?.valor, let n = e.new?.valor {
                                Text("\(o) → \(n) €").font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            }
        }
    }
}
