import Foundation
#if canImport(WidgetKit)
import WidgetKit
#endif

/// Habla con el panel de Seller Notes (las rutas /api/... de panel.py).
/// La sesión es la misma cookie de 30 días que usa la web; iOS la guarda entre aperturas de la app.
@MainActor
final class API: ObservableObject {
    static let servidorPorDefecto = "https://143-47-53-38.sslip.io"

    @Published var dentro = false
    @Published var comprobando = true
    @Published var usuario: String?

    var servidor: String {
        get { UserDefaults.standard.string(forKey: "servidor") ?? Self.servidorPorDefecto }
        set { UserDefaults.standard.set(newValue, forKey: "servidor") }
    }

    private let session: URLSession = {
        let c = URLSessionConfiguration.default
        c.httpCookieStorage = .shared
        c.httpShouldSetCookies = true
        c.timeoutIntervalForRequest = 30
        return URLSession(configuration: c)
    }()

    private let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    enum Fallo: LocalizedError {
        case sesion, servidor(String), red(String)
        var errorDescription: String? {
            switch self {
            case .sesion: return "Tu sesión ha caducado. Vuelve a entrar."
            case .servidor(let m): return m
            case .red(let m): return "No se pudo conectar con el servidor. \(m)"
            }
        }
    }

    private func url(_ path: String, _ query: [String: String?] = [:]) -> URL {
        var c = URLComponents(string: servidor.trimmingCharacters(in: CharacterSet(charactersIn: "/ ")) + path)!
        let items = query.compactMap { k, v in v.map { URLQueryItem(name: k, value: $0) } }
        if !items.isEmpty { c.queryItems = items.sorted { $0.name < $1.name } }
        return c.url!
    }

    private func send(_ req: URLRequest) async throws -> Data {
        var req = req
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        let data: Data, resp: URLResponse
        do {
            (data, resp) = try await session.data(for: req)
        } catch {
            throw Fallo.red(error.localizedDescription)
        }
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        if code == 401 && req.url?.path != "/api/entrar" {
            dentro = false
            throw Fallo.sesion
        }
        if !(200..<300).contains(code) {
            let msg = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["error"] as? String
            throw Fallo.servidor(msg ?? "El servidor respondió con un error (\(code)).")
        }
        return data
    }

    func get<T: Decodable>(_ path: String, _ query: [String: String?] = [:]) async throws -> T {
        let data = try await send(URLRequest(url: url(path, query)))
        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            throw Fallo.servidor("Los datos del servidor no tienen el formato esperado (\(error)).")
        }
    }

    /// Envía un formulario (POST), como los botones «Guardar» del panel web.
    func enviar(_ path: String, _ campos: [String: String]) async throws {
        var req = URLRequest(url: url(path))
        req.httpMethod = "POST"
        req.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        var form = URLComponents()
        form.queryItems = campos.map { URLQueryItem(name: $0.key, value: $0.value) }
        req.httpBody = form.percentEncodedQuery?.replacingOccurrences(of: "+", with: "%2B").data(using: .utf8)
        _ = try await send(req)
    }

    // MARK: sesión

    func comprobarSesion() async {
        comprobando = true
        defer { comprobando = false }
        struct S: Decodable { let dentro: Bool; let usuario: String? }
        if let s: S = try? await get("/api/sesion") {
            dentro = s.dentro
            usuario = s.usuario
        }
    }

    func entrar(usuario: String, contrasena: String) async throws {
        var req = URLRequest(url: url("/api/entrar"))
        req.httpMethod = "POST"
        req.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        var form = URLComponents()
        form.queryItems = [URLQueryItem(name: "usuario", value: usuario), URLQueryItem(name: "contrasena", value: contrasena)]
        req.httpBody = form.percentEncodedQuery?.replacingOccurrences(of: "+", with: "%2B").data(using: .utf8)
        struct R: Decodable { let ok: Bool; let usuario: String?; let error: String? }
        let data: Data
        do {
            data = try await send(req)
        } catch Fallo.servidor(let m) {
            throw Fallo.servidor(m)
        }
        let r = try decoder.decode(R.self, from: data)
        if !r.ok { throw Fallo.servidor(r.error ?? "No se pudo entrar") }
        self.usuario = r.usuario
        dentro = true
    }

    /// Deja el beneficio de este mes y la sesión en la carpeta común para el widget, y le pide que se actualice.
    func compartirConWidget(_ d: ProfitData) {
        let base = URL(string: servidor)!
        Compartido.servidor = servidor
        Compartido.cookie = HTTPCookie.requestHeaderFields(with: HTTPCookieStorage.shared.cookies(for: base) ?? [])["Cookie"]
        Compartido.resumen = .init(hoy: d.tarjetas.first { $0.key == "hoy" }?.neto, mes: d.tot.neto,
                                   ventasMes: d.tot.ventas, actualizado: .now)
        #if canImport(WidgetKit)
        WidgetCenter.shared.reloadAllTimelines()
        #endif
    }

    func salir() async {
        var req = URLRequest(url: url("/api/salir"))
        req.httpMethod = "POST"
        _ = try? await send(req)
        HTTPCookieStorage.shared.cookies?.forEach(HTTPCookieStorage.shared.deleteCookie)
        dentro = false
        usuario = nil
        Compartido.cookie = nil
        Compartido.resumen = nil
        #if canImport(WidgetKit)
        WidgetCenter.shared.reloadAllTimelines()
        #endif
    }
}
