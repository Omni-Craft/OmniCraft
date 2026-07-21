import SwiftUI
import Observation

/// Fonte dos dados do HUD: fixtures (desenvolvimento) ou o feed real.
enum FeedSource: String, CaseIterable, Identifiable {
    case mock
    case live

    var id: String { rawValue }
    var label: String {
        switch self {
        case .mock: "Cenários mock"
        case .live: "Feed real"
        }
    }
}

/// Onde a ilha aparece: fundida à notch (padrão) ou só no popover da barra de
/// menus (melhor em Mac sem notch / display externo).
enum ModoExibicao: String, CaseIterable, Identifiable {
    case notch
    case soBarraDeMenus

    var id: String { rawValue }
    var label: String {
        switch self {
        case .notch: "Notch + barra de menus"
        case .soBarraDeMenus: "Só barra de menus"
        }
    }
}

/// Estado central do HUD. Fixtures ou feed real — as views não sabem a diferença.
@MainActor
@Observable
final class HUDStore {
    static let baseURLDefaultsKey = "OmniCraftFeedBaseURL"

    var scenario: MockScenario = .tresAtivasUmaAguardando {
        didSet { if feedSource == .mock { applyScenario() } }
    }
    var visibility: PillVisibility = .sempre
    var isExpanded: Bool = false
    var modo: ModoExibicao = .notch

    /// Expansão veio do hover (não de clique/atenção): sair com o mouse recolhe.
    private(set) var expandidoPorHover = false

    /// Ao vivo por padrão: a ilha existe para mostrar o que os agentes estão
    /// fazendo agora. As fixtures continuam a um clique no painel de debug,
    /// que é onde elas servem.
    var feedSource: FeedSource = .live {
        didSet { feedSourceChanged() }
    }

    /// Base do servidor local; persiste em UserDefaults e vale na próxima busca.
    var baseURLString: String = UserDefaults.standard.string(forKey: HUDStore.baseURLDefaultsKey)
        ?? "http://127.0.0.1:6767" {
        didSet {
            // Só persiste mudança REAL: a TextField do debug reescreve o mesmo valor
            // ao renderizar, e sem esta guarda um -OmniCraftFeedBaseURL de teste
            // (domínio de argumentos) vazaria para o domínio persistente do app.
            guard baseURLString != oldValue else { return }
            UserDefaults.standard.set(baseURLString, forKey: Self.baseURLDefaultsKey)
            if feedSource == .live { restartPolling(immediate: true) }
        }
    }

    private(set) var snapshot: FeedSnapshot = FeedSnapshot(
        counts: .unavailable, sessions: [], janelasLimite: [])
    private(set) var isDisconnected = false
    private(set) var lastGeneratedAt: Date?

    /// Pedidos já vistos: colapsar manualmente marca os atuais como vistos,
    /// e o auto-expandir só dispara para pedido NOVO (nunca reabre o que a pessoa fechou).
    private var seenRequestIDs: Set<String> = []

    /// Pedidos já decididos. No feed real só entram aqui depois que o servidor
    /// confirma: sumir da pilha é o que diz "resolvido", então antecipar isso
    /// seria mentir sobre uma aprovação que pode não ter acontecido.
    private var resolvedRequestIDs: Set<String> = []

    /// Pedidos com decisão em voo (botões desabilitados enquanto isso).
    private(set) var pendingRequestIDs: Set<String> = []

    /// Se o pedido já saiu da pilha por decisão confirmada.
    func pedidoFoiResolvido(_ id: String) -> Bool { resolvedRequestIDs.contains(id) }

    /// Motivo da última falha por pedido — fica visível ao lado dele até a
    /// pessoa tentar de novo. Uma decisão que não chegou ao servidor não pode
    /// desaparecer em silêncio.
    private(set) var falhaPorPedido: [String: String] = [:]

    /// Índice na FILA GLOBAL de pedidos (todas as sessões empilham juntas).
    var indicePedido: Int = 0

    /// Registro visível do que os botões (só visuais) fizeram.
    private(set) var actionLog: [String] = []

    private let client: OmniCraftAPI
    private var pollTask: Task<Void, Never>?

    /// - Parameter client: Quem fala com o OmniCraft; trocado nos testes.
    ///
    /// `didSet` não roda na inicialização, então a fonte escolhida precisa ser
    /// aplicada aqui — sem isto a ilha abriria ao vivo e nunca buscaria nada.
    init(client: OmniCraftAPI = FeedClient()) {
        self.client = client
        feedSourceChanged()
    }

