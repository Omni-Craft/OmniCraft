import SwiftUI

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    static let sharedStore = HUDStore()
    private var panelController: NotchPanelController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        setvbuf(stdout, nil, _IONBF, 0)   // logs imediatos mesmo com stdout em arquivo
        // Acessório: sem ícone no Dock, sem janela principal (equivale a LSUIElement).
        NSApp.setActivationPolicy(.accessory)
        applyLaunchArguments(to: Self.sharedStore)
        panelController = NotchPanelController(store: Self.sharedStore)
    }

    /// Debug por terminal: `--cenario 1..8` escolhe a fixture; `--colapsado` força o
    /// pill; `--expandido` força a ilha; `--live` liga o feed real.
    private func applyLaunchArguments(to store: HUDStore) {
        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--cenario"), args.indices.contains(i + 1),
           let n = Int(args[i + 1]), (1...MockScenario.allCases.count).contains(n) {
            store.scenario = MockScenario.allCases[n - 1]
        }
        if args.contains("--live") {
            store.feedSource = .live
        }
        if args.contains("--colapsado") {
            store.collapseManually()
        }
        if args.contains("--expandido") {
            store.expand()
        }
    }
}

@main
struct OmniCraftNotchApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        MenuBarExtra("OmniCraft", systemImage: "circle.dotted.circle") {
            DebugMenuView()
                .environment(AppDelegate.sharedStore)
        }
        .menuBarExtraStyle(.window)

        // Modo barra de menus: a MESMA ilha como popover — para Mac sem notch
        // e displays externos (funciona junto ou no lugar da notch).
        MenuBarExtra("Ilha OmniCraft", systemImage: "bell.badge") {
            IlhaNaBarraView()
                .environment(AppDelegate.sharedStore)
        }
        .menuBarExtraStyle(.window)
    }
}

/// A ilha dentro do popover da barra de menus: mesmo conteúdo, sem a moldura
/// fundida à notch (aqui o contêiner é o próprio popover).
struct IlhaNaBarraView: View {
    @Environment(HUDStore.self) private var store

    var body: some View {
        ExpandedIslandView()
            .environment(\.colorScheme, .dark)
            .onAppear {
                // Abrir o popover conta como "olhando": acelera o polling.
                if store.feedSource == .live { store.expand() }
            }
    }
}

/// Painel de debug: fonte de dados (mock/feed real), base URL, cenário,
/// visibilidade e expansão — controla só o visual e a origem dos dados.
struct DebugMenuView: View {
    @Environment(HUDStore.self) private var store

    var body: some View {
        @Bindable var store = store

        VStack(alignment: .leading, spacing: 10) {
            Picker("Fonte", selection: $store.feedSource) {
                ForEach(FeedSource.allCases) { source in
                    Text(source.label).tag(source)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()

            if store.feedSource == .live {
                TextField("Base URL", text: $store.baseURLString)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(size: 11))
                    .accessibilityLabel("Base URL do servidor OmniCraft")

                connectionStatus
            } else {
                Picker("Cenário", selection: $store.scenario) {
                    ForEach(MockScenario.allCases) { scenario in
                        Text(scenario.label).tag(scenario)
                    }
                }
            }

            Divider()

            Picker("Visibilidade", selection: $store.visibility) {
                ForEach(PillVisibility.allCases) { mode in
                    Text(mode.label).tag(mode)
                }
            }

            Picker("Exibição", selection: $store.modo) {
                ForEach(ModoExibicao.allCases) { modo in
                    Text(modo.label).tag(modo)
                }
            }

            Button(store.isExpanded ? "Colapsar ilha" : "Expandir ilha") {
                store.toggleExpanded()
            }

            Divider()

            Button("Encerrar OmniCraft Notch") {
                NSApp.terminate(nil)
            }
        }
        .padding(12)
        .frame(width: 280)
    }

    private var connectionStatus: some View {
        HStack(spacing: 6) {
            Image(systemName: store.isDisconnected ? "wifi.slash" : "checkmark.circle.fill")
                .foregroundStyle(store.isDisconnected ? .red : .green)
                .font(.system(size: 10))
            Text(store.isDisconnected
                 ? "sem conexão"
                 : "conectado" + (store.lastGeneratedAt.map { " · feed de \(Formatters.hora($0))" } ?? ""))
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
        }
        .accessibilityElement(children: .combine)
    }
}
