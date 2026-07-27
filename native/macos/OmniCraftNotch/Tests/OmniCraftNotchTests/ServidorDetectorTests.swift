import XCTest

@testable import OmniCraftNotch

/// A leitura de quem está escutando.
///
/// A aba mostrava fixtures — portas que ninguém abriu, com um botão de parar ao
/// lado. O que se testa aqui é o parsing, que é onde dá para errar em silêncio:
/// uma porta lida errado vira um endereço que não existe, e o botão volta a
/// mentir.
final class ServidorDetectorTests: XCTestCase {
    private let detector = ServidorDetector()

    func testLeCadaOuvinteDaSaidaDoLsof() {
        // O formato -F: `p` abre o processo, `c` nomeia, `n` é cada endereço.
        let saida = """
            p4242
            cnode
            n*:5173
            n127.0.0.1:24678
            p777
            cpython3.11
            n[::1]:8000
            """
        XCTAssertEqual(
            detector.analisar(saida),
            [
                Ouvinte(pid: 4242, comando: "node", porta: 5173),
                Ouvinte(pid: 4242, comando: "node", porta: 24678),
                Ouvinte(pid: 777, comando: "python3.11", porta: 8000),
            ])
    }

    func testEnderecoIPv6NaoConfundeAPorta() {
        // `[::1]:3000` tem dois-pontos de sobra; a porta é a última parte.
        let ouvintes = detector.analisar("p1\ncnode\nn[::1]:3000")
        XCTAssertEqual(ouvintes.first?.porta, 3000)
    }

    func testIgnoraPortaPrivilegiadaEEfemera() {
        // 22 é serviço do sistema; 55000 é o lado de saída de uma conexão.
        let ouvintes = detector.analisar("p1\ncsshd\nn*:22\np2\ncalgo\nn*:55000")
        XCTAssertTrue(ouvintes.isEmpty)
    }

    func testLinhaSemProcessoNaoViraOuvinteOrfao() {
        // Um `n` antes de qualquer `p` não pertence a ninguém.
        XCTAssertTrue(detector.analisar("n*:5173").isEmpty)
    }

    func testLeOsTresFormatosDeTempoDoPs() {
        XCTAssertEqual(ServidorDetector.segundos(deEtime: "05:30"), 330)
        XCTAssertEqual(ServidorDetector.segundos(deEtime: "01:05:30"), 3930)
        XCTAssertEqual(ServidorDetector.segundos(deEtime: "2-01:05:30"), 176_730)
        XCTAssertNil(ServidorDetector.segundos(deEtime: "?"))
    }

    func testDaemonDoSistemaNaoGanhaProjetoInventado() throws {
        // Quem roda a partir de "/" não tem projeto; "—" diz mais que "/".
        let servidores = try XCTUnwrap(ServidorDetector().detectar())
        XCTAssertFalse(servidores.contains { $0.projeto == "/" })
    }

    /// A prova de que a lista é desta máquina: toda porta que ele reporta está
    /// mesmo aceitando conexão agora.
    func testTodaPortaReportadaEstaMesmoEscutando() throws {
        let servidores = try XCTUnwrap(detector.detectar(), "lsof não respondeu")
        for servidor in servidores.prefix(8) {
            let porta = try XCTUnwrap(Int(servidor.host.split(separator: ":").last ?? ""))
            XCTAssertTrue(
                Self.aceitaConexao(porta: porta),
                "\(servidor.host) foi listado mas ninguém atende nele")
        }
    }

    private static func aceitaConexao(porta: Int) -> Bool {
        let sock = socket(AF_INET, SOCK_STREAM, 0)
        guard sock >= 0 else { return false }
        defer { close(sock) }
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = UInt16(porta).bigEndian
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        let ok = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                connect(sock, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) == 0
            }
        }
        return ok
    }
}
