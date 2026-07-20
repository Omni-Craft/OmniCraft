import Foundation

// MARK: - DTOs do feed (espelham o JSON; TUDO opcional = decodificação tolerante)

struct MonitorFeedDTO: Decodable {
    var generatedAt: Double?
    var hostId: String?
    var counts: MonitorCountsDTO?
    var sessions: LossyArray<MonitorSessionItemDTO>?
    var degraded: [String]?
    var limitWindows: LossyArray<LimitWindowDTO>?
}

struct LimitWindowDTO: Decodable {
    var label: String?
    var usedFraction: Double?
    var resetsIn: String?
}

struct MonitorCountsDTO: Decodable {
    var active: Int?
    var awaiting: Int?
    var unknown: Int?
    var partial: Bool?
}

struct MonitorSessionItemDTO: Decodable {
    var sessionId: String?
    var title: String?
    var agentName: String?
    var status: String?
    var runnerOnline: Bool?
    var hostOnline: Bool?
    var costUsd: Double?
    var updatedAt: Double?
    var pendingElicitation: ElicitationDTO?
    var pendingElicitationsCount: Int?
    var usage: UsageDTO?
    var degraded: [String]?
    var currentTool: String?
    var diffAdded: Int?
    var diffRemoved: Int?
}

struct ElicitationDTO: Decodable {
    var id: String?
    var kind: String?
    var summary: String?
}

struct UsageDTO: Decodable {
    var source: String?
    var costUsd: Double?
    var budget: BudgetDTO?
}

struct BudgetDTO: Decodable {
    var maxCostUsd: Double?
    var source: String?
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
}

/// GET no feed do OmniCraft. Sem estado; um request por vez (quem garante é o Store).
struct FeedClient {
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

// MARK: - Mapeamento DTO → modelos da UI (as views não mudam)

enum FeedMapper {
    static func snapshot(from dto: MonitorFeedDTO) -> FeedSnapshot {
        let counts: CountsSummary
        if let c = dto.counts, let active = c.active, let awaiting = c.awaiting {
            // Regra 4: partial == true → as contagens são um PISO, nunca número exato.
            counts = (c.partial == true)
                ? .floor(active: active, waiting: awaiting)
                : .exact(active: active, waiting: awaiting)
        } else {
            counts = .unavailable
        }
        let sessions = (dto.sessions?.elements ?? []).map(mapSession)
        let janelas = (dto.limitWindows?.elements ?? []).enumerated().map { indice, janela in
            JanelaLimite(
                id: janela.label ?? "janela-\(indice)",
                rotulo: janela.label ?? "janela",
                fracaoUsada: janela.usedFraction.map { min(max($0, 0), 1) },
                reset: janela.resetsIn)
        }
        return FeedSnapshot(counts: counts, sessions: sessions, janelasLimite: janelas)
    }

    static func mapSession(_ dto: MonitorSessionItemDTO) -> AgentSession {
        let id = dto.sessionId ?? "sem-id-\(dto.title ?? dto.agentName ?? "?")"
        let degraded = Set(dto.degraded ?? [])

        let title = dto.title
            ?? dto.agentName
            ?? (dto.sessionId.map { String($0.prefix(14)) + "…" } ?? "sessão sem nome")

        // Um pedido pendente sempre significa "aguardando você", venha o status que vier.
        let state: SessionState
        if dto.pendingElicitation != nil {
            state = .aguardandoVoce
        } else if !degraded.isDisjoint(with: ["status_unknown", "status_unreadable"]) {
            // Regra 3: degraded nunca vira silêncio nem "ocioso" — mostra a incerteza.
            state = .desconhecido
        } else {
            switch dto.status {
            case "running", "launching": state = .emExecucao
            case "waiting": state = .aguardandoVoce
            case "idle": state = .ocioso
            case "failed": state = .falhou
            default: state = .desconhecido
            }
        }

        // Regra 1: barra SÓ com gasto E teto (budget.max_cost_usd); sem teto, texto.
        // usage.source é sempre "local_counter" — nunca derive porcentagem de token cru.
        let usage: Usage
        if !degraded.isDisjoint(with: ["usage_unreadable", "cost_unreadable"]) {
            usage = Usage()   // ilegível → desconhecido (—), nunca zero
        } else if let spent = dto.usage?.costUsd, let cap = dto.usage?.budget?.maxCostUsd {
            usage = Usage(spentUSD: spent, capUSD: cap)
        } else {
            usage = Usage(spentUSD: dto.costUsd ?? dto.usage?.costUsd, capUSD: nil)
        }

        var requests: [AttentionRequest] = []
        if let elicitation = dto.pendingElicitation {
            requests.append(AttentionRequest(
                id: elicitation.id ?? "\(id)-pedido-0",
                title: "Aprovação necessária",
                question: elicitation.summary ?? "Pedido pendente"))
            // O feed traz só o primeiro pedido + a contagem: os demais entram como
            // placeholders honestos para a navegação ‹ 1 de N › existir.
            let total = max(dto.pendingElicitationsCount ?? 1, 1)
            for index in 1..<total {
                requests.append(AttentionRequest(
                    id: "\(id)-pedido-\(index)",
                    title: "Aprovação necessária",
                    question: "Pedido pendente (detalhes ainda não carregados)"))
            }
        }

        return AgentSession(
            id: id,
            title: title,
            state: state,
            runnerOnline: dto.runnerOnline,
            host: dto.hostOnline.map { $0 ? "online" : "offline" },
            usage: usage,
            requests: requests,
            ferramentaAtual: dto.currentTool,
            diffMais: dto.diffAdded,
            diffMenos: dto.diffRemoved
        )
    }
}
