import SwiftUI

// Colores y formatos de Seller Notes (los mismos de DESIGN.md: «El puente de mando»).
extension Color {
    init(hex: UInt32) {
        self.init(red: Double((hex >> 16) & 0xff) / 255, green: Double((hex >> 8) & 0xff) / 255, blue: Double(hex & 0xff) / 255)
    }
    static let marino = Color(hex: 0x0f244a)
    static let marinoOscuro = Color(hex: 0x0a162b)
    static let azulPuente = Color(hex: 0x305f9e)
    static let cielo = Color(hex: 0x81acf1)
    static let beneficio = Color(hex: 0x059669)
    static let perdida = Color(hex: 0xba1a1a)
    static let cuidado = Color(hex: 0xd97706)

    /// ok / warn / bad → verde / ámbar / rojo
    static func tono(_ t: String) -> Color {
        switch t {
        case "ok": return .beneficio
        case "warn": return .cuidado
        case "bad": return .perdida
        default: return .secondary
        }
    }
}

// Letra de títulos y cifras grandes: estrecha y gruesa, como Anton en el panel web (sin descargar fuentes).
extension Font {
    static func anton(_ size: CGFloat) -> Font {
        .system(size: size, weight: .bold).width(.condensed)
    }
}

/// Selector de opciones con esquinas rectas (en lugar del segmentado redondeado de Apple).
struct Segmentos: View {
    let opciones: [(String, String)]
    @Binding var seleccion: String
    var body: some View {
        HStack(spacing: 0) {
            ForEach(opciones, id: \.0) { key, label in
                Button { seleccion = key } label: {
                    Text(label.uppercased())
                        .font(.anton(14)).tracking(0.6)
                        .frame(maxWidth: .infinity, minHeight: 34)
                        .background(seleccion == key ? Color.marino : Color.clear)
                        .foregroundStyle(seleccion == key ? Color.white : Color.primary)
                }
                .buttonStyle(.plain)
            }
        }
        .overlay(Rectangle().stroke(Color.marino.opacity(0.35), lineWidth: 1))
    }
}

enum Fmt {
    static let es = Locale(identifier: "es_ES")

    static func eur(_ v: Double?, decimales: Int = 2) -> String {
        guard let v else { return "—" }
        return v.formatted(.currency(code: "EUR").locale(es).precision(.fractionLength(decimales)))
    }

    static func num(_ v: Double?, decimales: Int = 0) -> String {
        guard let v else { return "—" }
        return v.formatted(.number.locale(es).precision(.fractionLength(decimales)))
    }

    static func pct(_ v: Double?, decimales: Int = 0) -> String {
        guard let v else { return "—" }
        return num(v, decimales: decimales) + " %"
    }

    private static let iso: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
    private static let isoSinFraccion = ISO8601DateFormatter()

    static func fecha(_ ts: String?) -> Date? {
        guard let ts else { return nil }
        if let d = isoSinFraccion.date(from: ts) ?? iso.date(from: ts) { return d }
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "Europe/Madrid")
        for formato in ["yyyy-MM-dd'T'HH:mm:ssXXXXX", "yyyy-MM-dd'T'HH:mm:ss.SSSSSSXXXXX", "yyyy-MM-dd"] {
            f.dateFormat = formato
            if let d = f.date(from: ts) { return d }
        }
        return nil
    }

    /// «hace 3 h», «ayer»…
    static func hace(_ ts: String?) -> String {
        guard let d = fecha(ts) else { return "" }
        let f = RelativeDateTimeFormatter()
        f.locale = es
        f.unitsStyle = .full
        return f.localizedString(for: d, relativeTo: .now)
    }

    static func diaHora(_ ts: String?) -> String {
        guard let d = fecha(ts) else { return "" }
        return d.formatted(.dateTime.day().month(.abbreviated).hour().minute().locale(es))
    }

    static func dia(_ ts: String?) -> String {
        guard let d = fecha(ts) else { return ts ?? "" }
        return d.formatted(.dateTime.day().month(.abbreviated).locale(es))
    }
}

/// Tarjeta blanca flotante sobre el fondo lavanda.
struct Tarjeta2<Content: View>: View {
    var padding: CGFloat = 16
    @ViewBuilder var content: Content
    var body: some View {
        content
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(padding)
            .background(.background, in: Rectangle())
            .shadow(color: .marino.opacity(0.06), radius: 12, y: 4)
    }
}

/// Píldora de color (estado de un listing, tono de un aviso…).
struct Pildora: View {
    let texto: String
    let tono: String
    var body: some View {
        Text(texto)
            .font(.caption2.weight(.semibold))
            .padding(.horizontal, 8).padding(.vertical, 3)
            .foregroundStyle(Color.tono(tono))
            .background(Color.tono(tono).opacity(0.12), in: Rectangle())
    }
}

/// Etiqueta pequeña en mayúsculas sobre una cifra.
struct Etiqueta: View {
    let texto: String
    var body: some View {
        Text(texto.uppercased())
            .font(.anton(13))
            .tracking(0.8)
            .foregroundStyle(.secondary)
    }
}

/// Carga los datos de una pantalla, con «desliza para actualizar» y mensaje de error.
struct Cargando<T, Content: View>: View {
    var id: AnyHashable = 0  // al cambiar (otro rango, otro producto…) se vuelve a cargar
    let cargar: () async throws -> T
    @ViewBuilder let content: (T) -> Content
    @State private var datos: T?
    @State private var error: String?

    var body: some View {
        Group {
            if let datos {
                content(datos)
            } else if let error {
                ContentUnavailableView {
                    Label("No se pudo cargar", systemImage: "wifi.exclamationmark")
                } description: {
                    Text(error)
                } actions: {
                    Button("Reintentar") { Task { await recargar() } }
                        .buttonStyle(.borderedProminent)
                }
            } else {
                ProgressView("Cargando…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .refreshable { await recargar() }
        .task(id: id) { await recargar() }
    }

    func recargar() async {
        do {
            datos = try await cargar()
            error = nil
        } catch is CancellationError {
        } catch let e as URLError where e.code == .cancelled {
        } catch {
            self.error = error.localizedDescription
        }
    }
}

extension View {
    /// Fondo lavanda de las pantallas.
    func fondoPanel() -> some View {
        #if os(iOS)
        background(Color(uiColor: .systemGroupedBackground).ignoresSafeArea())
        #else
        background(Color.gray.opacity(0.08).ignoresSafeArea())
        #endif
    }

    /// Lista sin esquinas redondeadas (secciones a todo lo ancho).
    @ViewBuilder func listaRecta() -> some View {
        #if os(iOS)
        listStyle(.grouped)
        #else
        listStyle(.plain)
        #endif
    }

    @ViewBuilder func tituloGrande() -> some View {
        #if os(iOS)
        navigationBarTitleDisplayMode(.large)
        #else
        self
        #endif
    }

    @ViewBuilder func tituloPequeno() -> some View {
        #if os(iOS)
        navigationBarTitleDisplayMode(.inline)
        #else
        self
        #endif
    }
}
