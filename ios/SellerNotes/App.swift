import LocalAuthentication
import SwiftUI
#if os(iOS)
import UIKit
#endif

@main
struct SellerNotesApp: App {
    @StateObject private var api = API()

    init() {
        #if os(iOS)
        // Títulos de la barra superior con letra estrecha y gruesa (como Anton en el panel web).
        let grande = UIFont.systemFont(ofSize: 36, weight: .bold, width: .condensed)
        let pequeno = UIFont.systemFont(ofSize: 19, weight: .bold, width: .condensed)
        let nav = UINavigationBarAppearance()
        nav.configureWithDefaultBackground()
        nav.largeTitleTextAttributes = [.font: grande, .kern: 0.6]
        nav.titleTextAttributes = [.font: pequeno, .kern: 0.4]
        UINavigationBar.appearance().standardAppearance = nav
        UINavigationBar.appearance().compactAppearance = nav
        let transparente = nav.copy()
        transparente.configureWithTransparentBackground()
        transparente.largeTitleTextAttributes = nav.largeTitleTextAttributes
        transparente.titleTextAttributes = nav.titleTextAttributes
        UINavigationBar.appearance().scrollEdgeAppearance = transparente
        #endif
    }

    var body: some Scene {
        WindowGroup {
            Raiz()
                .environmentObject(api)
                .tint(.azulPuente)
                .environment(\.locale, Fmt.es)
        }
    }
}

struct Raiz: View {
    @EnvironmentObject var api: API
    @Environment(\.scenePhase) private var fase
    @AppStorage("faceID") private var pedirFaceID = true
    @State private var bloqueada = true

    var body: some View {
        ZStack {
            Group {
                if api.comprobando {
                    ProgressView()
                } else if api.dentro {
                    Pestanas()
                } else {
                    EntrarView()
                }
            }
            if api.dentro && pedirFaceID && bloqueada {
                Bloqueo { Task { await desbloquear() } }
                    .transition(.opacity)
            }
        }
        .task {
            await api.comprobarSesion()
            await desbloquear()
        }
        .onChange(of: fase) { _, nueva in
            if nueva == .background { bloqueada = true }  // al volver a la app, se pide Face ID otra vez
            if nueva == .active && bloqueada { Task { await desbloquear() } }
        }
    }

    /// Face ID (o el código del iPhone si Face ID falla). Si el teléfono no tiene ni lo uno ni lo otro, no se bloquea.
    func desbloquear() async {
        guard api.dentro, pedirFaceID, bloqueada else { bloqueada = false; return }
        let ctx = LAContext()
        ctx.localizedCancelTitle = "Cancelar"
        var error: NSError?
        guard ctx.canEvaluatePolicy(.deviceOwnerAuthentication, error: &error) else {
            withAnimation { bloqueada = false }
            return
        }
        if (try? await ctx.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: "Abrir Seller Notes")) == true {
            withAnimation { bloqueada = false }
        }
    }
}

/// Lo que se ve mientras la app espera Face ID: tapa los datos.
struct Bloqueo: View {
    let reintentar: () -> Void
    var body: some View {
        ZStack {
            LinearGradient(colors: [.marinoOscuro, .marino], startPoint: .top, endPoint: .bottom).ignoresSafeArea()
            VStack(spacing: 18) {
                Image(systemName: "faceid").font(.system(size: 54, weight: .light)).foregroundStyle(Color.cielo)
                Text("SELLER NOTES").font(.anton(30)).tracking(1.5).foregroundStyle(.white)
                Button(action: reintentar) {
                    Text("DESBLOQUEAR").font(.anton(16)).tracking(1)
                        .padding(.horizontal, 26).padding(.vertical, 12)
                        .background(.white, in: Rectangle())
                        .foregroundStyle(Color.marino)
                }
                .buttonStyle(.plain)
            }
        }
    }
}

/// Las tres secciones de la app, abajo como pestañas (Profit es la de inicio).
struct Pestanas: View {
    @State private var pestana = "profit"

    var body: some View {
        TabView(selection: $pestana) {
            NavigationStack { ProfitView() }
                .tabItem { Label("Profit", systemImage: "eurosign.circle") }.tag("profit")
            NavigationStack { ListingsView() }
                .tabItem { Label("Listings", systemImage: "list.bullet.rectangle") }.tag("listings")
            NavigationStack { ProductosView() }
                .tabItem { Label("Productos", systemImage: "chart.xyaxis.line") }.tag("productos")
            NavigationStack { RepricerView() }
                .tabItem { Label("Repricer", systemImage: "tag") }.tag("repricer")
        }
    }
}

/// Botón de la barra superior con la cuenta, Face ID y «Salir».
struct MenuCuenta: View {
    @EnvironmentObject var api: API
    @AppStorage("faceID") private var pedirFaceID = true
    var body: some View {
        Menu {
            if let u = api.usuario { Text("Has entrado como \(u)") }
            Text(api.servidor.replacingOccurrences(of: "https://", with: ""))
            Toggle(isOn: $pedirFaceID) { Label("Pedir Face ID al abrir", systemImage: "faceid") }
            Button("Salir", systemImage: "rectangle.portrait.and.arrow.right", role: .destructive) {
                Task { await api.salir() }
            }
        } label: {
            Image(systemName: "person.crop.circle")
        }
    }
}
