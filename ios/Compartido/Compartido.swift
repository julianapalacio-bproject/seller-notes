import Foundation

/// Lo que comparten la app y el widget: un «grupo de app» (una pequeña carpeta común) donde la app deja
/// el último beneficio y la sesión, para que el widget pueda enseñarlo y actualizarlo él solo.
enum Compartido {
    static let grupo = "group.com.theblueproject.sellernotes"
    static var ajustes: UserDefaults { UserDefaults(suiteName: grupo) ?? .standard }

    struct Resumen: Codable {
        var hoy: Double?
        var mes: Double
        var ventasMes: Double
        var actualizado: Date
    }

    static var resumen: Resumen? {
        get { ajustes.data(forKey: "resumen").flatMap { try? JSONDecoder().decode(Resumen.self, from: $0) } }
        set { ajustes.set(newValue.flatMap { try? JSONEncoder().encode($0) }, forKey: "resumen") }
    }

    /// Dirección del servidor y la cookie de sesión, para que el widget pida los datos con la misma sesión.
    static var servidor: String? {
        get { ajustes.string(forKey: "servidor") }
        set { ajustes.set(newValue, forKey: "servidor") }
    }
    static var cookie: String? {
        get { ajustes.string(forKey: "cookie") }
        set { ajustes.set(newValue, forKey: "cookie") }
    }

    /// Respuesta mínima de /api/profit que necesita el widget.
    struct Profit: Decodable {
        struct Tarjeta: Decodable { let key: String; let neto: Double }
        struct Tot: Decodable { let neto: Double; let ventas: Double }
        let tarjetas: [Tarjeta]
        let tot: Tot

        var resumen: Resumen {
            Resumen(hoy: tarjetas.first { $0.key == "hoy" }?.neto, mes: tot.neto, ventasMes: tot.ventas, actualizado: .now)
        }
    }

    /// Pide el beneficio de este mes al servidor (lo usa el widget).
    static func pedirResumen() async -> Resumen? {
        guard let servidor, let cookie,
              let url = URL(string: servidor.trimmingCharacters(in: CharacterSet(charactersIn: "/ ")) + "/api/profit?rango=este_mes")
        else { return nil }
        var req = URLRequest(url: url, timeoutInterval: 20)
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        req.setValue(cookie, forHTTPHeaderField: "Cookie")
        guard let (data, resp) = try? await URLSession.shared.data(for: req),
              (resp as? HTTPURLResponse)?.statusCode == 200,
              let p = try? JSONDecoder().decode(Profit.self, from: data)
        else { return nil }
        let r = p.resumen
        resumen = r
        return r
    }
}
