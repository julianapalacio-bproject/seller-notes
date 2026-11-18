import Foundation

// Los datos que devuelve /api/... (los nombres son los del panel, en snake_case → camelCase).

/// Un texto que en el JSON puede venir como texto o como número (p. ej. el valor antiguo de un evento).
struct Texto: Decodable, Hashable {
    let valor: String
    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if let s = try? c.decode(String.self) { valor = s }
        else if let d = try? c.decode(Double.self) { valor = d.formatted() }
        else { valor = "" }
    }
}

struct Frescura: Decodable, Hashable {
    let text: String
    let tone: String
    let ts: String?
}

struct Resumen: Decodable, Hashable {
    let label: String
    let valor: Double
    let tono: String
}

// MARK: Profit

struct ProfitData: Decodable {
    let tarjetas: [Tarjeta]
    let rangos: [Rango]
    let tot: Totales
    let rows: [SkuProfit]
    let periods: [Periodo]
    let cuenta: [Concepto]
    let refunds: Reembolsos
    let start: String
    let end: String
    let preset: String?
    let last: String?
}

struct Tarjeta: Decodable, Identifiable {
    var id: String { key }
    let key, label, fechas: String
    let prevision: Bool
    let ventas, pedidos, uds, devoluciones, ingresos, comisiones, fba, coste, publicidad, otros, neto, iva, amazon: Double
    let margen, roi: Double?
}

struct Rango: Decodable, Identifiable, Hashable {
    var id: String { key }
    let key, label, desde, hasta: String
}

struct Totales: Decodable {
    let ventas, devoluciones, ingresos, uds, producto, cuenta, neto: Double
    let margen: Double?
}

struct SkuProfit: Decodable, Identifiable {
    var id: String { sku }
    let sku, nombre: String
    let ventas, uds, beneficio, ingresos, coste: Double
    let margen, roi, porUd: Double?
    let sinCoste: Bool
}

struct Periodo: Decodable, Identifiable {
    var id: String { key }
    let key, label: String
    let neto, producto, ventas, uds: Double
}

struct Concepto: Decodable, Identifiable {
    var id: String { concepto }
    let concepto: String
    let importe: Double
}

struct Reembolsos: Decodable {
    let productos: [Reembolso]
    let devueltas: Double
    let tasa: Double?
    let efecto: Double
}

struct Reembolso: Decodable, Identifiable {
    var id: String { sku }
    let sku, nombre: String
    let devueltas, uds, efecto: Double
    let tasa: Double?
    let tone: String
}

// MARK: Listings

struct ListingsData: Decodable {
    let ts: String?
    let rows: [Listing]
    let resumen: [Resumen]
    let fresh: Frescura
    let diasAviso: Int
}

struct Listing: Decodable, Identifiable, Hashable {
    var id: String { sku }
    let sku: String
    let asin: String?
    let nombre: String?
    let estado: String
    let precio: Double?
    let canal: String?
    let stock: Int?
    let errores: Int?
    let avisos: Int?
    let listaIncidencias: [String]
    let ventasDia: Double
    let dias: Double?
    let stockBajo: Bool
}

// MARK: Análisis Productos (Buy Box)

struct ProductosData: Decodable {
    let menu: [ProductoMenu]
    let asin: String?
    let periodo: String
    let periodos: [Rango2]
    let dia, desde, anterior, siguiente: String
    let esHoy: Bool
    let producto: Producto?
}

struct Rango2: Decodable, Identifiable, Hashable {
    var id: String { key }
    let key, label: String
}

struct ProductoMenu: Decodable, Identifiable, Hashable {
    var id: String { asin }
    let asin, sku, nombre: String
    let hasData: Bool
    let bbPrice: Double?
    let bbIsMe: Bool
}

struct Producto: Decodable {
    struct Parte: Decodable, Identifiable { var id: String { vendedor }; let vendedor: String; let pct: Double; let yo: Bool }
    struct Segmento: Decodable, Identifiable { var id: String { tipo }; let tipo, vendedor: String; let yo: Bool }
    struct Oferta: Decodable, Identifiable {
        var id: String { seller + String(landed ?? 0) }
        let seller: String
        let isMe, isBb, isPrime, isNormal: Bool
        let price, shipping, landed: Double?
        let fba: Int?
    }
    struct Evento: Decodable, Identifiable {
        var id: String { ts + label + seller }
        let ts, label, type, seller: String
        let old, new: Texto?
    }
    struct Economia: Decodable {
        let cost, iva, rate, fba: Double
        let source: String
        let breakeven, myPrice, myProfit, bbProfit: Double?
    }
    struct Guerra: Decodable {
        struct Bot: Decodable, Identifiable { var id: String { vendedor }; let vendedor: String; let cambios: Int; let yo: Bool }
        let active: Bool
        let drop: Double
        let bots: [Bot]
    }
    struct Punto: Decodable, Identifiable { var id: String { ts }; let ts: String; let precio: Double?; let ofertas: Int? }

    let asin: String
    let snaps: Int
    let sku, nombre: String?
    let ts: String?
    let offersN: Int?
    let bbSeller: String?
    let bbIsMe: Bool?
    let bbPrice, myStock, priceMin, priceMax: Double?
    let bbChanges: Int?
    let share: [Parte]?
    let segments: [Segmento]?
    let offers: [Oferta]?
    let events: [Evento]?
    let econ: Economia?
    let war: Guerra?
    let puntos: [Punto]?
}
