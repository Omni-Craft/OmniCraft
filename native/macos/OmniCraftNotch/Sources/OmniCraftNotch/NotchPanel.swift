import AppKit
import SwiftUI

/// NSPanel que pode invadir a área da barra de menus/notch: sem o override de
/// `constrainFrameRect`, o AppKit empurra a janela para baixo do menu bar.
private final class NotchPanelWindow: NSPanel {
    override func constrainFrameRect(_ frameRect: NSRect, to screen: NSScreen?) -> NSRect {
        frameRect
    }
}

/// Hosting view que aceita o PRIMEIRO clique mesmo com o app inativo — sem isso,
/// num painel não-ativante o AppKit consome o clique inicial (click-through) e os
/// botões do HUD nunca disparam.
private final class FirstMouseHostingView<Content: View>: NSHostingView<Content> {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

/// Painel sem moldura, always-on-top e não-ativante, colado no topo da tela
/// e centralizado na notch (em Mac sem notch, flutua centralizado no topo).
final class NotchPanelController {
    private let panel: NSPanel

    init(store: HUDStore) {
        panel = NotchPanelWindow(
            contentRect: .zero,
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false            // a sombra é do SwiftUI, no formato da cápsula
        panel.isMovable = false
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = true // nunca rouba foco de quem está digitando
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        // Por ÚLTIMO: isFloatingPanel/styleMask resetam o level; .statusBar (25) fica
        // acima da janela "Menubar" do sistema (24), que senão engole os cliques do topo.
        panel.level = .statusBar

        let root = NotchHUDView(onSizeChange: { [weak self] size in
            self?.anchor(to: size)
        })
        .environment(store)

        panel.contentView = FirstMouseHostingView(rootView: root)
        panel.orderFrontRegardless()
    }

    /// Reposiciona colado no topo da tela, centralizado — a ilha FUNDE com a notch
    /// (o preto do HUD encosta no preto do hardware, sem fresta).
    private func anchor(to size: CGSize) {
        guard let screen = NSScreen.main, size.width > 0, size.height > 0 else { return }
        let frame = screen.frame
        let origin = NSPoint(
            x: frame.midX - size.width / 2,
            y: frame.maxY - size.height
        )
        panel.setFrame(NSRect(origin: origin, size: size), display: true)
    }
}

/// Geometria da notch da tela principal (com fallback para Mac sem notch).
struct NotchMetrics {
    let width: CGFloat    // largura do recorte da câmera
    let height: CGFloat   // altura da notch (ou da barra de menus)
    let hasNotch: Bool

    static func current() -> NotchMetrics {
        guard let screen = NSScreen.main else {
            return NotchMetrics(width: 196, height: 32, hasNotch: false)
        }
        let inset = screen.safeAreaInsets.top
        if inset > 0, let left = screen.auxiliaryTopLeftArea, let right = screen.auxiliaryTopRightArea {
            let notchWidth = screen.frame.width - left.width - right.width
            return NotchMetrics(width: notchWidth, height: inset, hasNotch: true)
        }
        // Sem notch: a ilha flutua colada ao topo com proporções de notch típica.
        let menuBar = screen.frame.maxY - screen.visibleFrame.maxY
        return NotchMetrics(width: 196, height: max(menuBar, 32), hasNotch: false)
    }
}
