import XCTest

@testable import OmniCraftWidgets

/// O que o feed vira na tela.
///
/// As regras testadas aqui são as que separam "não sei" de "nada acontecendo":
/// um estado ilegível não pode virar "ocioso", um pedido pendente vence o
/// status que vier, e gasto sem teto não pode virar barra.
final class FeedMapperTests: XCTestCase {
    private func item(_ json: String) throws -> MonitorSessionItemDTO {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(MonitorSessionItemDTO.self, from: Data(json.utf8))
    }

    func testPedidoPendenteVenceOStatus() throws {
        // O status ainda diz "running" enquanto o pedido espera por você.
        let dto = try item(
            """
            {"session_id": "s1", "status": "running",
             "pending_elicitation": {"id": "e1", "summary": "Rodar git push?"}}
            """)
        let sessao = FeedMapper.mapSession(dto)
        XCTAssertEqual(sessao.ref.estado, .aguardandoVoce)
        XCTAssertEqual(sessao.ref.motivoAtencao, "Rodar git push?")
        XCTAssertEqual(ColunaBoard.coluna(para: sessao.ref.estado), .atencao)
    }

    func testEstadoIlegivelViraDesconhecidoNuncaOcioso() throws {
        let dto = try item(
            """
            {"session_id": "s1", "status": "idle", "degraded": ["status_unknown"]}
            """)
        let sessao = FeedMapper.mapSession(dto)
        XCTAssertEqual(sessao.ref.estado, .desconhecido)
        XCTAssertNotNil(sessao.ref.motivoAtencao, "desconhecido precisa dizer por quê")
    }

    func testStatusDesconhecidoDoServidorNaoViraOcioso() throws {
        // Um status que este build não conhece é incerteza, não silêncio.
        let sessao = FeedMapper.mapSession(try item(#"{"session_id": "s1", "status": "compacting"}"#))
        XCTAssertEqual(sessao.ref.estado, .desconhecido)
    }

    func testGastoSemTetoNaoViraFracao() throws {
        let dto = try item(#"{"session_id": "s1", "status": "running", "cost_usd": 1.5}"#)
        let uso = try XCTUnwrap(FeedMapper.mapSession(dto).uso)
        XCTAssertEqual(uso.gastoUSD, 1.5)
        XCTAssertNil(uso.fracao, "sem teto declarado não há barra legítima")
    }

    func testGastoIlegivelViraDesconhecidoNuncaZero() throws {
        let dto = try item(
            """
            {"session_id": "s1", "status": "running", "cost_usd": 1.5,
             "degraded": ["cost_unreadable"]}
            """)
        XCTAssertNil(FeedMapper.mapSession(dto).uso)
    }

    func testDetalheDaSessaoFicaVazioEmVezDeInventado() throws {
        // O feed não traz o interior da sessão; preencher seria mentir.
        let sessao = FeedMapper.mapSession(try item(#"{"session_id": "s1", "status": "running"}"#))
        XCTAssertTrue(sessao.transcript.isEmpty)
        XCTAssertTrue(sessao.ferramentas.isEmpty)
        XCTAssertTrue(sessao.subagentes.isEmpty)
        XCTAssertTrue(sessao.tarefas.isEmpty)
    }

    func testSessaoMalformadaNaoDerrubaOFeedInteiro() throws {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let dto = try decoder.decode(
            MonitorFeedDTO.self,
            from: Data(
                """
                {"counts": {"active": 1, "awaiting": 0, "partial": true},
                 "sessions": [{"session_id": "boa", "status": "running"}, 42]}
                """.utf8))
        let snapshot = FeedMapper.snapshot(from: dto)
        XCTAssertEqual(snapshot.sessoes.map(\.id), ["boa"])
        XCTAssertTrue(snapshot.contagensSaoPiso, "partial → a contagem é piso, não exata")
    }

    func testJanelaDeLimiteIlegivelNaoViraBarra() throws {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let dto = try decoder.decode(
            MonitorFeedDTO.self,
            from: Data(#"{"limit_windows": [{"label": "5 h", "resets_in": "2 h 05"}]}"#.utf8))
        let janela = try XCTUnwrap(FeedMapper.snapshot(from: dto).janelasLimite.first)
        XCTAssertEqual(janela.rotulo, "5 h")
        XCTAssertNil(janela.fracaoUsada)
    }
}
