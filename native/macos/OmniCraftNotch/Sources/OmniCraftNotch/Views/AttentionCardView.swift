import SwiftUI

/// Card de atenção da FILA GLOBAL: um pedido por vez, de qualquer sessão,
/// com navegação ‹ i de N › atravessando todas as sessões.
struct AttentionCardView: View {
    @Environment(HUDStore.self) private var store
    let sessao: AgentSession
    let pedido: AttentionRequest

    private var fila: [(sessao: AgentSession, pedido: AttentionRequest)] {
        store.pedidosPendentes
    }

    private var indice: Int {
        min(max(store.indicePedido, 0), max(fila.count - 1, 0))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Image(systemName: "bell.fill")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.orange)
                Text(sessao.title)
                    .font(.system(size: 12, weight: .semibold))
                    .lineLimit(1)
                Spacer(minLength: 8)
                HStack(spacing: 4) {
                    Image(systemName: sessao.state.symbolName)
                        .font(.system(size: 9, weight: .semibold))
                    Text(sessao.state.label)
                        .font(.system(size: 10.5, weight: .medium))
                        .fixedSize()
                }
                .foregroundStyle(.orange)
            }

            Text(sessao.metadataLine)
                .font(.system(size: 10.5))
                .foregroundStyle(.secondary)
                .lineLimit(1)

            requestBlock(pedido)
        }
        .padding(12)
        .background(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .fill(Color.orange.opacity(0.12))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .strokeBorder(Color.orange.opacity(0.55), lineWidth: 1.5)
        )
        .accessibilityElement(children: .contain)
        .accessibilityLabel(
            "Pedido \(indice + 1) de \(fila.count): sessão \(sessao.title) aguardando você")
    }

    private func requestBlock(_ request: AttentionRequest) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Image(systemName: "questionmark.circle.fill")
                    .font(.system(size: 11))
                    .foregroundStyle(.orange)
                VStack(alignment: .leading, spacing: 2) {
                    Text(request.title)
                        .font(.system(size: 11, weight: .semibold))
                    Text(request.question)
                        .font(.system(size: 11))
                        .foregroundStyle(.primary.opacity(0.85))
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
            }

            HStack(spacing: 8) {
                Button("✓ Aprovar") { store.approve(request, in: sessao) }
                    .buttonStyle(AttentionButtonStyle(kind: .primary))
                    .accessibilityLabel("Aprovar: \(request.question)")

                Button("✓ Aprovar tudo nesta sessão") { store.approveAll(in: sessao) }
                    .buttonStyle(AttentionButtonStyle(kind: .secondary))
                    .accessibilityLabel("Aprovar todos os pedidos da sessão \(sessao.title)")

                Button("✕ Rejeitar") { store.reject(request, in: sessao) }
                    .buttonStyle(AttentionButtonStyle(kind: .secondary))
                    .accessibilityLabel("Rejeitar: \(request.question)")
            }

            if fila.count > 1 {
                pager
            }
        }
        .padding(.leading, 2)
    }

    /// Navegação ‹ 1 de N › pela fila global (todas as sessões).
    private var pager: some View {
        HStack(spacing: 6) {
            Button {
                store.indicePedido = max(indice - 1, 0)
            } label: {
                Image(systemName: "chevron.left")
                    .font(.system(size: 9, weight: .bold))
                    .padding(5)
                    .contentShape(Circle())
            }
            .buttonStyle(HoverCircleButtonStyle())
            .disabled(indice == 0)
            .focusable()
            .accessibilityLabel("Pedido anterior")

            Text("\(indice + 1) de \(fila.count)")
                .font(.system(size: 10, weight: .medium).monospacedDigit())
                .foregroundStyle(.secondary)

            Button {
                store.indicePedido = min(indice + 1, fila.count - 1)
            } label: {
                Image(systemName: "chevron.right")
                    .font(.system(size: 9, weight: .bold))
                    .padding(5)
                    .contentShape(Circle())
            }
            .buttonStyle(HoverCircleButtonStyle())
            .disabled(indice == fila.count - 1)
            .focusable()
            .accessibilityLabel("Próximo pedido")
        }
        .frame(maxWidth: .infinity, alignment: .center)
    }
}

/// Botões do card: Aprovar preenchido (primário); os demais, secundários.
struct AttentionButtonStyle: ButtonStyle {
    enum Kind { case primary, secondary }
    let kind: Kind
    @State private var isHovering = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 10.5, weight: .semibold))
            .lineLimit(1)
            .fixedSize()
            .padding(.horizontal, 10)
            .padding(.vertical, 5)
            .background(background)
            .foregroundStyle(kind == .primary ? .white : .primary)
            .clipShape(Capsule())
            .overlay(
                Capsule().strokeBorder(
                    kind == .secondary ? Color.primary.opacity(0.2) : .clear, lineWidth: 1)
            )
            .opacity(configuration.isPressed ? 0.7 : 1)
            .scaleEffect(configuration.isPressed ? 0.97 : (isHovering ? 1.02 : 1))
            .onHover { isHovering = $0 }
            .animation(.easeOut(duration: 0.12), value: isHovering)
            .contentShape(Capsule())
    }

    private var background: some ShapeStyle {
        switch kind {
        case .primary: AnyShapeStyle(Color.orange.opacity(isHovering ? 1.0 : 0.9))
        case .secondary: AnyShapeStyle(Color.primary.opacity(isHovering ? 0.12 : 0.06))
        }
    }
}
