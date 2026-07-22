import Foundation

// ============================================================================
// Modelos PUROS dos widgets — só Foundation, nenhuma dependência do app.
// Este arquivo é o contrato que será mapeado para o feed real depois.
// Vocabulário de estado idêntico ao OmniCraftNotch.
// ============================================================================

// MARK: - Estado de sessão / subagente

enum EstadoSessao: String, CaseIterable, Equatable {
    case emExecucao
    case aguardandoVoce
    case ocioso
    case falhou
    case concluida
    case desconhecido

    /// Rótulo visível — estado nunca é comunicado só por cor.
    var label: String {
        switch self {
        case .emExecucao: "em execução"
        case .aguardandoVoce: "aguardando você"
        case .ocioso: "ocioso"
        case .falhou: "falhou"
        case .concluida: "concluída"
        case .desconhecido: "desconhecido"
        }
    }

    var symbolName: String {
        switch self {
        case .emExecucao: "play.circle.fill"
        case .aguardandoVoce: "bell.fill"
        case .ocioso: "moon.zzz.fill"
        case .falhou: "xmark.octagon.fill"
        case .concluida: "checkmark.circle.fill"
        case .desconhecido: "questionmark.circle.fill"
        }
    }
}

// MARK: - Referência de sessão (cards do board)

struct SessaoRef: Identifiable, Equatable {
    let id: String
    var titulo: String
    var projeto: String
    var agente: String
    var estado: EstadoSessao
    var motivoAtencao: String?    // preenchido quando está na coluna Atenção
    var haQuantoTempo: String?    // "há 2 min" — string pronta; nil = desconhecido
    var subestado: String?        // "pensando" · "compactando · 45 s" — detalhe vivo
}

/// Coluna do board — SEMPRE derivada do estado, nunca escolhida à mão.
enum ColunaBoard: String, CaseIterable, Identifiable {
    case ativas, atencao, concluidas

    var id: String { rawValue }
    var titulo: String {
        switch self {
        case .ativas: "Ativas"
        case .atencao: "Atenção"
        case .concluidas: "Concluídas"
        }
    }

    static func coluna(para estado: EstadoSessao) -> ColunaBoard {
        switch estado {
        case .emExecucao: .ativas
        // Regra 3: desconhecido nunca some nem vira "ocioso" — pede atenção.
        case .aguardandoVoce, .ocioso, .desconhecido: .atencao
        case .concluida, .falhou: .concluidas
        }
    }
}

// MARK: - Transcript

enum AutorMensagem: Equatable {
    case voce
    case agente

    var label: String {
        switch self {
        case .voce: "você"
        case .agente: "agente"
        }
    }
}

struct BlocoFerramenta: Identifiable, Equatable {
    let id: String
    var nome: String          // "Bash"
    var alvo: String          // "npm test"
    var detalhe: String?      // conteúdo mostrado ao expandir
}

enum ConteudoTranscript: Equatable {
    case texto(String)
    case ferramenta(BlocoFerramenta)
}

struct EntradaTranscript: Identifiable, Equatable {
    let id: String
    var autor: AutorMensagem?
    var conteudo: ConteudoTranscript
    var hora: String              // "14:32"
    var emStreaming: Bool = false
}

// MARK: - Ferramentas

enum EstadoFerramenta: Equatable {
    case executando
    case concluida
    case falhou

    var label: String {
        switch self {
        case .executando: "executando"
        case .concluida: "concluída"
        case .falhou: "falhou"
        }
    }

    var symbolName: String {
        switch self {
        case .executando: "circle.dotted"
        case .concluida: "checkmark.circle.fill"
        case .falhou: "xmark.octagon.fill"
        }
    }
}

struct ChamadaFerramenta: Identifiable, Equatable {
    let id: String
    var nome: String              // "Bash"
    var alvo: String              // "npm test" (resumo em uma linha)
    var duracao: String?          // "1,2 s" — nil = ainda não sabe
    var estado: EstadoFerramenta
    var primeiraLinhaErro: String?  // SÓ a primeira linha; sem stack trace
}

// MARK: - Subagentes