    // MARK: Derivados

    /// Sessões com pedidos resolvidos filtrados; atenção no topo, depois por urgência.
    var visibleSessions: [AgentSession] {
        let filtered = snapshot.sessions.map { session in
            var s = session
            s.requests.removeAll { resolvedRequestIDs.contains($0.id) }
            if s.state == .aguardandoVoce && s.requests.isEmpty && !session.requests.isEmpty {
                s.state = .ocioso   // todos os pedidos decididos localmente
            }
            return s
        }
        return filtered.sorted { a, b in
            if a.needsAttention != b.needsAttention { return a.needsAttention }
            return rank(a.state) < rank(b.state)
        }
    }

    private func rank(_ state: SessionState) -> Int {
        switch state {
        case .aguardandoVoce: 0
        case .emExecucao: 1
        case .falhou: 2
        case .desconhecido: 3
        case .ocioso: 4
        }
    }

    var hasAttention: Bool { visibleSessions.contains(where: \.needsAttention) }

    /// Fila global: pedidos de TODAS as sessões, na ordem das sessões visíveis.
    var pedidosPendentes: [(sessao: AgentSession, pedido: AttentionRequest)] {
        visibleSessions.flatMap { sessao in
            sessao.requests.map { (sessao, $0) }
        }
    }

    var pedidoAtual: (sessao: AgentSession, pedido: AttentionRequest)? {
        let fila = pedidosPendentes
        guard !fila.isEmpty else { return nil }
        return fila[min(max(indicePedido, 0), fila.count - 1)]
    }

    /// O pill some no vazio (ou fica mínimo no modo "sempre").
    var pillVisible: Bool {
        switch visibility {
        case .sempre: return true
        case .esconderOcioso: return !snapshot.isEmpty
        case .soAtencao: return hasAttention
        }
    }

    // MARK: Ações

    func toggleExpanded() {
        if isExpanded { collapseManually() } else { expand() }
    }

    func expand(porHover: Bool = false) {
        expandidoPorHover = porHover && !isExpanded
        isExpanded = true
        // Expandiu = está olhando: busca já e acelera o ritmo (3 s).
        if feedSource == .live { restartPolling(immediate: true) }
    }

    /// Sair com o mouse recolhe SÓ o que o hover abriu — clique e atenção nova
    /// ficam abertos, e o hover-out NÃO marca pedidos como vistos.
    func colapsarPorSaidaDoHover() {
        guard expandidoPorHover else { return }
        expandidoPorHover = false
        isExpanded = false
        if feedSource == .live { restartPolling(immediate: false) }
    }

    func collapseManually() {
        // Fechou vendo estes pedidos → não reabrimos para eles.
        for session in visibleSessions {
            for request in session.requests { seenRequestIDs.insert(request.id) }
        }
        isExpanded = false
        expandidoPorHover = false
        if feedSource == .live { restartPolling(immediate: false) }  // desacelera (10 s)
    }

    func approve(_ request: AttentionRequest, in session: AgentSession) {
        decidir(request, in: session, decisao: .aceitar)
    }

    func reject(_ request: AttentionRequest, in session: AgentSession) {
        decidir(request, in: session, decisao: .recusar)
    }

    func approveAll(in session: AgentSession) {
        // Um POST por pedido: o servidor resolve um pedido de cada vez, e não
        // há endpoint de lote. Só os que têm id de verdade.
        for request in session.requests where request.isResolvable {
            decidir(request, in: session, decisao: .aceitar)
        }
    }

    /// Envia a decisão e só a dá por feita quando o servidor confirma.
    private func decidir(_ request: AttentionRequest, in session: AgentSession, decisao: Decisao) {
        let verbo = decisao == .aceitar ? "Aprovado" : "Rejeitado"
        let simbolo = decisao == .aceitar ? "✓" : "✕"

        guard request.isResolvable else {
            // Placeholder da fila ‹ 1 de N ›: não existe id para resolver.
            falhaPorPedido[request.id] = "abra a sessão no OmniCraft para decidir este"
            return
        }

        guard feedSource == .live else {
            // Cenários mock: a decisão é da fixture, não há servidor.
            log("\(simbolo) \(verbo): \(request.question) [\(session.title)]")
            resolvedRequestIDs.insert(request.id)
            clamparFila()
            return
        }

        falhaPorPedido[request.id] = nil
        pendingRequestIDs.insert(request.id)
        let base = baseURLString

        Task { [weak self] in
            do {
                try await self?.client.resolve(
                    baseURL: base,
                    sessionId: session.id,
                    elicitationId: request.id,
                    decisao: decisao)
                guard let self else { return }
                pendingRequestIDs.remove(request.id)
                resolvedRequestIDs.insert(request.id)
                log("\(simbolo) \(verbo): \(request.question) [\(session.title)]")
                clamparFila()
                restartPolling(immediate: true)   // reflete o novo estado sem esperar o tick
            } catch {
                guard let self else { return }
                pendingRequestIDs.remove(request.id)
                let motivo = (error as? FeedError)?.mensagem ?? error.localizedDescription
                falhaPorPedido[request.id] = motivo
                log("⚠︎ Falhou ao \(decisao == .aceitar ? "aprovar" : "rejeitar"): \(motivo)")
            }
        }
    }

