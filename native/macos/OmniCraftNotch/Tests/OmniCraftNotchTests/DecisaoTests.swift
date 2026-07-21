import XCTest

@testable import OmniCraftNotch

/// Um OmniCraft de mentira: responde o que o teste mandar e anota o que recebeu.
private final class APIDuplo: OmniCraftAPI, @unchecked Sendable {
    var erroAoResolver: Error?
    private(set) var resolvidos: [(sessao: String, pedido: String, decisao: Decisao)] = []

    func fetch(baseURL: String) async throws -> MonitorFeedDTO {
        MonitorFeedDTO()
    }

    func resolve(
        baseURL: String, sessionId: String, elicitationId: String, decisao: Decisao
    ) async throws {
        if let erroAoResolver { throw erroAoResolver }
        resolvidos.append((sessionId, elicitationId, decisao))
    }
}

/// O que acontece com um pedido quando a decisão é enviada.
///
/// Sumir da pilha é o que a ilha usa para dizer "resolvido". Se sumir sem o
/// servidor ter aceitado, a pessoa acredita ter aprovado algo que segue
/// parado — o modo de falha mais caro desta tela, e o motivo destes testes.
@MainActor
final class DecisaoTests: XCTestCase {
    private let pedido = AttentionRequest(
        id: "elic_1", title: "Aprovação necessária", question: "Permitir git push?")

    private func sessao(_ requests: [AttentionRequest]) -> AgentSession {
        AgentSession(id: "conv_1", title: "Sessão", state: .aguardandoVoce, requests: requests)
    }

    private func store(_ api: APIDuplo) -> HUDStore {
        let store = HUDStore(client: api)
        store.feedSource = .live
        return store
    }

    func testPedidoAceitoPeloServidorSaiDaPilha() async throws {
        let api = APIDuplo()
        let store = store(api)

        store.approve(pedido, in: sessao([pedido]))
        try await esperar { !store.pendingRequestIDs.contains(self.pedido.id) }

        XCTAssertEqual(api.resolvidos.count, 1)
        XCTAssertEqual(api.resolvidos[0].pedido, "elic_1")
        XCTAssertEqual(api.resolvidos[0].decisao, .aceitar)
        XCTAssertNil(store.falhaPorPedido[pedido.id])
        XCTAssertTrue(store.pedidoFoiResolvido(pedido.id))
    }

    func testPedidoRecusadoPeloServidorContinuaNaPilha() async throws {
        let api = APIDuplo()
        api.erroAoResolver = FeedError.badStatus(500)
        let store = store(api)

        store.approve(pedido, in: sessao([pedido]))
        try await esperar { !store.pendingRequestIDs.contains(self.pedido.id) }

        XCTAssertFalse(
            store.pedidoFoiResolvido(pedido.id),
            "o servidor não aceitou: o pedido NÃO pode sair da pilha")
        XCTAssertEqual(store.falhaPorPedido[pedido.id], "o servidor respondeu 500")
    }

    func testRejeitarMandaDecline() async throws {
        let api = APIDuplo()
        let store = store(api)

        store.reject(pedido, in: sessao([pedido]))
        try await esperar { !store.pendingRequestIDs.contains(self.pedido.id) }

        XCTAssertEqual(api.resolvidos.first?.decisao, .recusar)
    }

    func testPlaceholderNaoVaiParaOServidor() async throws {
        let api = APIDuplo()
        let store = store(api)
        let placeholder = AttentionRequest(
            id: "conv_1-pedido-1", title: "Aprovação necessária",
            question: "…", isResolvable: false)

        store.approve(placeholder, in: sessao([placeholder]))

        XCTAssertTrue(api.resolvidos.isEmpty, "id fabricado nunca vai para o servidor")
        XCTAssertFalse(store.pedidoFoiResolvido(placeholder.id))
        XCTAssertNotNil(store.falhaPorPedido[placeholder.id], "a pessoa precisa saber por quê")
    }

    func testAprovarTudoPulaOsPlaceholders() async throws {
        let api = APIDuplo()
        let store = store(api)
        let placeholder = AttentionRequest(
            id: "conv_1-pedido-1", title: "Aprovação necessária",
            question: "…", isResolvable: false)

        store.approveAll(in: sessao([pedido, placeholder]))
        try await esperar { store.pendingRequestIDs.isEmpty }

        XCTAssertEqual(api.resolvidos.map(\.pedido), ["elic_1"])
    }

    func testNoModoFixtureNadaVaiParaARede() async throws {
        let api = APIDuplo()
        let store = HUDStore(client: api)
        store.feedSource = .mock

        store.approve(pedido, in: sessao([pedido]))

        XCTAssertTrue(api.resolvidos.isEmpty, "cenário de demonstração não fala com servidor")
        XCTAssertTrue(store.pedidoFoiResolvido(pedido.id), "mas some da pilha, como antes")
    }

    /// Espera uma condição virar verdadeira, com prazo — as decisões são
    /// assíncronas e um `sleep` fixo tornaria o teste lento ou instável.
    private func esperar(
        _ prazo: TimeInterval = 5, _ condicao: @MainActor () -> Bool
    ) async throws {
        let limite = Date().addingTimeInterval(prazo)
        while !condicao() {
            if Date() > limite { XCTFail("condição não ocorreu em \(prazo)s"); return }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
    }
}
