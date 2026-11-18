import Charts
import SwiftUI

/// Profit (inicio): ventas del rango (con el beneficio neto debajo), tarjetas de periodos, evolución, productos y gastos de la cuenta.
struct ProfitView: View {
    @EnvironmentObject var api: API
    @State private var rango = "este_mes"
    static let rangos: [(String, String)] = [("hoy", "Hoy"), ("ayer", "Ayer"), ("7d", "7 días"), ("30d", "30 días"),
                                             ("este_mes", "Este mes"), ("mes_pasado", "Mes pasado")]

    var body: some View {
        Cargando(id: rango) { () async throws -> ProfitData in
            let d: ProfitData = try await api.get("/api/profit", ["rango": rango])
            if rango == "este_mes" { api.compartirConWidget(d) }
            return d
        } content: { d in
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    selectorRango
                    resumen(d)
                    tarjetas(d.tarjetas)
                    if d.periods.count > 1 { grafico(d.periods) }
                    productos(d.rows)
                    if !d.cuenta.isEmpty { cuenta(d.cuenta) }
                    if !d.refunds.productos.isEmpty { reembolsos(d.refunds) }
                    if let last = d.last {
                        Text("Datos de Amazon \(Fmt.hace(last))")
                            .font(.caption).foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity)
                    }
                }
                .padding()
            }
        }
        .fondoPanel()
        .navigationTitle("Profit")
        .toolbar { ToolbarItem(placement: .primaryAction) { MenuCuenta() } }
    }

    var selectorRango: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 0) {
                ForEach(Self.rangos, id: \.0) { key, label in
                    Button { rango = key } label: {
                        Text(label.uppercased())
                            .font(.anton(14)).tracking(0.6)
                            .padding(.horizontal, 14).padding(.vertical, 8)
                            .background(rango == key ? Color.marino : Color.clear)
                            .foregroundStyle(rango == key ? Color.white : Color.primary)
                    }
                    .buttonStyle(.plain)
                }
            }
            .overlay(Rectangle().stroke(Color.marino.opacity(0.35), lineWidth: 1))
        }
    }

    func resumen(_ d: ProfitData) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Etiqueta(texto: "Ventas").foregroundStyle(.white.opacity(0.7))
            Text(Fmt.eur(d.tot.ventas))
                .font(.anton(46))
                .foregroundStyle(.white)
                .contentTransition(.numericText())
            HStack(spacing: 0) {
                cifra("Beneficio neto", Fmt.eur(d.tot.neto, decimales: 0),
                      color: d.tot.neto < 0 ? Color(hex: 0xffb4ab) : .white)
                cifra("Unidades", Fmt.num(d.tot.uds))
                cifra("Margen", Fmt.pct(d.tot.margen))
            }
        }
        .padding(20)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(LinearGradient(colors: [.marino, .azulPuente], startPoint: .topLeading, endPoint: .bottomTrailing),
                    in: Rectangle())
    }

    func cifra(_ titulo: String, _ valor: String, color: Color = .white) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(titulo).font(.caption).foregroundStyle(.white.opacity(0.65))
            Text(valor).font(.anton(20).monospacedDigit()).foregroundStyle(color)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    func tarjetas(_ ts: [Tarjeta]) -> some View {
        LazyVGrid(columns: [GridItem(.flexible(), spacing: 12), GridItem(.flexible(), spacing: 12)], spacing: 12) {
            ForEach(ts) { t in
                Tarjeta2(padding: 14) {
                    VStack(alignment: .leading, spacing: 6) {
                        HStack {
                            Text(t.label.uppercased()).font(.anton(14)).tracking(0.5).lineLimit(1)
                            Spacer(minLength: 0)
                            if t.prevision { Image(systemName: "sparkles").font(.caption).foregroundStyle(Color.azulPuente) }
                        }
                        Text(t.fechas).font(.caption2).foregroundStyle(.secondary)
                        Text(Fmt.eur(t.ventas, decimales: 0))
                            .font(.anton(24).monospacedDigit())
                        HStack(spacing: 4) {
                            Text("Neto \(Fmt.eur(t.neto, decimales: 0))")
                                .foregroundStyle(t.neto < 0 ? Color.perdida : Color.secondary)
                            Text("· \(Fmt.num(t.uds)) uds").foregroundStyle(.secondary)
                        }
                        .font(.caption).lineLimit(1)
                    }
                }
            }
        }
    }

    func grafico(_ ps: [Periodo]) -> some View {
        Tarjeta2 {
            VStack(alignment: .leading, spacing: 10) {
                Etiqueta(texto: "Ventas por periodo")
                Chart(ps) { p in
                    BarMark(x: .value("Periodo", p.label), y: .value("Ventas", p.ventas))
                        .foregroundStyle(Color.azulPuente)
                }
                .chartXAxis {
                    AxisMarks(values: .automatic(desiredCount: 6)) { _ in AxisValueLabel() }
                }
                .frame(height: 170)
            }
        }
    }

    func productos(_ rows: [SkuProfit]) -> some View {
        Tarjeta2 {
            VStack(alignment: .leading, spacing: 0) {
                Etiqueta(texto: "Por producto").padding(.bottom, 8)
                if rows.isEmpty {
                    Text("Sin ventas en este periodo.").font(.subheadline).foregroundStyle(.secondary)
                }
                ForEach(rows) { r in
                    HStack(alignment: .firstTextBaseline, spacing: 12) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(r.nombre.isEmpty ? r.sku : r.nombre).font(.subheadline).lineLimit(2)
                            HStack(spacing: 6) {
                                Text("\(Fmt.num(r.uds)) uds · ROI \(Fmt.pct(r.roi))")
                                if r.sinCoste { Pildora(texto: "sin coste", tono: "warn") }
                            }
                            .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        VStack(alignment: .trailing, spacing: 2) {
                            Text(Fmt.eur(r.ventas))
                                .font(.subheadline.weight(.semibold).monospacedDigit())
                            Text("neto \(Fmt.eur(r.beneficio))")
                                .font(.caption.monospacedDigit())
                                .foregroundStyle(r.beneficio < 0 ? Color.perdida : Color.beneficio)
                        }
                    }
                    .padding(.vertical, 9)
                    if r.id != rows.last?.id { Divider() }
                }
            }
        }
    }

    func cuenta(_ cs: [Concepto]) -> some View {
        Tarjeta2 {
            VStack(alignment: .leading, spacing: 8) {
                Etiqueta(texto: "Gastos de la cuenta")
                ForEach(cs) { c in
                    HStack {
                        Text(c.concepto).font(.subheadline)
                        Spacer()
                        Text(Fmt.eur(c.importe)).font(.subheadline.monospacedDigit())
                            .foregroundStyle(c.importe < 0 ? Color.perdida : Color.beneficio)
                    }
                }
            }
        }
    }

    func reembolsos(_ r: Reembolsos) -> some View {
        Tarjeta2 {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Etiqueta(texto: "Reembolsos")
                    Spacer()
                    Text("\(Fmt.num(r.devueltas)) uds · \(Fmt.pct(r.tasa, decimales: 1))").font(.caption).foregroundStyle(.secondary)
                }
                ForEach(r.productos) { p in
                    HStack(alignment: .firstTextBaseline) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(p.nombre.isEmpty ? p.sku : p.nombre).font(.subheadline).lineLimit(1)
                            Text("\(Fmt.num(p.devueltas)) de \(Fmt.num(p.uds)) · \(Fmt.pct(p.tasa))")
                                .font(.caption).foregroundStyle(p.tone.isEmpty ? Color.secondary : Color.tono(p.tone))
                        }
                        Spacer()
                        Text(Fmt.eur(p.efecto)).font(.subheadline.monospacedDigit()).foregroundStyle(Color.perdida)
                    }
                }
            }
        }
    }
}
