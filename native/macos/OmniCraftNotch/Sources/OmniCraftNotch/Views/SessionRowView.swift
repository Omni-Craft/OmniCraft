import SwiftUI

/// Linha de sessão: título, estado (ícone + texto), metadados e uso.
struct SessionRowView: View {
    let session: AgentSession
    @State private var isHovering = false

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline) {
                Text(session.title)
                    .font(.system(size: 12, weight: .medium))
                    .lineLimit(1)
                Spacer(minLength: 8)
                stateBadge
            }

            Text(session.metadataLine)
                .font(.system(size: 10.5))
                .foregroundStyle(.secondary)
                .lineLimit(1)

            // O que roda AGORA + tamanho do diff (só quando conhecidos — nunca 0).
            if session.ferramentaAtual != nil || session.diffTexto != nil {
                HStack(spacing: 6) {
                    if let ferramenta = session.ferramentaAtual {
                        HStack(spacing: 4) {
                            Image(systemName: "gearshape.2")
                                .font(.system(size: 8))
                            Text(ferramenta)
                                .lineLimit(1)
                                .truncationMode(.middle)
                        }
                    }
                    if let diff = session.diffTexto {
                        Text(diff)
                            .foregroundStyle(session.diffMais ?? 0 > 0 ? .green : .secondary)
                    }
                }
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(.secondary)
                .accessibilityLabel(
                    [session.ferramentaAtual.map { "executando \($0)" },
                     session.diffTexto.map { "diff \($0)" }]
                        .compactMap { $0 }.joined(separator: ", "))
            }

            UsageGaugeView(usage: session.usage)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(.primary.opacity(isHovering ? 0.06 : 0))
        )
        .onHover { isHovering = $0 }
        .animation(.easeOut(duration: 0.12), value: isHovering)
        .focusable()
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(session.title), \(session.state.label). \(session.metadataLine)")
    }

    private var stateBadge: some View {
        HStack(spacing: 4) {
            Image(systemName: session.state.symbolName)
                .font(.system(size: 9, weight: .semibold))
            Text(session.state.label)
                .font(.system(size: 10.5, weight: .medium))
                .fixedSize()
        }
        .foregroundStyle(stateColor)
    }

    private var stateColor: Color {
        switch session.state {
        case .emExecucao: .green
        case .aguardandoVoce: .orange
        case .ocioso: .secondary
        case .falhou: .red
        case .desconhecido: .secondary
        }
    }
}
