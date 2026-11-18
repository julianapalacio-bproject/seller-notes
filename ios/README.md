# Seller Notes para iPhone

App nativa (SwiftUI, iOS 17 o posterior) que muestra los mismos datos que el panel web. No guarda nada en el teléfono salvo la sesión: todo lo pide al servidor por la API `/api/...` de `api.py`, con el mismo usuario y contraseña del panel.

## Qué hay

| Archivo | Qué es |
|---|---|
| `project.yml` | Receta del proyecto. `SellerNotes.xcodeproj` se genera con `xcodegen` |
| `SellerNotes/App.swift` | Arranque, pestañas y menú de la cuenta |
| `SellerNotes/API.swift` | Conexión con el servidor e inicio de sesión |
| `SellerNotes/Models.swift` | Forma de los datos de cada pantalla |
| `SellerNotes/Theme.swift` | Colores de DESIGN.md, formatos (€, %, fechas) y piezas comunes |
| `Compartido/Compartido.swift` | Lo que comparten la app y el widget (beneficio y sesión, en el grupo `group.com.theblueproject.sellernotes`) |
| `Widget/SellerNotesWidget.swift` | Widget de la pantalla de inicio: beneficio de hoy y de este mes |
| `SellerNotes/*View.swift` | Pantallas: Entrar, Profit, Listings y Productos (Avisos y Catálogos se quedan solo en la web) |

Aspecto: esquinas rectas en toda la app (como el panel web) y letra estrecha y gruesa de Apple en títulos y cifras (parecida a Anton; Anton no va incluida). Icono: la ola de The Blue Project sobre azul marino.
Face ID: se pide al abrir la app y al volver a ella; se puede desactivar en el menú de la cuenta.

Servidor por defecto: `https://143-47-53-38.sslip.io`. Se puede cambiar en la pantalla de entrar, con el botón «Servidor».

## Abrirla en Xcode

```bash
cd ios && xcodegen && open SellerNotes.xcodeproj
```

En Xcode: elige un iPhone del simulador, o tu iPhone conectado por cable, y pulsa ▶.
En tu iPhone con un Apple ID gratis, la app deja de abrirse a los 7 días y hay que volver a instalarla.

## Pasos que faltan para el App Store

1. **Xcode** en el disco «Desarrollador». *Hecho* (Xcode 26; el disco tiene que estar conectado para compilar).
2. **Subir `api.py` al servidor Oracle**, con las dos líneas nuevas de `panel.py`, y reiniciar `seller-panel`. *Hecho* (12/11/2026).
3. **Probar en tu iPhone** con tu Apple ID gratis. *Instalada* (13/11/2026). Con la cuenta gratis Apple no permite «grupos de app», que es lo que usa el widget, así que se instala sin widget con `project-gratis.yml`:

   ```bash
   cd ios && xcodegen --spec project-gratis.yml
   xcodebuild -project SellerNotesGratis.xcodeproj -scheme SellerNotes -destination 'id=00008110-001825D10A20401E' -derivedDataPath /Volumes/Desarrollador/DerivedData -allowProvisioningUpdates build
   ```

   La primera vez, en el iPhone: Settings → General → VPN & Device Management → tu Apple ID → Trust. Deja de abrirse a los 7 días; se reinstala con los mismos comandos.
4. **Mejoras antes de enviarla**:
   - Notificaciones cuando pierdes la Buy Box o un listing se desactiva.

   Estas mejoras también ayudan con la guía 4.2 de Apple.
5. **Cuenta de desarrollador de Apple** (99 €/año), como persona o como empresa (con D-U-N-S).
6. **Usuario de demostración** para la revisión de Apple, con datos inventados, para que no vean tus ventas reales.
7. **Página de política de privacidad**, por ejemplo `/privacidad` en el propio servidor, y la «etiqueta de privacidad» en App Store Connect. La app no recoge datos para seguimiento; el archivo `PrivacyInfo.xcprivacy` ya está incluido.
8. **Subir la versión con Xcode → Archive → App Store Connect**, y probarla primero con **TestFlight**.
9. **Pedir la distribución «Unlisted»**: solo la instala quien tiene el enlace.

Opcional: un dominio propio (p. ej. `panel.theblueproject.pt`) en lugar de `sslip.io` da mejor imagen en la revisión.
