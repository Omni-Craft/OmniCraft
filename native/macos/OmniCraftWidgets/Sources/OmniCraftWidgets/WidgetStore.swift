import SwiftUI
import OmniCraftPets
import Observation

/// Estado central dos widgets. Só fixtures + timers de simulação — nada de rede.
@MainActor
@Observable
final class WidgetStore {
    var cenario: CenarioWidgets = .streaming {
        didSet { aplicarCenario() }
    }

    /// Os widgets (menos o board) partem da sessão selecionada.
    var sessaoSelecionadaID: String?

    private(set) var snapshot: SnapshotWidgets = MockFeed.snapshot(for: .streaming)
    private(set) var actionLog: [String] = []

    private var timer: Timer?

    init() {
        aplicarCenario()
    }

    var sessaoSelecionada: SessaoDetalhe? {
        snapshot.sessoes.first { $0.id == sessaoSelecionadaID } ?? snapshot.sessoes.first
    }

    var projetoAtual: String {
        sessaoSelecionada?.ref.projeto ?? "global"
    }

    // Utilidades locais (widgets Servidores/Rotas; copiar usa o clipboard real)
    let servidores = MockFeed.servidores
    let rotas = MockFeed.rotas

    /// Colunas do board com "mostrar todas" individual.
    var colunasExpandidas: Set<String> = []

    // MARK: Mascote (mesmo pet do notch, pacote OmniCraftPets)

    /// Qual pet aparece no board.
    var pet: Pet = .fucho

    /// Velocidade da animação — o padrão desacelera o manifesto.
    var ritmoPet: RitmoPet = .ameno

    /// Traduz o estado das sessões para o que o bicho deve fazer.
    /// Mesma prioridade do notch: o que exige você primeiro.
    var estadoMascote: EstadoMascote {
        let sessoes = snapshot.sessoes.map(\.ref)
        if sessoes.contains(where: { $0.estado == .aguardandoVoce }) { return .atencao }
        if sessoes.contains(where: { $0.estado == .falhou }) { return .erro }
        if sessoes.contains(where: { $0.estado == .emExecucao }) { return .trabalhando }
        if sessoes.contains(where: { $0.estado == .concluida }) { return .concluido }
        return sessoes.isEmpty ? .oculto : .ocioso
    }

    func copiar(_ texto: String, rotulo: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(texto, forType: .string)
        registrar("⧉ Copiado \(rotulo): \(texto)")
    }

    func acaoServidor(_ servidor: ServidorLocal, _ acao: String) {
        registrar("⚙ Servidor \(servidor.nome): \(acao) (visual)")
    }

    func abrirRota(_ rota: RotaLocal) {
        registrar("→ Rota: \(rota.rotulo) (visual)")
    }

    func registrar(_ acao: String) {
        actionLog.append(acao)
        print("[OmniCraftWidgets] \(acao)")
    }

    // MARK: - Cenário e simulações

    private func aplicarCenario() {
        timer?.invalidate()
        timer = nil
        snapshot = MockFeed.snapshot(for: cenario)
        sessaoSelecionadaID = snapshot.sessoes.first?.id

        switch cenario {
        case .streaming: iniciarStreaming()
        case .boardMigrando: iniciarMigracao()
        default: break
        }
    }

    /// Simula a última mensagem chegando em pedaços (fixture, sem rede).
    private func iniciarStreaming() {
        let pedacos = [
            " cada retry herda", " o tempo do caso anterior.", " Vou resetar o mock",
            " no beforeEach", " e subir o timeout do CI para 15 s.",
        ]
        var indice = 0
        timer = Timer.scheduledTimer(withTimeInterval: 1.4, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self, self.cenario == .streaming,
                      var sessao = self.snapshot.sessoes.first,
                      let ultima = sessao.transcript.indices.last,
                      case var .texto(texto) = sessao.transcript[ultima].conteudo else { return }
                if indice < pedacos.count {
                    texto += pedacos[indice]
                    indice += 1
                    sessao.transcript[ultima].conteudo = .texto(texto)
                    sessao.transcript[ultima].emStreaming = indice < pedacos.count
                } else {
                    // Reinicia o ciclo para a demo continuar viva.
                    indice = 0
                    sessao.transcript[ultima].conteudo =
                        .texto("Encontrei: o mock do relógio não é resetado entre os casos, então")
                    sessao.transcript[ultima].emStreaming = true
                }
                self.snapshot.sessoes[0] = sessao
            }
        }
    }

    /// Alterna o estado de uma sessão do board para exercitar a animação de migração.
    private func iniciarMigracao() {
        timer = Timer.scheduledTimer(withTimeInterval: 3.5, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self, self.cenario == .boardMigrando,
                      let indice = self.snapshot.sessoes.firstIndex(where: { $0.id == "m-migra" })
                else { return }
                var ref = self.snapshot.sessoes[indice].ref
                if ref.estado == .emExecucao {
                    ref.estado = .aguardandoVoce
                    ref.motivoAtencao = "Permitir rodar `git push` no repositório?"
                } else {
                    ref.estado = .emExecucao
                    ref.motivoAtencao = nil
                }
                self.snapshot.sessoes[indice].ref = ref
            }
        }
    }
}
