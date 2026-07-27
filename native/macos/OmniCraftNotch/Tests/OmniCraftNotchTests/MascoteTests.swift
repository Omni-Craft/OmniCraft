import XCTest

@testable import OmniCraftNotch

/// Quando o Fucho aparece.
///
/// Ele sumia sempre que a lista ficava vazia — o que virou o estado comum
/// depois que sessões antigas passaram a ser lidas como ociosas em vez de
/// desconhecidas. Esconder o pet no estado mais frequente tira o rosto da ilha;
/// ele só some quando não há feed nenhum para representar.
@MainActor
final class MascoteTests: XCTestCase {
    private func store(_ counts: CountsSummary, _ sessoes: [AgentSession] = []) -> HUDStore {
        let store = HUDStore(client: FeedClient())
        store.aplicarSnapshotParaTeste(FeedSnapshot(counts: counts, sessions: sessoes))
        return store
    }

    func testMaquinaParadaMantemOPetNaTela() {
        XCTAssertEqual(store(.exact(active: 0, waiting: 0)).estadoMascote, .ocioso)
    }

    func testSemFeedNaoHaOQueRepresentar() {
        XCTAssertEqual(store(.unavailable).estadoMascote, .oculto)
        XCTAssertEqual(store(.disconnected).estadoMascote, .oculto)
    }

    func testSessaoRodandoAindaPoeOPetParaTrabalhar() {
        let rodando = AgentSession(id: "s1", title: "s1", state: .emExecucao)
        XCTAssertEqual(store(.exact(active: 1, waiting: 0), [rodando]).estadoMascote, .trabalhando)
    }
}
