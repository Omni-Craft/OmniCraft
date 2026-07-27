import Foundation

// ============================================================================
// Feed real dos widgets — `GET <base>/v1/monitor/sessions`, o mesmo do notch.
//
// O feed descreve SESSÕES: título, estado, gasto, pedido pendente, janelas de
// limite. Não traz transcript, chamadas de ferramenta, subagentes nem tarefas —
// esses vivem nos itens da sessão, que ainda não são buscados aqui. Por isso o
// mapeamento abaixo preenche só o `ref` de cada sessão e deixa o resto vazio:
// inventar detalhe seria pior que não ter.
// ============================================================================

// MARK: - DTOs (tudo opcional: decodificação tolerante a um servidor mais novo)

struct MonitorFeedDTO: Decodable {
    var counts: MonitorCountsDTO?
    var sessions: LossyArray<MonitorSessionItemDTO>?
    var limitWindows: LossyArray<LimitWindowDTO>?
}

struct MonitorCountsDTO: Decodable {
    var active: Int?
    var awaiting: Int?
    var partial: Bool?
}

struct LimitWindowDTO: Decodable {
    var label: String?
    var usedFraction: Double?
    var resetsIn: String?
}

struct MonitorSessionItemDTO: Decodable {
    var sessionId: String?
    var title: String?
    var agentName: String?
    var workspace: String?
    var status: String?
    var costUsd: Double?
    var updatedAt: Double?
    var pendingElicitation: ElicitationDTO?
    var usage: UsageDTO?
    var degraded: [String]?
    var currentTool: String?
}

struct ElicitationDTO: Decodable {
    var id: String?
    var summary: String?
}

struct UsageDTO: Decodable {
    var costUsd: Double?
    var budget: BudgetDTO?
}

struct BudgetDTO: Decodable {
    var maxCostUsd: Double?
}

/// Array que pula elementos malformados em vez de derrubar o decode inteiro —
/// o app nunca quebra porque o servidor evoluiu.
struct LossyArray<Element: Decodable>: Decodable {
    var elements: [Element] = []

    private struct AnythingDecodable: Decodable {}

    init(from decoder: Decoder) throws {
        var container = try decoder.unkeyedContainer()
        while !container.isAtEnd {
            if let element = try? container.decode(Element.self) {
                elements.append(element)
            } else {
                _ = try? container.decode(AnythingDecodable.self)  // pula o item ruim
            }
        }
    }
}

// MARK: - Client

enum FeedError: Error {
    case invalidURL
    case badStatus(Int)

    /// Texto curto para a UI — o motivo precisa caber numa linha do menu.
    var mensagem: String {
        switch self {
        case .invalidURL: "endereço do servidor inválido"
        case .badStatus(let code): "o servidor respondeu \(code)"
        }
    }
}

/// Quem sabe conversar com o OmniCraft. Existe para o Store poder ser testado
/// com um duplo, sem rede: o que importa checar é o que ele faz com a resposta.
protocol WidgetsAPI: Sendable {
    func fetch(baseURL: String) async throws -> MonitorFeedDTO
}

/// GET no feed. Sem estado; um request por vez (quem garante é o Store).
struct FeedClient: WidgetsAPI {
    private let session: URLSession

    init() {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 3
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        session = URLSession(configuration: config)
    }

    func fetch(baseURL: String) async throws -> MonitorFeedDTO {
        guard let url = URL(string: baseURL)?.appending(path: "v1/monitor/sessions") else {
            throw FeedError.invalidURL
        }
        let (data, response) = try await session.data(from: url)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            throw FeedError.badStatus(http.statusCode)
        }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(MonitorFeedDTO.self, from: data)
    }
}

// MARK: - Mapeamento DTO → modelos dos widgets (as views não mudam)

enum FeedMapper {
    static func snapshot(from dto: MonitorFeedDTO) -> SnapshotWidgets {
        let janelas = (dto.limitWindows?.elements ?? []).enumerated().map { indice, janela in
            JanelaLimite(
                id: janela.label ?? "janela-\(indice)",
                rotulo: janela.label ?? "janela",
                fracaoUsada: janela.usedFraction.map { min(max($0, 0), 1) },
                reset: janela.resetsIn)
        }
        return SnapshotWidgets(
            sessoes: (dto.sessions?.elements ?? []).map(mapSession),
            contagensSaoPiso: dto.counts?.partial == true,
            janelasLimite: janelas)
    }

    static func mapSession(_ dto: MonitorSessionItemDTO) -> SessaoDetalhe {
        let id = dto.sessionId ?? "sem-id-\(dto.title ?? dto.agentName ?? "?")"
        let degraded = Set(dto.degraded ?? [])

        // Um pedido pendente sempre significa "aguardando você", venha o status
        // que vier; um estado ilegível vira "desconhecido", nunca "ocioso".
        let estado: EstadoSessao
        if dto.pendingElicitation != nil {
            estado = .aguardandoVoce
        } else if !degraded.isDisjoint(with: ["status_unknown", "status_unreadable"]) {
            estado = .desconhecido
        } else {
            switch dto.status {
            case "running", "launching": estado = .emExecucao
            case "waiting": estado = .aguardandoVoce
            case "idle": estado = .ocioso
            case "failed": estado = .falhou
            case "completed": estado = .concluida
            default: estado = .desconhecido
            }
        }

        let ref = SessaoRef(
            id: id,
            titulo: dto.title
                ?? dto.agentName
                ?? (dto.sessionId.map { String($0.prefix(14)) + "…" } ?? "sessão sem nome"),
            projeto: dto.workspace.map { ($0 as NSString).lastPathComponent } ?? "—",
            agente: dto.agentName ?? "—",
            estado: estado,
            motivoAtencao: motivoAtencao(dto, estado: estado),
            haQuantoTempo: dto.updatedAt.map(idadeRelativa),
            subestado: dto.currentTool)

        // Barra de gasto SÓ com teto declarado; ilegível vira desconhecido (—),
        // nunca zero.
        let uso: UsoSessao?
        if !degraded.isDisjoint(with: ["usage_unreadable", "cost_unreadable"]) {
            uso = nil
        } else if let gasto = dto.usage?.costUsd ?? dto.costUsd {
            uso = UsoSessao(gastoUSD: gasto, tetoUSD: dto.usage?.budget?.maxCostUsd)
        } else {
            uso = nil
        }

        return SessaoDetalhe(ref: ref, uso: uso)
    }

    /// Por que a sessão está na coluna Atenção — o card mostra o motivo.
    private static func motivoAtencao(
        _ dto: MonitorSessionItemDTO, estado: EstadoSessao
    ) -> String? {
        switch estado {
        case .aguardandoVoce:
            dto.pendingElicitation?.summary ?? "Esperando a sua decisão"
        case .desconhecido:
            "O servidor não conseguiu ler o estado desta sessão"
        default:
            nil
        }
    }

    /// "agora" · "há 40 s" · "há 12 min" · "há 3 h" — no momento do snapshot.
    static func idadeRelativa(_ epoch: Double) -> String {
        let s = max(Int(Date().timeIntervalSince1970 - epoch), 0)
        if s < 10 { return "agora" }
        if s < 60 { return "há \(s) s" }
        if s < 3600 { return "há \(s / 60) min" }
        return "há \(s / 3600) h"
    }
}
