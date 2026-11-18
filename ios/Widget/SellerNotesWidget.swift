import SwiftUI
import WidgetKit

/// Widget de la pantalla de inicio: beneficio neto de hoy y de este mes.
@main
struct SellerNotesWidgets: WidgetBundle {
    var body: some Widget { BeneficioWidget() }
}

struct BeneficioWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "beneficio", provider: Proveedor()) { e in
            VistaWidget(e: e)
                .containerBackground(for: .widget) {
                    LinearGradient(colors: [Color(red: 0.04, green: 0.09, blue: 0.17), Color(red: 0.06, green: 0.14, blue: 0.29)],
                                   startPoint: .top, endPoint: .bottom)
                }
        }
        .configurationDisplayName("Beneficio")
        .description("Tu beneficio neto de hoy y de este mes.")
        .supportedFamilies([.systemSmall, .systemMedium])
    }
}

struct Entrada: TimelineEntry {
    let date: Date
    let resumen: Compartido.Resumen?
}

struct Proveedor: TimelineProvider {
    func placeholder(in context: Context) -> Entrada {
        Entrada(date: .now, resumen: .init(hoy: 12.4, mes: 98.3, ventasMes: 1240, actualizado: .now))
    }

    func getSnapshot(in context: Context, completion: @escaping (Entrada) -> Void) {
        completion(context.isPreview ? placeholder(in: context) : Entrada(date: .now, resumen: Compartido.resumen))
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<Entrada>) -> Void) {
        Task {
            let r = await Compartido.pedirResumen() ?? Compartido.resumen
            let siguiente = Calendar.current.date(byAdding: .minute, value: 30, to: .now)!
            completion(Timeline(entries: [Entrada(date: .now, resumen: r)], policy: .after(siguiente)))
        }
    }
}

struct VistaWidget: View {
    let e: Entrada
    @Environment(\.widgetFamily) private var familia
    private let cielo = Color(red: 0.51, green: 0.67, blue: 0.95)
    private let rojo = Color(red: 1.0, green: 0.71, blue: 0.67)

    var body: some View {
        if let r = e.resumen {
            if familia == .systemMedium {
                HStack(alignment: .top, spacing: 16) {
                    bloque("HOY", r.hoy)
                    bloque("ESTE MES", r.mes)
                }
                .overlay(alignment: .bottomLeading) { pie(r) }
            } else {
                VStack(alignment: .leading, spacing: 6) {
                    bloque("HOY", r.hoy)
                    Spacer(minLength: 0)
                    Text("Mes \(eur(r.mes))").font(.caption.weight(.semibold)).foregroundStyle(.white.opacity(0.85))
                    pie(r)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        } else {
            VStack(alignment: .leading, spacing: 6) {
                Text("SELLER NOTES").font(.system(size: 16, weight: .bold).width(.condensed)).foregroundStyle(.white)
                Text("Abre la app para ver tu beneficio.").font(.caption).foregroundStyle(.white.opacity(0.75))
            }
        }
    }

    func bloque(_ titulo: String, _ v: Double?) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(titulo).font(.system(size: 13, weight: .bold).width(.condensed)).tracking(0.8).foregroundStyle(cielo)
            Text(eur(v))
                .font(.system(size: 30, weight: .bold).width(.condensed).monospacedDigit())
                .foregroundStyle((v ?? 0) < 0 ? rojo : .white)
                .minimumScaleFactor(0.5).lineLimit(1)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    func pie(_ r: Compartido.Resumen) -> some View {
        Text(r.actualizado, style: .time)
            .font(.caption2).foregroundStyle(.white.opacity(0.55))
    }

    func eur(_ v: Double?) -> String {
        guard let v else { return "—" }
        return v.formatted(.currency(code: "EUR").locale(Locale(identifier: "es_ES")).precision(.fractionLength(0)))
    }
}
