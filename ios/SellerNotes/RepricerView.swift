import SwiftUI

// MARK: datos (GET /api/repricer)

struct RepricerData: Decodable {
    let items: [RepricerItem]
    let historial: [RepricerMovimiento]
    let activos: Int
    let sinStock: Int
    let margenDefecto: Double
    let estrategias: [Estrategia]
}

struct Estrategia: Decodable, Hashable {
    let key: String
    let label: String
}

struct RepricerItem: Decodable, Identifiable, Hashable {
    var id: String { sku }
    let sku: String
    let asin: String?
    let nombre: String?
    let precio: Double?
    let stock: Int?
    let conf: Ajustes
    let d: Decision?
    let vigilado: Bool
    let bbNombre: String?
    let bbSoyYo: Bool

    struct Ajustes: Decodable, Hashable {
        let estrategia: String?
        let activo: Int?
        let minimo: Double?
        let maximo: Double?
        let margen: Double?
        var encendido: Bool { (activo ?? 0) == 1 }
    }

    struct Decision: Decodable, Hashable {
        let actual: Double?
        let minimo: Double?
        let minimoAuto: Double?
        let maximo: Double?
        let objetivo: Double?
        let motivo: String
        let consulta: String?
        let bb: Ganadora?
    }

    struct Ganadora: Decodable, Hashable {
        let sellerId: String?
        let landed: Double?
    }

    /// Nombre corto de quien tiene la Buy Box («Tú», o la tienda sin el [ID]).
    var ganadora: String {
        if bbSoyYo { return "Tú" }
        guard let n = bbNombre else { return "" }
        return String(n.components(separatedBy: " [").first ?? n)
    }
}

struct RepricerMovimiento: Decodable, Identifiable {
    var id: String { ts + sku }
    let ts: String
    let sku: String
    let antes: Double?
    let nuevo: Double?
    let bbPrecio: Double?
    let motivo: String?
    let aplicado: Int?
    let error: String?
}

// MARK: lista

/// Repricer: qué precio pondría cada producto según la Buy Box, sus ajustes y el historial de cambios.
struct RepricerView: View {
    @EnvironmentObject var api: API
    @State private var recarga = 0  // al guardar un producto, la lista se vuelve a pedir

    var body: some View {
        Cargando(id: recarga) { () async throws -> RepricerData in
            try await api.get("/api/repricer")
        } content: { d in
            List {
                Section {
                    HStack(spacing: 8) {
                        chip(Fmt.num(Double(d.activos)), d.activos == 1 ? "activado" : "activados", d.activos > 0 ? .beneficio : .primary)
                        chip(Fmt.num(Double(d.items.count)), "con stock", .primary)
                        if d.sinStock > 0 { chip(Fmt.num(Double(d.sinStock)), "sin stock (ocultos)", .secondary) }
                    }
                    .listRowInsets(EdgeInsets())
                    .listRowBackground(Color.clear)
                } footer: {
                    Text("Revisa la Buy Box cada 5 minutos. Los productos sin activar solo muestran qué harían.")
                }
                Section("Productos") {
                    ForEach(d.items) { i in
                        NavigationLink(value: i) { fila(i) }
                    }
                }
                Section {
                    if d.historial.isEmpty {
                        Text("Aún no hay movimientos.").font(.footnote).foregroundStyle(.secondary)
                    }
                    ForEach(d.historial) { m in movimiento(m) }
                } header: {
                    Text("Historial")
                } footer: {
                    Text("Cambios hechos en Amazon y lo que habría hecho en simulación.")
                }
            }
            .listaRecta()
            .navigationDestination(for: RepricerItem.self) { i in
                RepricerDetalle(item: i, estrategias: d.estrategias, margenDefecto: d.margenDefecto) { recarga += 1 }
            }
        }
        .navigationTitle("Repricer")
        .toolbar { ToolbarItem(placement: .primaryAction) { MenuCuenta() } }
    }