struct Subagente: Identifiable, Equatable {
    let id: String
    var nome: String
    var tarefa: String
    var estado: EstadoSessao
    var haQuantoTempo: String
    var filhos: [Subagente] = []

    var precisaAtencao: Bool { estado == .aguardandoVoce }
}

// MARK: - Uso

/// Regra 1: barra SÓ quando há gasto E teto. Regra 2: o teto é o
/// "orçamento do agente" — pode existir limite mais apertado em vigor.
struct UsoSessao: Equatable {
    var gastoUSD: Double?
    var tetoUSD: Double?
    var tokensEntrada: Int?
    var tokensSaida: Int?
    var tokensCacheLeitura: Int?
    var tokensCacheCriacao: Int?

    var fracao: Double? {
        guard let gastoUSD, let tetoUSD, tetoUSD > 0 else { return nil }
        return min(max(gastoUSD / tetoUSD, 0), 1)
    }
}

// MARK: - Tarefas

enum EstadoTarefa: Equatable {
    case pendente
    case emAndamento
    case concluida

    var label: String {
        switch self {
        case .pendente: "pendente"
        case .emAndamento: "em andamento"
        case .concluida: "concluída"
        }
    }

    var symbolName: String {
        switch self {
        case .pendente: "circle"
        case .emAndamento: "arrow.triangle.2.circlepath.circle.fill"
        case .concluida: "checkmark.circle.fill"
        }
    }
}

struct Tarefa: Identifiable, Equatable {
    let id: String
    var titulo: String
    var estado: EstadoTarefa
}

// MARK: - Sessão completa e snapshot

struct SessaoDetalhe: Identifiable, Equatable {
    var ref: SessaoRef
    var transcript: [EntradaTranscript] = []
    var ferramentas: [ChamadaFerramenta] = []
    var subagentes: [Subagente] = []
    var uso: UsoSessao?           // nil = sem dado nenhum (—)
    var tarefas: [Tarefa] = []

    var id: String { ref.id }
}

struct SnapshotWidgets: Equatable {
    var sessoes: [SessaoDetalhe] = []
    /// Regra 3: contagem que é piso mostra ≥, nunca número exato.
    var contagensSaoPiso: Bool = false
    /// Janelas de rate-limit do provedor — denominador REAL, barra legítima.
    var janelasLimite: [JanelaLimite] = []
}

// MARK: - Janela de limite do provedor ("5 h · 52% · reseta em 2 h 05")

struct JanelaLimite: Identifiable, Equatable {
    let id: String
    var rotulo: String        // "5 h" · "7 d"
    var fracaoUsada: Double?  // nil = ilegível → — (nunca barra inventada)
    var reset: String?        // "reseta em 2 h 05"
}

// MARK: - Servidores locais e rotas (widgets utilitários; ações visuais/log)

struct ServidorLocal: Identifiable, Equatable {
    let id: String
    var nome: String
    var host: String           // "localhost:8080"
    var framework: String?
    var projeto: String?
    var uptime: String?        // "há 40 min" — nil = desconhecido
    var rodando: Bool
    var principal: Bool = true // false = grupo "outros ouvintes"

    var url: String { "http://\(host)" }
}

struct RotaLocal: Identifiable, Equatable {
    let id: String
    var rotulo: String
    var icone: String
    var corNome: String = "cinza"  // "laranja" · "azul" · "verde" · "cinza"
}

// MARK: - Formatação (pt-BR; regra 4: ausente é —, nunca 0)

enum Fmt {
    static func usd(_ valor: Double?) -> String {
        guard let valor else { return "—" }
        return "US$ " + String(format: "%.2f", valor).replacingOccurrences(of: ".", with: ",")
    }

    static func tokens(_ n: Int?) -> String {
        guard let n else { return "—" }
        if n >= 1_000_000 { return String(format: "%.1f M", Double(n) / 1_000_000).replacingOccurrences(of: ".", with: ",") }
        if n >= 1_000 { return String(format: "%.1f k", Double(n) / 1_000).replacingOccurrences(of: ".", with: ",") }
        return "\(n)"
    }

    /// "3" ou "≥3", conforme a contagem for exata ou piso.
    static func contagem(_ n: Int, piso: Bool) -> String {
        piso ? "≥\(n)" : "\(n)"
    }
}
