import SwiftUI
import OmniCraftPets

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    static let sharedStore = WidgetStore()

    func applicationDidFinishLaunching(_ notification: Notification) {
        setvbuf(stdout, nil, _IONBF, 0)   // logs imediatos com stdout em arquivo
        NSApp.setActivationPolicy(.accessory)
        aplicarArgumentos()
    }

    /// Debug por terminal, como o notch: `--cenario 1..10` escolhe a fixture;
    /// `--widget transcript|ferramentas|subagentes|uso|tarefas|board|todos` abre janelas.
    private func aplicarArgumentos() {
        let args = CommandLine.arguments
        let store = Self.sharedStore

        if let i = args.firstIndex(of: "--cenario"), args.indices.contains(i + 1),
           let n = Int(args[i + 1]), (1...CenarioWidgets.allCases.count).contains(n) {
            store.cenario = CenarioWidgets.allCases[n - 1]
        }
        var indiceWidget = args.firstIndex(of: "--widget")
        while let i = indiceWidget, args.indices.contains(i + 1) {
            let nome = args[i + 1]
            if nome == "todos" {
                GerenteJanelas.shared.abrirTodos(store: store)
            } else if let tipo = TipoWidget(rawValue: nome) {
                GerenteJanelas.shared.abrir(tipo, store: store)
            }
            indiceWidget = args[(i + 1)...].firstIndex(of: "--widget")
        }
        if let i = args.firstIndex(of: "--rail"), args.indices.contains(i + 1),
           let tipo = TipoWidget(rawValue: args[i + 1]) {
            GerenteJanelas.shared.abrir(tipo, store: store)
            GerenteJanelas.shared.painel(tipo)?.forcarRail()
        }
    }
}

@main
struct OmniCraftWidgetsApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        MenuBarExtra("OmniCraft Widgets", systemImage: "rectangle.3.group") {
            DebugMenuView()
                .environment(AppDelegate.sharedStore)
        }
        .menuBarExtraStyle(.window)
    }
}

/// Painel de debug: cenário, sessão selecionada e abertura dos widgets.
struct DebugMenuView: View {
    @Environment(WidgetStore.self) private var store

    var body: some View {
        @Bindable var store = store

        VStack(alignment: .leading, spacing: 10) {
            Picker("Cenário", selection: $store.cenario) {
                ForEach(CenarioWidgets.allCases) { cenario in
                    Text(cenario.label).tag(cenario)
                }
            }

            if store.snapshot.sessoes.count > 1 {
                Picker("Sessão", selection: $store.sessaoSelecionadaID) {
                    ForEach(store.snapshot.sessoes) { sessao in
                        Text(sessao.ref.titulo)
                            .lineLimit(1)
                            .tag(Optional(sessao.id))
                    }
                }
            }

            Divider()

            Text("Destacar widget")
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(.secondary)
                .textCase(.uppercase)

            ForEach(TipoWidget.allCases) { tipo in
                Button {
                    store.registrar("destacar \(tipo.rawValue)")
                    GerenteJanelas.shared.abrir(tipo, store: store)
                } label: {
                    Label(tipo.titulo, systemImage: tipo.icone)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }

            Button("Destacar todos") {
                store.registrar("destacar todos")
                GerenteJanelas.shared.abrirTodos(store: store)
            }

            Divider()

            Button("Encerrar OmniCraft Widgets") {
                NSApp.terminate(nil)
            }
        }
        .padding(12)
        .frame(width: 260)
    }
}