    func chip(_ valor: String, _ texto: String, _ color: Color) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(valor).font(.anton(24).monospacedDigit()).foregroundStyle(color)
            Text(texto).font(.caption).foregroundStyle(.secondary)
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
        .background(.background, in: Rectangle())
    }

    func fila(_ i: RepricerItem) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Text(i.sku).font(.subheadline.weight(.semibold).monospaced()).lineLimit(1)
                Spacer()
                if !i.vigilado {
                    Pildora(texto: "Sin vigilar", tono: "")
                } else if i.conf.encendido {
                    Pildora(texto: "Activado", tono: "ok")
                } else {
                    Pildora(texto: "Sin activar", tono: "")
                }
            }
            Text(i.nombre ?? "").font(.caption).foregroundStyle(.secondary).lineLimit(1)
            if let d = i.d {
                HStack(spacing: 14) {
                    dato("Tú", Fmt.eur(d.actual))
                    dato("Buy Box", d.bb.map { Fmt.eur($0.landed) } ?? "Nadie", color: i.bbSoyYo ? .beneficio : .primary)
                    Spacer()
                    if let o = d.objetivo {
                        VStack(alignment: .trailing, spacing: 1) {
                            Text(i.conf.encendido ? "SIGUIENTE" : "PONDRÍA").font(.anton(11)).tracking(0.6).foregroundStyle(.secondary)
                            Text(Fmt.eur(o)).font(.subheadline.weight(.bold).monospacedDigit()).foregroundStyle(Color.azulPuente)
                        }
                    } else {
                        Text("Sin cambios").font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
        }
        .padding(.vertical, 2)
    }

    func dato(_ titulo: String, _ valor: String, color: Color = .primary) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(titulo.uppercased()).font(.anton(11)).tracking(0.6).foregroundStyle(.secondary)
            Text(valor).font(.subheadline.monospacedDigit()).foregroundStyle(color)
        }
    }

    func movimiento(_ m: RepricerMovimiento) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(m.sku).font(.caption.weight(.semibold).monospaced())
                Spacer()
                switch m.aplicado {
                case 1: Pildora(texto: "Cambiado", tono: "ok")
                case -1: Pildora(texto: "Error", tono: "bad")
                default: Pildora(texto: "Simulado", tono: "")
                }
            }
            HStack(spacing: 6) {
                Text(Fmt.eur(m.antes)).foregroundStyle(.secondary)
                Image(systemName: "arrow.right").font(.caption2).foregroundStyle(.secondary)
                Text(Fmt.eur(m.nuevo)).fontWeight(.bold)
                Spacer()
                Text(Fmt.diaHora(m.ts)).font(.caption).foregroundStyle(.secondary)
            }
            .font(.subheadline.monospacedDigit())
            if let mot = m.motivo, !mot.isEmpty { Text(mot).font(.caption).foregroundStyle(.secondary) }
            if let e = m.error { Text(e).font(.caption).foregroundStyle(Color.perdida) }
        }
        .padding(.vertical, 2)
    }
}

// MARK: ajustes de un producto

struct RepricerDetalle: View {
    @EnvironmentObject var api: API
    @Environment(\.dismiss) private var cerrar
    let item: RepricerItem
    let estrategias: [Estrategia]
    let margenDefecto: Double
    let guardado: () -> Void

    @State private var estrategia = "igualar"
    @State private var minimo = ""
    @State private var maximo = ""
    @State private var margen = ""
    @State private var activo = false
    @State private var confirmar = false
    @State private var guardando = false
    @State private var error: String?

