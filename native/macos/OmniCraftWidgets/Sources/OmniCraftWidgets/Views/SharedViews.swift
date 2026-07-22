import SwiftUI
import OmniCraftPets

// MARK: - Paleta (painéis pretos sólidos; mono denso)

enum Paleta {
    static let painel = Color(red: 0.075, green: 0.075, blue: 0.082)
    static let cartao = Color.white.opacity(0.05)
    static let borda = Color.white.opacity(0.09)
}

// MARK: - Cores de estado (idênticas ao OmniCraftNotch)

extension EstadoSessao {
    var cor: Color {
        switch self {
        case .emExecucao: .green
        case .aguardandoVoce: .orange
        case .ocioso: .secondary
        case .falhou: .red
        case .concluida: .green
        case .desconhecido: .secondary
        }
    }
}

extension EstadoFerramenta {
    var cor: Color {
        switch self {
        case .executando: .blue
        case .concluida: .green
        case .falhou: .red
        }
    }
}

// MARK: - Badge de estado: SEMPRE ícone + texto, nunca só cor

struct BadgeEstado: View {
    let icone: String
    let texto: String
    let cor: Color

    init(_ estado: EstadoSessao) {
        icone = estado.symbolName; texto = estado.label; cor = estado.cor
    }

    init(_ estado: EstadoFerramenta) {
        icone = estado.symbolName; texto = estado.label; cor = estado.cor
    }

    init(icone: String, texto: String, cor: Color) {
        self.icone = icone; self.texto = texto; self.cor = cor
    }

    var body: some View {
        HStack(spacing: 3) {
            Image(systemName: icone)
                .font(.system(size: 8, weight: .semibold))
            Text(texto.uppercased())
                .font(.system(size: 8.5, weight: .bold, design: .monospaced))
                .tracking(0.3)
                .fixedSize()
        }
        .foregroundStyle(cor)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(texto)
    }
}

// MARK: - Raiz de cada janela: conteúdo normal ou rail compacto

struct WidgetRootView: View {
    let tipo: TipoWidget
    let controlador: PainelWidget

    var body: some View {
        Group {
            if controlador.emRail {
                RailView(tipo: tipo, controlador: controlador)
            } else {
                WidgetChrome(tipo: tipo, controlador: controlador)
            }
        }
        .environment(\.colorScheme, .dark)   // painéis sempre escuros, como a referência
    }
}

// MARK: - Moldura comum (painel preto sólido + header com contagem)

struct WidgetChrome: View {
    @Environment(WidgetStore.self) private var store
    let tipo: TipoWidget
    let controlador: PainelWidget

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Rectangle().fill(Paleta.borda).frame(height: 1)
            conteudo
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        }
        .background(Paleta.painel, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .strokeBorder(Paleta.borda, lineWidth: 1)
        )
    }

    private var header: some View {
        HStack(spacing: 6) {
            // O pet vive no Board — é o painel global, o equivalente da ilha.
            // Nos demais widgets ele seria repetição (8 peixes na tela).
            if tipo == .board, store.estadoMascote != .oculto {
                MascoteView(estado: store.estadoMascote, pet: store.pet,
                            altura: 38, ritmo: store.ritmoPet)
                    .padding(.trailing, 2)
                    .transition(.opacity)
            } else {
                Image(systemName: tipo.icone)
                    .font(.system(size: 9, weight: .semibold))
                    .foregroundStyle(.secondary)
            }
            Text(tipo.titulo.uppercased())
                .font(.system(size: 10, weight: .bold, design: .monospaced))
                .tracking(1)
            if tipo != .board, let sessao = store.sessaoSelecionada {
                Text(sessao.ref.titulo)
                    .font(.system(size: 9.5, design: .monospaced))
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
                    .truncationMode(.tail)
            }
            Spacer(minLength: 8)
            Text(metaHeader)
                .font(.system(size: 9.5, design: .monospaced))
                .foregroundStyle(.secondary)
                .fixedSize()
            Button {
                store.registrar("fechar widget \(tipo.rawValue)")
                controlador.fechar()
            } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 8, weight: .bold))
                    .foregroundStyle(.tertiary)
                    .padding(4)
                    .contentShape(Circle())
            }
            .buttonStyle(BotaoCirculoHover())
            .focusable()
            .accessibilityLabel("Fechar o widget \(tipo.titulo)")
        }
        .padding(.horizontal, 10)
        // Header do board respira menos na vertical: quem dá a altura é o pet.
        .padding(.vertical, tipo == .board && store.estadoMascote != .oculto ? 4 : 7)
    }

    /// Contagem à direita do header, como "134 tools" na referência.
    private var metaHeader: String {
        let sessao = store.sessaoSelecionada
        switch tipo {
        case .transcript:
            guard let n = sessao?.transcript.count, n > 0 else { return "—" }
            return "\(n) msgs"
        case .ferramentas:
            guard let f = sessao?.ferramentas, !f.isEmpty else { return "—" }
            let erros = f.filter { $0.estado == .falhou }.count
            return erros > 0 ? "\(f.count) tools · \(erros) err" : "\(f.count) tools"
        case .subagentes:
            guard let s = sessao?.subagentes, !s.isEmpty else { return "—" }
            return "\(s.count + s.map(\.filhos.count).reduce(0, +)) agentes"
        case .uso:
            return Fmt.usd(sessao?.uso?.gastoUSD)
        case .tarefas:
            guard let t = sessao?.tarefas, !t.isEmpty else { return "—" }
            return "\(t.filter { $0.estado == .concluida }.count)/\(t.count)"
        case .servidores:
            let rodando = store.servidores.filter(\.rodando).count
            return "\(rodando) rodando"
        case .rotas:
            return "\(store.rotas.count) rotas"
        case .board:
            let ativas = store.snapshot.sessoes.filter {
                ColunaBoard.coluna(para: $0.ref.estado) == .ativas
            }.count
            return "\(Fmt.contagem(ativas, piso: store.snapshot.contagensSaoPiso)) ativas"
        }
    }

    @ViewBuilder
    private var conteudo: some View {
        switch tipo {
        case .transcript: TranscriptView()
        case .ferramentas: FerramentasView()
        case .subagentes: SubagentesView()
        case .uso: UsoView()
        case .tarefas: TarefasView()
        case .servidores: ServidoresView()
        case .rotas: RotasView()
        case .board: BoardView()
        }
    }
}

