import SwiftUI
import OmniCraftPets

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    static let sharedStore = HUDStore()
    private var panelController: NotchPanelController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        setvbuf(stdout, nil, _IONBF, 0)   // logs imediatos mesmo com stdout em arquivo
        // Acessório: sem ícone no Dock, sem janela principal (equivale a LSUIElement).
        NSApp.setActivationPolicy(.accessory)
        if CommandLine.arguments.contains("--diagnostico-local") {
            Task { await Self.rodarDiagnosticoLocal() }
            return
        }
        if CommandLine.arguments.contains("--diagnostico-pets") {
            Self.rodarDiagnosticoPets()
            return
        }
        applyLaunchArguments(to: Self.sharedStore)
        panelController = NotchPanelController(store: Self.sharedStore)
    }

    /// `--diagnostico-pets`: confere que os atlas carregam e fatiam, e qual
    /// animação cada estado do HUD vai tocar.
    private static func rodarDiagnosticoPets() {
        for pet in Pet.allCases {
            let animacoes = PetSpritesheet.animacoes(de: pet)
            guard !animacoes.isEmpty else {
                print("\(pet.label): sem atlas → usa o mascote desenhado em código")
                continue
            }
            let resumo = animacoes.keys.sorted()
                .map { "\($0)(\(animacoes[$0]!.quadros.count))" }
                .joined(separator: " ")
            print("\(pet.label): \(animacoes.count) animações — \(resumo)")
            let estados: [(String, EstadoMascote)] = [
                ("atenção", .atencao), ("erro", .erro), ("trabalhando", .trabalhando),
                ("concluído", .concluido), ("ocioso", .ocioso)
            ]
            for (rotulo, estado) in estados {
                let escolhida = estado.animacoes.first { animacoes[$0] != nil } ?? "—"
                let a = animacoes[escolhida]
                print("   \(rotulo.padding(toLength: 12, withPad: " ", startingAt: 0)) → \(escolhida)"
                      + (a.map { " (\($0.quadros.count) quadros, \($0.msPorQuadro) ms)" } ?? ""))
            }
        }
        NSApp.terminate(nil)
    }

    /// `--diagnostico-local`: roda a detecção UMA vez, imprime o que achou e sai.
    /// Serve para conferir no terminal o que a ilha veria, sem abrir janela.
    private static func rodarDiagnosticoLocal() async {
        let inicio = Date()
        let lido = await LocalDetector().detectar()
        let ms = Int(Date().timeIntervalSince(inicio) * 1000)

        guard let lido else {
            print("não deu para ler os processos desta máquina (ps/lsof falharam) — \(ms) ms")
            NSApp.terminate(nil)
            return
        }
        print("detecção local em \(ms) ms · \(lido.counts.pillText)")
        if lido.sessions.isEmpty {
            print("nenhuma sessão de agente ativa agora")
        }
        for s in lido.sessions {
            let extras = [s.ferramentaAtual, s.subestado, s.atualizadoHa]
                .compactMap { $0?.isEmpty == false ? $0 : nil }
                .joined(separator: " · ")
            print("• \(s.title) [\(s.state.label)] \(extras)")
            print("  \(s.metadataLine)")
        }
        NSApp.terminate(nil)
    }

    /// Debug por terminal: `--cenario 1..8` escolhe a fixture; `--colapsado` força o
    /// pill; `--expandido` força a ilha; `--live` liga o feed do servidor;
    /// `--local` liga a detecção local (sem servidor).
    private func applyLaunchArguments(to store: HUDStore) {
        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--cenario"), args.indices.contains(i + 1),
           let n = Int(args[i + 1]), (1...MockScenario.allCases.count).contains(n) {
            store.scenario = MockScenario.allCases[n - 1]
        }
        if args.contains("--live") {
            store.feedSource = .live
        }
        if args.contains("--local") {
            store.feedSource = .local
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

            Text(store.feedSource.descricao)
                .font(.system(size: 10.5))
                .foregroundStyle(.secondary)

            if store.feedSource == .live {
                TextField("Base URL", text: $store.baseURLString)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(size: 11))
                    .accessibilityLabel("Base URL do servidor OmniCraft")

                connectionStatus
            } else if store.feedSource == .local {
                localStatus
            } else {
                Picker("Cenário", selection: $store.scenario) {
                    ForEach(MockScenario.allCases) { scenario in
                        Text(scenario.label).tag(scenario)
                    }
                }
            }

            Divider()

            Picker("Pet", selection: $store.pet) {
                ForEach(Pet.allCases) { pet in
                    Text(pet.label).tag(pet)
                }
            }

            Picker("Ritmo", selection: $store.ritmoPet) {
                ForEach(RitmoPet.allCases) { ritmo in
                    Text(ritmo.label).tag(ritmo)
                }
            }

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

            Toggle("Efeitos sonoros", isOn: $store.sonsAtivados)
            Toggle("Horário silencioso", isOn: $store.horarioSilencioso)
                .disabled(!store.sonsAtivados)

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

    /// Local não tem "conexão": ou leu os processos da máquina, ou não leu.
    private var localStatus: some View {
        let ilegivel = store.snapshot.counts.isUnavailable
        return HStack(spacing: 6) {
            Image(systemName: ilegivel ? "exclamationmark.triangle.fill" : "desktopcomputer")
                .foregroundStyle(ilegivel ? .orange : .green)
                .font(.system(size: 10))
            Text(ilegivel
                 ? "não deu para ler os processos"
                 : "lendo esta máquina" + (store.lastGeneratedAt.map { " · \(Formatters.hora($0))" } ?? ""))
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
        }
        .accessibilityElement(children: .combine)
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
