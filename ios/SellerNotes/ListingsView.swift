import SwiftUI

/// Listings: estado, precio y stock de cada SKU, con buscador.
struct ListingsView: View {
    @EnvironmentObject var api: API
    @State private var buscar = ""

    var body: some View {
        Cargando { () async throws -> ListingsData in
            try await api.get("/api/listings")
        } content: { d in
            List {
                Section {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 8) {
                            ForEach(d.resumen.filter { $0.valor > 0 || $0.tono == "ok" }, id: \.label) { r in
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(Fmt.num(r.valor)).font(.anton(24).monospacedDigit())
                                        .foregroundStyle(r.tono.isEmpty ? Color.primary : Color.tono(r.tono))
                                    Text(r.label).font(.caption).foregroundStyle(.secondary)
                                }
                                .padding(.horizontal, 14).padding(.vertical, 10)
                                .background(.background, in: Rectangle())
                            }
                        }
                    }
                    .listRowInsets(EdgeInsets())
                    .listRowBackground(Color.clear)
                } footer: {
                    Text("Consultado \(d.fresh.text)").foregroundStyle(Color.tono(d.fresh.tone))
                }
                Section {
                    ForEach(filtrar(d.rows)) { r in
                        NavigationLink(value: r) { fila(r) }
                    }
                }
            }
            .listaRecta()
            .searchable(text: $buscar, prompt: "Buscar SKU, ASIN o nombre")
            .navigationDestination(for: Listing.self) { r in ListingDetalle(l: r, diasAviso: d.diasAviso) }
        }
        .navigationTitle("Listings")
        .toolbar { ToolbarItem(placement: .primaryAction) { MenuCuenta() } }
    }

    func filtrar(_ rows: [Listing]) -> [Listing] {
        let q = buscar.trimmingCharacters(in: .whitespaces).lowercased()
        guard !q.isEmpty else { return rows }
        return rows.filter { ($0.sku + " " + ($0.asin ?? "") + " " + ($0.nombre ?? "")).lowercased().contains(q) }
    }

    func fila(_ r: Listing) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Text(r.nombre ?? r.sku).font(.subheadline.weight(.medium)).lineLimit(2)
                Spacer()
                Text(Fmt.eur(r.precio)).font(.subheadline.monospacedDigit())
            }
            HStack(spacing: 6) {
                Pildora(texto: r.estado.capitalized(with: Fmt.es), tono: tono(r.estado))
                Text(r.sku).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                Spacer()
                Text(stock(r)).font(.caption.monospacedDigit())
                    .foregroundStyle(r.stock == 0 ? Color.perdida : r.stockBajo ? Color.cuidado : Color.secondary)
            }
        }
        .padding(.vertical, 2)
    }

    func stock(_ r: Listing) -> String {
        guard let s = r.stock else { return "stock —" }
        if s == 0 { return "sin stock" }
        if let d = r.dias { return "\(s) uds · \(Fmt.num(d)) días" }
        return "\(s) uds"
    }
}

func tono(_ estado: String) -> String {
    switch estado {
    case "ACTIVO": return "ok"
    case "VISIBLE, NO COMPRABLE": return "warn"
    default: return "bad"
    }
}

struct ListingDetalle: View {
    let l: Listing
    let diasAviso: Int

    var body: some View {
        List {
            Section {
                Text(l.nombre ?? l.sku).font(.headline)
                LabeledContent("Estado") { Pildora(texto: l.estado.capitalized(with: Fmt.es), tono: tono(l.estado)) }
                LabeledContent("SKU", value: l.sku)
                if let a = l.asin { LabeledContent("ASIN", value: a) }
                if let c = l.canal { LabeledContent("Canal", value: c) }
            }
            Section("Precio y stock") {
                LabeledContent("Precio", value: Fmt.eur(l.precio))
                LabeledContent("Stock", value: l.stock.map { "\($0) uds" } ?? "—")
                LabeledContent("Ventas al día (30 días)", value: Fmt.num(l.ventasDia, decimales: 1))
                if let d = l.dias {
                    LabeledContent("El stock dura") {
                        Text("\(Fmt.num(d)) días").foregroundStyle(d < Double(diasAviso) ? Color.cuidado : Color.secondary)
                    }
                }
            }
            if !l.listaIncidencias.isEmpty {
                Section("Incidencias") {
                    ForEach(l.listaIncidencias, id: \.self) { Text($0).font(.footnote) }
                }
            }
            if let a = l.asin {
                Section {
                    Link(destination: URL(string: "https://www.amazon.es/dp/\(a)")!) {
                        Label("Ver en amazon.es", systemImage: "safari")
                    }
                }
            }
        }
        .listaRecta()
        .navigationTitle(l.sku)
        .tituloPequeno()
    }
}