    var body: some View {
        Form {
            Section {
                Text(item.nombre ?? item.sku).font(.headline)
                LabeledContent("SKU", value: item.sku)
                if let a = item.asin { LabeledContent("ASIN", value: a) }
                LabeledContent("Stock", value: item.stock.map { "\($0) uds" } ?? "—")
            }
            if !item.vigilado {
                Section {
                    Text("El monitor no vigila la Buy Box de este ASIN. Añádelo en Análisis Productos del panel web para poder usar el repricer.")
                        .font(.footnote)
                }
            } else if let d = item.d {
                Section {
                    LabeledContent("Tu precio", value: Fmt.eur(d.actual))
                    LabeledContent("Buy Box") {
                        VStack(alignment: .trailing) {
                            Text(d.bb.map { Fmt.eur($0.landed) } ?? "Nadie")
                            if !item.ganadora.isEmpty { Text(item.ganadora).font(.caption).foregroundStyle(.secondary) }
                        }
                    }
                    LabeledContent("Mínimo") {
                        VStack(alignment: .trailing) {
                            Text(Fmt.eur(d.minimo))
                            Text(item.conf.minimo != nil ? "a mano" : d.minimo != nil ? "automático" : "falta el coste")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    LabeledContent(item.conf.encendido ? "Siguiente precio" : "Pondría ahora") {
                        Text(d.objetivo.map { Fmt.eur($0) } ?? "Sin cambios")
                            .fontWeight(.bold).foregroundStyle(d.objetivo == nil ? Color.secondary : Color.azulPuente)
                    }
                    Text(d.motivo).font(.footnote).foregroundStyle(.secondary)
                } header: {
                    Text("Ahora")
                } footer: {
                    if let c = d.consulta { Text("Buy Box consultada \(Fmt.hace(c))") }
                }

                Section {
                    Segmentos(opciones: estrategias.map { ($0.key, $0.key == "igualar" ? "Igualar" : "Competir −0,02 €") },
                              seleccion: $estrategia)
                        .listRowInsets(EdgeInsets(top: 8, leading: 16, bottom: 8, trailing: 16))
                    campo("Mínimo (€)", $minimo, ayuda: d.minimoAuto.map { Fmt.num($0, decimales: 2) } ?? "obligatorio")
                    campo("Máximo (€)", $maximo, ayuda: "sin tope")
                    campo("Margen mínimo (%)", $margen, ayuda: Fmt.num(margenDefecto))
                } header: {
                    Text("Estrategia")
                } footer: {
                    Text("Igualar pone el mismo precio que la Buy Box. Competir se pone 0,02 € por debajo. Si ya tienes la Buy Box, tu precio se mantiene. Nunca baja de tu mínimo: si lo dejas vacío, se calcula con el coste, las comisiones, la tarifa FBA y el margen.")
                }

                Section {
                    Toggle("Cambiar el precio en Amazon", isOn: Binding(
                        get: { activo },
                        set: { nuevo in if nuevo { confirmar = true } else { activo = false } }))
                } footer: {
                    Text("Si está apagado, el repricer solo anota qué haría (simulación).")
                }

                Section {
                    Button {
                        Task { await guardar() }
                    } label: {
                        HStack {
                            Spacer()
                            if guardando { ProgressView().tint(.white) } else { Text("GUARDAR").font(.anton(17)).tracking(1) }
                            Spacer()
                        }
                        .padding(.vertical, 12)
                        .background(Color.marino, in: Rectangle())
                        .foregroundStyle(.white)
                    }
                    .buttonStyle(.plain)
                    .disabled(guardando)
                    .listRowInsets(EdgeInsets())
                    .listRowBackground(Color.clear)
                    if let error { Text(error).font(.footnote).foregroundStyle(Color.perdida) }
                }
            }
        }
        .navigationTitle(item.sku)
        .tituloPequeno()
        .onAppear(perform: rellenar)
        .alert("¿Activar el repricer?", isPresented: $confirmar) {
            Button("Cancelar", role: .cancel) {}
            Button("Activar") { activo = true }
        } message: {
            Text("Seller Notes cambiará el precio de \(item.sku) en Amazon automáticamente, sin bajar de tu mínimo.")
        }
    }

    func campo(_ titulo: String, _ texto: Binding<String>, ayuda: String) -> some View {
        LabeledContent(titulo) {
            TextField(ayuda, text: texto)
                .multilineTextAlignment(.trailing)
                #if os(iOS)
                .keyboardType(.decimalPad)
                #endif
        }
    }

    func rellenar() {
        let c = item.conf
        estrategia = c.estrategia ?? "igualar"
        minimo = c.minimo.map { Fmt.num($0, decimales: 2) } ?? ""
        maximo = c.maximo.map { Fmt.num($0, decimales: 2) } ?? ""
        margen = c.margen.map { Fmt.num($0, decimales: $0.rounded() == $0 ? 0 : 1) } ?? ""
        activo = c.encendido
    }

    /// El servidor entiende «17,50» y «17.50». Si hay coma, los puntos son de miles (1.234,50 → 1234,50).
    func limpio(_ s: String) -> String {
        var t = s.replacingOccurrences(of: "€", with: "").trimmingCharacters(in: .whitespaces)
        if t.contains(",") { t = t.replacingOccurrences(of: ".", with: "") }
        return t
    }

    func guardar() async {
        guardando = true
        defer { guardando = false }
        var campos = ["sku": item.sku, "asin": item.asin ?? "", "estrategia": estrategia,
                      "minimo": limpio(minimo), "maximo": limpio(maximo), "margen": limpio(margen)]
        if activo { campos["activo"] = "1" }
        do {
            try await api.enviar("/api/repricer/guardar", campos)
            error = nil
            guardado()
            cerrar()
        } catch {
            self.error = error.localizedDescription
        }
    }
}