// MARK: - Rail compacto mínimo (badge na borda), clique expande

struct RailView: View {
    @Environment(WidgetStore.self) private var store
    let tipo: TipoWidget
    let controlador: PainelWidget
    @State private var pairando = false

    var body: some View {
        Button {
            controlador.sairDoRail()
        } label: {
            VStack(spacing: 6) {
                ZStack {
                    Circle().fill(Paleta.cartao).frame(width: 24, height: 24)
                    Image(systemName: tipo.icone)
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(corResumo)
                }
                Text(numeroResumo)
                    .font(.system(size: 9.5, weight: .bold, design: .monospaced))
                    .lineLimit(1)
                    .minimumScaleFactor(0.6)
                Image(systemName: controlador.railNaEsquerda ? "chevron.right" : "chevron.left")
                    .font(.system(size: 7, weight: .bold))
                    .foregroundStyle(.tertiary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .background(Paleta.painel, in: RoundedRectangle(cornerRadius: 11, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 11, style: .continuous)
                .strokeBorder(Paleta.borda, lineWidth: 1)
        )
        .brightness(pairando ? 0.1 : 0)
        .onHover { pairando = $0 }
        .animation(.easeOut(duration: 0.15), value: pairando)
        .focusable()
        .accessibilityLabel("\(tipo.titulo) compacto: \(numeroResumo). Clique para expandir")
    }

    /// O essencial de cada widget num número (regra 4: sem dado é —, nunca 0).
    private var numeroResumo: String {
        let sessao = store.sessaoSelecionada
        switch tipo {
        case .transcript:
            guard let n = sessao?.transcript.count else { return "—" }
            return "\(n)"
        case .ferramentas:
            guard let f = sessao?.ferramentas else { return "—" }
            let erros = f.filter { $0.estado == .falhou }.count
            return erros > 0 ? "\(erros)!" : "\(f.count)"
        case .subagentes:
            guard let s = sessao?.subagentes, !s.isEmpty else { return "—" }
            return "\(s.count)"
        case .uso:
            return Fmt.usd(sessao?.uso?.gastoUSD)
        case .tarefas:
            guard let t = sessao?.tarefas, !t.isEmpty else { return "—" }
            return "\(t.filter { $0.estado == .concluida }.count)/\(t.count)"
        case .servidores:
            return "\(store.servidores.filter(\.rodando).count)"
        case .rotas:
            return "\(store.rotas.count)"
        case .board:
            let atencao = store.snapshot.sessoes.filter {
                ColunaBoard.coluna(para: $0.ref.estado) == .atencao
            }.count
            return Fmt.contagem(atencao, piso: store.snapshot.contagensSaoPiso)
        }
    }

    private var corResumo: Color {
        if tipo == .board {
            let temAtencao = store.snapshot.sessoes.contains {
                ColunaBoard.coluna(para: $0.ref.estado) == .atencao
            }
            return temAtencao ? .orange : .secondary
        }
        if tipo == .ferramentas,
           store.sessaoSelecionada?.ferramentas.contains(where: { $0.estado == .falhou }) == true {
            return .red
        }
        return .secondary
    }
}

// MARK: - Estado vazio digno

struct VazioView: View {
    let icone: String
    let texto: String

    var body: some View {
        VStack(spacing: 8) {
            Image(systemName: icone)
                .font(.system(size: 18))
                .foregroundStyle(.tertiary)
            Text(texto)
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(24)
        .accessibilityElement(children: .combine)
    }
}

// MARK: - Botões auxiliares

struct BotaoCirculoHover: ButtonStyle {
    @State private var pairando = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .background(Circle().fill(.primary.opacity(pairando ? 0.1 : 0)))
            .opacity(configuration.isPressed ? 0.6 : 1)
            .onHover { pairando = $0 }
            .animation(.easeOut(duration: 0.12), value: pairando)
    }
}

// MARK: - Glifo por ferramenta (como a coluna de ícones da referência)

func glifoFerramenta(_ nome: String) -> String {
    switch nome.lowercased() {
    case "bash", "exec_command": "terminal"
    case "read": "doc.text"
    case "edit", "apply_patch", "write": "pencil"
    case "grep", "glob": "magnifyingglass"
    case "git", "git commit", "git diff", "git status": "arrow.triangle.branch"
    default: "wrench.and.screwdriver"
    }
}
