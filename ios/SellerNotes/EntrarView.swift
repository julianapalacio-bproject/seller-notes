import SwiftUI

/// Pantalla de entrar: el mismo usuario y contraseña del panel web.
struct EntrarView: View {
    @EnvironmentObject var api: API
    @State private var usuario = ""
    @State private var contrasena = ""
    @State private var servidor = ""
    @State private var error: String?
    @State private var entrando = false
    @State private var verServidor = false
    @FocusState private var foco: Campo?
    enum Campo { case usuario, contrasena }

    var body: some View {
        ZStack {
            LinearGradient(colors: [.marinoOscuro, .marino, .azulPuente], startPoint: .top, endPoint: .bottom)
                .ignoresSafeArea()
            VStack(spacing: 28) {
                Spacer()
                VStack(spacing: 10) {
                    Image(systemName: "chart.line.uptrend.xyaxis.circle.fill")
                        .font(.system(size: 64))
                        .foregroundStyle(.white, Color.cielo)
                    Text("SELLER NOTES")
                        .font(.anton(38))
                        .tracking(1.5)
                        .foregroundStyle(.white)
                    Text("The Blue Project")
                        .font(.subheadline)
                        .foregroundStyle(.white.opacity(0.7))
                }
                VStack(spacing: 12) {
                    TextField("Usuario", text: $usuario)
                        .textContentType(.username)
                        .autocorrectionDisabled()
                        #if os(iOS)
                        .textInputAutocapitalization(.never)
                        #endif
                        .focused($foco, equals: .usuario)
                        .submitLabel(.next)
                        .onSubmit { foco = .contrasena }
                        .campoEntrar()
                    SecureField("Contraseña", text: $contrasena)
                        .textContentType(.password)
                        .focused($foco, equals: .contrasena)
                        .submitLabel(.go)
                        .onSubmit { Task { await entrar() } }
                        .campoEntrar()
                    if let error {
                        Text(error)
                            .font(.footnote.weight(.medium))
                            .foregroundStyle(Color(hex: 0xffdad6))
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    Button {
                        Task { await entrar() }
                    } label: {
                        Group {
                            if entrando { ProgressView().tint(.marino) } else { Text("ENTRAR").font(.anton(18)).tracking(1) }
                        }
                        .frame(maxWidth: .infinity, minHeight: 50)
                    }
                    .background(.white, in: Rectangle())
                    .foregroundStyle(Color.marino)
                    .disabled(usuario.isEmpty || contrasena.isEmpty || entrando)
                }
                .padding(.horizontal, 28)
                Spacer()
                Button("Servidor") { servidor = api.servidor; verServidor = true }
                    .font(.footnote)
                    .foregroundStyle(.white.opacity(0.6))
            }
            .padding(.bottom, 8)
        }
        .alert("Servidor", isPresented: $verServidor) {
            TextField("https://…", text: $servidor)
            Button("Guardar") { api.servidor = servidor.isEmpty ? API.servidorPorDefecto : servidor }
            Button("Cancelar", role: .cancel) {}
        } message: {
            Text("Dirección del panel de Seller Notes.")
        }
    }

    func entrar() async {
        guard !usuario.isEmpty, !contrasena.isEmpty else { return }
        entrando = true
        defer { entrando = false }
        do {
            try await api.entrar(usuario: usuario.trimmingCharacters(in: .whitespaces), contrasena: contrasena)
            error = nil
        } catch {
            self.error = error.localizedDescription
            contrasena = ""
        }
    }
}

private extension View {
    func campoEntrar() -> some View {
        padding(.horizontal, 16)
            .frame(height: 50)
            .background(.white.opacity(0.12), in: Rectangle())
            .overlay(Rectangle().stroke(.white.opacity(0.18)))
            .foregroundStyle(.white)
            .tint(.white)
    }
}
