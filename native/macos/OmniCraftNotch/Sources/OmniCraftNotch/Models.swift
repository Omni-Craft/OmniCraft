import Foundation

// MARK: - Estado de uma sessão de agente

enum SessionState: String, CaseIterable, Identifiable {
    case emExecucao
    case aguardandoVoce
    case ocioso
    case falhou
    case desconhecido

    var id: String { rawValue }

    /// Rótulo visível — o estado nunca é comunicado só por cor.
    var label: String {
        switch self {
        case .emExecucao: "em execução"
        case .aguardandoVoce: "aguardando você"
        case .ocioso: "ocioso"
        case .falhou: "falhou"
        case .desconhecido: "desconhecido"
        }
    }

    var symbolName: String {
        switch self {
        case .emExecucao: "play.circle.fill"
        case .aguardandoVoce: "bell.fill"
        case .ocioso: "moon.zzz.fill"
        case .falhou: "xmark.octagon.fill"
        case .desconhecido: "questionmark.circle.fill"
        }
    }
}

// MARK: - Uso (gasto e teto)

/// Barra só existe quando há gasto E teto — sem denominador não se inventa porcentagem.
struct Usage: Equatable {
    var spentUSD: Double?
    var capUSD: Double?

    var fraction: Double? {
        guard let spentUSD, let capUSD, capUSD > 0 else { return nil }
        return min(max(spentUSD / capUSD, 0), 1)
    }
}

// MARK: - Pedido de atenção (aprovação inline)

struct AttentionRequest: Identifiable, Equatable {
    let id: String
    var title: String      // ex.: "Aprovação necessária"
    var question: String   // ex.: "Permitir rodar `git push`…?"
}

// MARK: - Sessão

struct AgentSession: Identifiable, Equatable {
    let id: String
    var title: String
    var state: SessionState
    var runnerOnline: Bool?   // nil = desconhecido
    var host: String?
    var usage: Usage = Usage()
    var requests: [AttentionRequest] = []
    var ferramentaAtual: String?   // "Bash · npm test" — o que roda AGORA
    var diffMais: Int?             // +N do diff da sessão; nil = desconhecido
    var diffMenos: Int?

    var needsAttention: Bool { state == .aguardandoVoce && !requests.isEmpty }

    /// "runner: online · host: macbook · custo: US$ 0,42" — desconhecido vira "—".
    var metadataLine: String {
        let runner = runnerOnline.map { $0 ? "online" : "offline" } ?? "—"
        let hostText = host ?? "—"
        let custo = Formatters.usd(usage.spentUSD)
        return "runner: \(runner) · host: \(hostText) · custo: \(custo)"
    }

    /// Diffs "+12 −5" quando conhecidos (nunca 0 inventado).
    var diffTexto: String? {
        guard let diffMais, let diffMenos else { return nil }
        return "+\(diffMais) −\(diffMenos)"
    }
}

// MARK: - Contagens do pill

/// Contagens agregadas: exatas, piso (dado degradado), ilegíveis ou sem conexão.
enum CountsSummary: Equatable {
    case exact(active: Int, waiting: Int)
    case floor(active: Int, waiting: Int)
    case unavailable
    case disconnected

    var pillText: String {
        switch self {
        case let .exact(active, waiting):
            waiting > 0 ? "\(Self.ativas(active)) · \(waiting) aguardando" : Self.ativas(active)
        case let .floor(active, waiting):
            waiting > 0 ? "≥\(Self.ativas(active)) · ≥\(waiting) aguardando" : "≥\(Self.ativas(active))"
        case .unavailable:
            "contagens indisponíveis"
        case .disconnected:
            "sem conexão com o OmniCraft"
        }
    }

    private static func ativas(_ n: Int) -> String {
        n == 1 ? "1 ativa" : "\(n) ativas"
    }

    var isUnavailable: Bool {
        switch self {
        case .unavailable, .disconnected: true
        default: false
        }
    }
}

// MARK: - Janelas de limite do provedor (estilo "5H 52% used")

/// Fração com denominador REAL (a janela de rate-limit do provedor) — por isso
/// barra é legítima aqui. `fracaoUsada` nil = janela ilegível → mostra —.
struct JanelaLimite: Identifiable, Equatable {
    let id: String
    var rotulo: String        // "5 h" · "7 d"
    var fracaoUsada: Double?
    var reset: String?        // "reseta em 2 h 05" — string pronta; nil = desconhecido
}

// MARK: - Snapshot do feed

struct FeedSnapshot: Equatable {
    var counts: CountsSummary
    var sessions: [AgentSession]
    var janelasLimite: [JanelaLimite] = []

    var isEmpty: Bool {
        sessions.isEmpty && !counts.isUnavailable
    }
}

// MARK: - Utilidades locais (painel "tudo a um clique" — só visual/log nesta etapa)

struct ServidorLocal: Identifiable, Equatable {
    let id: String
    var nome: String
    var url: String
    var rodando: Bool
}

struct ComandoSalvo: Identifiable, Equatable {
    let id: String
    var rotulo: String
    var comando: String
}

struct AtalhoLocal: Identifiable, Equatable {
    let id: String
    var rotulo: String
    var icone: String
}

// MARK: - Modo de visibilidade do pill

enum PillVisibility: String, CaseIterable, Identifiable {
    case sempre
    case esconderOcioso
    case soAtencao

    var id: String { rawValue }

    var label: String {
        switch self {
        case .sempre: "Sempre visível"
        case .esconderOcioso: "Esconder quando ocioso"
        case .soAtencao: "Só quando há atenção"
        }
    }
}

// MARK: - Formatação

enum Formatters {
    /// "US$ 0,42" em pt-BR; nil vira "—" — nunca um zero inventado.
    static func usd(_ value: Double?) -> String {
        guard let value else { return "—" }
        return "US$ " + String(format: "%.2f", value).replacingOccurrences(of: ".", with: ",")
    }

    /// "14:32:05" — horário local, para marcar de quando é o feed.
    static func hora(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss"
        return formatter.string(from: date)
    }
}
