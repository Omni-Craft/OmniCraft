import SwiftUI

/// Raiz do HUD: alterna pill ↔ ilha com mola (fade no Reduce Motion)
/// e reporta o tamanho para o painel se re-ancorar no topo-centro.
struct NotchHUDView: View {
    @Environment(HUDStore.self) private var store
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var onSizeChange: (CGSize) -> Void

    private var transitionAnimation: Animation {
        reduceMotion ? .easeOut(duration: 0.2) : .spring(response: 0.35, dampingFraction: 0.8)
    }

    /// Tarefa pendente de hover (abrir com intenção; fechar com folga).
    @State private var tarefaHover: Task<Void, Never>?

    var body: some View {
        Group {
            if store.pillVisible && store.modo != .soBarraDeMenus {
                if store.isExpanded {
                    ExpandedIslandView()
                        .transition(.opacity.combined(with: reduceMotion ? .identity : .scale(scale: 0.95, anchor: .top)))
                } else {
                    CollapsedPillView()
                        .transition(.opacity.combined(with: reduceMotion ? .identity : .scale(scale: 0.95, anchor: .top)))
                }
            } else {
                // Invisível, mas mantém o painel com 1pt para não sumir do window server.
                Color.clear.frame(width: 1, height: 1)
            }
        }
        .onHover { dentro in
            tarefaHover?.cancel()
            if dentro {
                guard !store.isExpanded else { return }
                // Pequeno delay = intenção, não passagem do mouse pelo topo da tela.
                tarefaHover = Task { @MainActor in
                    try? await Task.sleep(for: .milliseconds(280))
                    guard !Task.isCancelled else { return }
                    store.expand(porHover: true)
                }
            } else {
                // Folga antes de recolher: só fecha o que o hover abriu.
                tarefaHover = Task { @MainActor in
                    try? await Task.sleep(for: .milliseconds(380))
                    guard !Task.isCancelled else { return }
                    store.colapsarPorSaidaDoHover()
                }
            }
        }
        .environment(\.colorScheme, .dark)   // a ilha é sempre preta, como a notch
        .padding(.horizontal, 16)
        .padding(.bottom, 24) // folga para a sombra respirar
        .animation(transitionAnimation, value: store.isExpanded)
        .animation(transitionAnimation, value: store.pillVisible)
        .fixedSize()
        .background(SizeReporter(onChange: onSizeChange))
    }
}

/// Mede o conteúdo e avisa o painel a cada frame da animação.
private struct SizeReporter: View {
    var onChange: (CGSize) -> Void

    var body: some View {
        GeometryReader { geo in
            Color.clear
                .preference(key: SizeKey.self, value: geo.size)
        }
        .onPreferenceChange(SizeKey.self, perform: onChange)
    }

    private struct SizeKey: PreferenceKey {
        static var defaultValue: CGSize = .zero
        static func reduce(value: inout CGSize, nextValue: () -> CGSize) {
            value = nextValue()
        }
    }
}

/// Fundo da ilha: preto sólido fundido à notch — topo reto colado na borda da
/// tela, só os cantos INFERIORES arredondados.
struct IslandBackground: ViewModifier {
    var bottomRadius: CGFloat

    private var shape: UnevenRoundedRectangle {
        UnevenRoundedRectangle(
            topLeadingRadius: 0, bottomLeadingRadius: bottomRadius,
            bottomTrailingRadius: bottomRadius, topTrailingRadius: 0,
            style: .continuous
        )
    }

    func body(content: Content) -> some View {
        content
            .background(Color.black, in: shape)
            .shadow(color: .black.opacity(0.45), radius: 14, y: 6)
    }
}

extension View {
    func islandBackground(bottomRadius: CGFloat) -> some View {
        modifier(IslandBackground(bottomRadius: bottomRadius))
    }
}