    // MARK: Utilidades locais (ações visuais: log; copiar usa o clipboard de verdade)

    let servidores = MockFeed.servidores
    let comandos = MockFeed.comandos
    let atalhos = MockFeed.atalhos

    func copiar(_ texto: String, rotulo: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(texto, forType: .string)
        log("⧉ Copiado \(rotulo): \(texto)")
    }

    func acaoServidor(_ servidor: ServidorLocal, _ acao: String) {
        log("⚙ Servidor \(servidor.nome): \(acao) (visual)")
    }

    func abrirAtalho(_ atalho: AtalhoLocal) {
        log("→ Atalho: \(atalho.rotulo) (visual)")
    }

    // MARK: Fonte de dados

    private func feedSourceChanged() {
        pollTask?.cancel()
        pollTask = nil
        resolvedRequestIDs = []
        indicePedido = 0
        switch feedSource {
        case .mock:
            isDisconnected = false
            lastGeneratedAt = nil
            applyScenario()
        case .live:
            restartPolling(immediate: true)
        }
    }

    private func applyScenario() {
        snapshot = MockFeed.snapshot(for: scenario)
        resolvedRequestIDs = []
        indicePedido = 0
        autoExpandIfNewAttention()
    }

    // MARK: Polling do feed real

    /// Um único loop vivo por vez: relançar cancela o anterior (e o request em voo).
    private func restartPolling(immediate: Bool) {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            await self?.pollLoop(immediate: immediate)
        }
    }

    private func pollLoop(immediate: Bool) async {
        var consecutiveFailures = 0
        if !immediate {
            try? await Task.sleep(for: .seconds(normalInterval))
        }
        while !Task.isCancelled && feedSource == .live {
            do {
                let dto = try await client.fetch(baseURL: baseURLString)
                guard !Task.isCancelled else { return }
                consecutiveFailures = 0
                applyLive(dto)
            } catch {
                guard !Task.isCancelled else { return }
                consecutiveFailures += 1
                markDisconnected()
            }
            // Backoff em falha: 1 → 2 → 5 → 15 s (teto); saudável: 3 s aberto, 10 s fechado.
            let backoff: [Double] = [1, 2, 5, 15]
            let interval = consecutiveFailures > 0
                ? backoff[min(consecutiveFailures - 1, backoff.count - 1)]
                : normalInterval
            try? await Task.sleep(for: .seconds(interval))
        }
    }

    private var normalInterval: Double { isExpanded ? 3 : 10 }

    private func applyLive(_ dto: MonitorFeedDTO) {
        isDisconnected = false
        lastGeneratedAt = dto.generatedAt.map { Date(timeIntervalSince1970: $0) }
        snapshot = FeedMapper.snapshot(from: dto)
        autoExpandIfNewAttention()
    }

    /// Sem resposta = sem dado: nunca mostrar a última lista boa como se fosse o agora.
    private func markDisconnected() {
        isDisconnected = true
        snapshot = FeedSnapshot(counts: .disconnected, sessions: [])
    }

    // MARK: Internos

    /// Auto-expande só quando há pedido que a pessoa ainda não viu.
    private func autoExpandIfNewAttention() {
        let pending = visibleSessions.flatMap(\.requests).map(\.id)
        if pending.contains(where: { !seenRequestIDs.contains($0) }) {
            isExpanded = true
        }
    }

    private func clamparFila() {
        indicePedido = min(max(indicePedido, 0), max(pedidosPendentes.count - 1, 0))
    }

    private func log(_ message: String) {
        actionLog.append(message)
        print("[OmniCraftNotch] \(message)")
    }
}
