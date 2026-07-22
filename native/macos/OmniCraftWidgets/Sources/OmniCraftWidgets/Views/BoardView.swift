import SwiftUI

/// Board global: três colunas DERIVADAS do estado (nada de arrastar/arquivar à
/// mão). A migração entre colunas é animada (mola; fade com Reduce Motion).
struct BoardView: View {
    @Environment(WidgetStore.self) private var store
    @Environment(\.accessibilityReduceMotion) private var reduzirMovimento
    @Namespace private var ns

    var body: some View {
        if store.snapshot.sessoes.isEmpty {
            VazioView(icone: "square.grid.3x1.below.line.grid.1x2",
                      texto: "Nenhuma sessão em nenhum projeto")
        } else {
            HStack(alignment: .top, spacing: 10) {
                ForEach(ColunaBoard.allCases) { coluna in
                    colunaView(coluna)
                }
            }
            .padding(12)
            .animation(
                reduzirMovimento ? .easeOut(duration: 0.2)
                                 : .spring(response: 0.4, dampingFraction: 1.0),
                value: assinaturaEstados)
        }
    }

    /// Muda quando qualquer sessão troca de coluna → dispara a animação.
    private var assinaturaEstados: String {
        store.snapshot.sessoes.map { "\($0.id):\($0.ref.estado.rawValue)" }.joined()
    }

    private func sessoes(em coluna: ColunaBoard) -> [SessaoRef] {
        store.snapshot.sessoes.map(\.ref).filter {
            ColunaBoard.coluna(para: $0.estado) == coluna
        }
    }

    private func colunaView(_ coluna: ColunaBoard) -> some View {
        let refs = sessoes(em: coluna)
        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Text(coluna.titulo)
                    .font(.system(size: 10, weight: .bold, design: .monospaced))
                    .tracking(1)
                    .textCase(.uppercase)
                    .foregroundStyle(corColuna(coluna))
                Text(Fmt.contagem(refs.count, piso: store.snapshot.contagensSaoPiso))
                    .font(.system(size: 10, weight: .bold, design: .monospaced))
                    .foregroundStyle(.secondary)
                Spacer(minLength: 0)
            }
            .accessibilityElement(children: .combine)
            .accessibilityLabel(
                "Coluna \(coluna.titulo): \(Fmt.contagem(refs.count, piso: store.snapshot.contagensSaoPiso)) sessões")

            ScrollView(showsIndicators: false) {
                VStack(spacing: 8) {
                    // Coluna cheia colapsa em 4 + "mostrar todas" (lição do VibeIsland).
                    let expandida = store.colunasExpandidas.contains(coluna.rawValue)
                    let visiveis = expandida ? refs : Array(refs.prefix(4))
                    ForEach(visiveis) { ref in
                        cartao(ref)
                            .matchedGeometryEffect(id: ref.id, in: ns)
                    }
                    if refs.count > 4 {
                        Button {
                            if expandida { store.colunasExpandidas.remove(coluna.rawValue) }
                            else { store.colunasExpandidas.insert(coluna.rawValue) }
                        } label: {
                            Text(expandida ? "mostrar menos" : "mostrar todas as \(refs.count)")
                                .font(.system(size: 9.5, weight: .semibold, design: .monospaced))
                                .foregroundStyle(.secondary)
                                .frame(maxWidth: .infinity)
                                .padding(.vertical, 4)
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .focusable()
                        .accessibilityLabel(expandida
                            ? "Mostrar menos sessões da coluna \(coluna.titulo)"
                            : "Mostrar todas as \(refs.count) sessões da coluna \(coluna.titulo)")
                    }
                    if refs.isEmpty {
                        Text("vazio")
                            .font(.system(size: 10.5))
                            .foregroundStyle(.tertiary)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 16)
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
    }

    private func cartao(_ ref: SessaoRef) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(ref.titulo)
                .font(.system(size: 11, weight: .bold, design: .monospaced))
                .lineLimit(2)
                .truncationMode(.tail)
            Text("\(ref.projeto) · \(ref.agente)")
                .font(.system(size: 9.5, design: .monospaced))
                .foregroundStyle(.secondary)
                .lineLimit(1)
            HStack(spacing: 6) {
                BadgeEstado(ref.estado)
                Spacer(minLength: 0)
                if let tempo = ref.haQuantoTempo {
                    Text(tempo)
                        .font(.system(size: 9, design: .monospaced))
                        .foregroundStyle(.tertiary)
                }
            }
            if let subestado = ref.subestado {
                HStack(spacing: 4) {
                    Image(systemName: "ellipsis.circle")
                        .font(.system(size: 8))
                    Text(subestado)
                        .lineLimit(1)
                }
                .font(.system(size: 9.5, design: .monospaced))
                .foregroundStyle(.secondary)
                .accessibilityLabel("Agora: \(subestado)")
            }
            if let motivo = ref.motivoAtencao {
                HStack(alignment: .firstTextBaseline, spacing: 4) {
                    Image(systemName: "questionmark.circle.fill")
                        .font(.system(size: 9))
                        .foregroundStyle(.orange)
                    Text(motivo)
                        .font(.system(size: 9.5, design: .monospaced))
                        .foregroundStyle(.orange)
                        .lineLimit(2)
                }
            }
        }
        .padding(8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 9, style: .continuous)
                .fill(ref.motivoAtencao != nil ? Color.orange.opacity(0.10) : Paleta.cartao)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .strokeBorder(ref.motivoAtencao != nil ? Color.orange.opacity(0.5) : .clear,
                              lineWidth: 1)
        )
        .accessibilityElement(children: .combine)
        .accessibilityLabel(
            "\(ref.titulo), projeto \(ref.projeto), agente \(ref.agente), \(ref.estado.label)"
            + (ref.motivoAtencao.map { ". Motivo: \($0)" } ?? ""))
    }

    private func corColuna(_ coluna: ColunaBoard) -> Color {
        switch coluna {
        case .ativas: .green
        case .atencao: .orange
        case .concluidas: .secondary
        }
    }
}
