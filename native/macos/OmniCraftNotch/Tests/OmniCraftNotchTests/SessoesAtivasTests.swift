import XCTest

@testable import OmniCraftNotch

/// O que a ilha mostra sem você pedir.
///
/// A lista aberta virou uma parede de sessões de dias atrás, e o que estava
/// acontecendo agora ficava enterrado. O filtro corta pelo que PODE estar
/// rodando, não pelo rótulo: incerteza continua visível enquanto houver runner
/// para rodar, e nada é descartado — só sai da primeira tela.
final class SessoesAtivasTests: XCTestCase {
    private func sessao(
        _ id: String,
        _ estado: SessionState,
        runner: Bool? = true,
        pedidos: [AttentionRequest] = []
    ) -> AgentSession {
        AgentSession(id: id, title: id, state: estado, runnerOnline: runner, requests: pedidos)
    }

    /// O que a lista mostraria fechada, e o que continua no dado.
    private func ativas(_ sessoes: [AgentSession]) -> [String] {
        sessoes.filter(\.podeEstarRodando).map(\.id)
    }

    func testMostraOQueEstaRodandoEOQueEsperaPorVoce() {
        let sessoes = [
            sessao("rodando", .emExecucao),
            sessao(
                "esperando", .aguardandoVoce,
                pedidos: [AttentionRequest(id: "p1", title: "Aprovação", question: "?")]),
        ]
        XCTAssertEqual(Set(ativas(sessoes)), ["rodando", "esperando"])
    }

    func testEsconderOciosaEFalhaDaPrimeiraTela() {
        let sessoes = [
            sessao("rodando", .emExecucao),
            sessao("ociosa", .ocioso),
            sessao("falhou", .falhou),
        ]
        XCTAssertEqual(ativas(sessoes), ["rodando"])
        XCTAssertEqual(sessoes.count, 3, "nenhuma sessão é descartada do dado")
    }

    func testDesconhecidaComRunnerDePeContinuaVisivel() {
        // Pode estar rodando; a regra é que incerteza não vira silêncio.
        XCTAssertEqual(ativas([sessao("incerta", .desconhecido, runner: true)]), ["incerta"])
    }

    func testDesconhecidaSemRunnerNaoEstaRodando() {
        // Sem runner nada executa — isso é fato, não palpite.
        let velha = sessao("velha", .desconhecido, runner: false)
        XCTAssertTrue(ativas([velha]).isEmpty)
        XCTAssertEqual(velha.state, .desconhecido, "o estado não é reescrito para escondê-la")
    }

    func testRunnerIlegivelNaoEscondeASessao() {
        // `nil` é "não deu para ler", e não deu para ler não é "não está rodando".
        XCTAssertEqual(ativas([sessao("incerta", .desconhecido, runner: nil)]), ["incerta"])
    }
}
