import SwiftUI
import OmniCraftPets

/// Estado colapsado: barra preta fundida à notch. O conteúdo vive nas LATERAIS
/// do recorte da câmera (resumo à esquerda, chevron à direita).
struct CollapsedPillView: View {
    @Environment(HUDStore.self) private var store
    @State private var isHovering = false

    private let metrics = NotchMetrics.current()

    /// Vão central reservado para a câmera (em Mac sem notch, só um respiro).
    private var cameraGap: CGFloat { metrics.hasNotch ? metrics.width + 20 : 24 }

    var body: some View {
        Button {
            store.toggleExpanded()
        } label: {
            HStack(spacing: 0) {
                HStack(spacing: 7) {
                    if store.estadoMascote != .oculto {
                        MascoteView(estado: store.estadoMascote, pet: store.pet,
                                    altura: metrics.height + 6, ritmo: store.ritmoPet)
                            .transition(.opacity)
                    }
                    statusIcon
                    Text(store.snapshot.counts.pillText)
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(store.snapshot.counts.isUnavailable ? .secondary : .primary)
                        .lineLimit(1)
                        .fixedSize()
                }
                .padding(.leading, 18)

                Spacer(minLength: cameraGap)

                Image(systemName: "chevron.right")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(.secondary)
                    .padding(.trailing, 16)
            }
            // Um pouco mais alta que a notch: a borda inferior aparece sob a câmera.
            .frame(height: metrics.height + 8)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .islandBackground(bottomRadius: 14)
        .brightness(isHovering ? 0.15 : 0)   // hover sem escalar: não quebra a fusão com a notch
        .onHover { isHovering = $0 }
        .animation(.easeOut(duration: 0.15), value: isHovering)
        .animation(.easeOut(duration: 0.2), value: store.estadoMascote)
        .focusable()
        .accessibilityLabel(accessibilityText)
        .accessibilityHint("Expande a lista de sessões de agentes")
    }

    /// Ícone + texto — nunca só cor.
    @ViewBuilder
    private var statusIcon: some View {
        if store.snapshot.counts.isUnavailable {
            Image(systemName: "questionmark.circle")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(.secondary)
        } else if store.hasAttention {
            Image(systemName: "bell.fill")
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(.orange)
        } else {
            Image(systemName: "circle.dotted.circle")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(.green)
        }
    }

    private var accessibilityText: String {
        let base = store.snapshot.counts.isUnavailable
            ? "OmniCraft: contagens indisponíveis"
            : "OmniCraft: \(store.snapshot.counts.pillText)"
        // O mascote verde é informação nova: quem usa leitor de tela também recebe.
        return store.estadoMascote == .concluido
            ? base + ", concluído desde a última vez que você olhou"
            : base
    }
}
