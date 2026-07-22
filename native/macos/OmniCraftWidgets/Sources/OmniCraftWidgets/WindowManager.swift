import AppKit
import SwiftUI
import Observation

// MARK: - Tipos de widget

enum TipoWidget: String, CaseIterable, Identifiable {
    case transcript, ferramentas, subagentes, uso, tarefas, servidores, rotas, board

    var id: String { rawValue }

    var titulo: String {
        switch self {
        case .transcript: "Transcript"
        case .ferramentas: "Ferramentas"
        case .subagentes: "Subagentes"
        case .uso: "Uso"
        case .tarefas: "Tarefas"
        case .servidores: "Servidores"
        case .rotas: "Rotas"
        case .board: "Board"
        }
    }

    var icone: String {
        switch self {
        case .transcript: "text.bubble"
        case .ferramentas: "wrench.and.screwdriver"
        case .subagentes: "point.3.filled.connected.trianglepath.dotted"
        case .uso: "gauge.with.needle"
        case .tarefas: "checklist"
        case .servidores: "server.rack"
        case .rotas: "folder"
        case .board: "square.grid.3x1.below.line.grid.1x2"
        }
    }

    var tamanhoInicial: NSSize {
        switch self {
        case .transcript: NSSize(width: 380, height: 460)
        case .ferramentas: NSSize(width: 360, height: 380)
        case .subagentes: NSSize(width: 360, height: 340)
        case .uso: NSSize(width: 320, height: 300)
        case .tarefas: NSSize(width: 320, height: 360)
        case .servidores: NSSize(width: 400, height: 320)
        case .rotas: NSSize(width: 340, height: 260)
        case .board: NSSize(width: 640, height: 420)
        }
    }
}

// MARK: - Painel de um widget

/// Janela de widget: sem moldura visível, translúcida, always-on-top, movível e
/// redimensionável, e que NUNCA rouba foco (lições do OmniCraftNotch aplicadas:
/// first mouse + level definido por último).
private final class JanelaWidget: NSPanel {
    override var canBecomeKey: Bool { true }   // teclado quando necessário, sem ativar o app
}

private final class FirstMouseHostingView<Content: View>: NSHostingView<Content> {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

@MainActor
@Observable
final class PainelWidget: NSObject, NSWindowDelegate {
    let tipo: TipoWidget
    private(set) var emRail = false
    /// Borda em que o rail está encostado (.minX = esquerda, .maxX = direita).
    private(set) var railNaEsquerda = false

    private let panel: NSPanel
    private let store: WidgetStore
    private var frameAntesDoRail: NSRect?
    private var debounceRail: Timer?
    private var restaurandoFrame = false

    private var chaveFrame: String { "frame.\(store.projetoAtual).\(tipo.rawValue)" }

    init(tipo: TipoWidget, store: WidgetStore, indiceCascata: Int) {
        self.tipo = tipo
        self.store = store
        panel = JanelaWidget(
            contentRect: NSRect(origin: .zero, size: tipo.tamanhoInicial),
            styleMask: [.titled, .nonactivatingPanel, .fullSizeContentView, .resizable],
            backing: .buffered,
            defer: false
        )
        super.init()

        panel.titleVisibility = .hidden
        panel.titlebarAppearsTransparent = true
        panel.title = "OmniCraftWidget-\(tipo.rawValue)"   // para screenshots por janela
        for botao: NSWindow.ButtonType in [.closeButton, .miniaturizeButton, .zoomButton] {
            panel.standardWindowButton(botao)?.isHidden = true
        }
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.isMovableByWindowBackground = true
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = true
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.minSize = NSSize(width: 280, height: 160)
        panel.maxSize = NSSize(width: 640, height: 900)
        // Por último — styleMask/painel podem resetar o level (bug já pago no notch).
        panel.level = .floating
        panel.delegate = self

        let root = WidgetRootView(tipo: tipo, controlador: self)
            .environment(store)
        panel.contentView = FirstMouseHostingView(rootView: root)

        posicionar(indiceCascata: indiceCascata)
        panel.orderFrontRegardless()
    }

    // MARK: posição inicial: lembrada por widget+projeto, senão cascata

    private func posicionar(indiceCascata: Int) {
        if let salvo = UserDefaults.standard.string(forKey: chaveFrame) {
            panel.setFrame(from: salvo)
            // O frame salvo pode vir de um arranjo de monitores que não existe
            // mais (ex.: display externo à esquerda, com x negativo). Se ele não
            // toca NENHUMA tela atual, o widget nasceria invisível e sem jeito de
            // recuperar — então descarta e cai na cascata.
            if NSScreen.screens.contains(where: { $0.visibleFrame.intersects(panel.frame) }) {
                fixarNaAreaVisivel()
                return
            }
            UserDefaults.standard.removeObject(forKey: chaveFrame)
        }
        guard let tela = NSScreen.main else { return }
        let v = tela.visibleFrame
        // Cascata: nunca nascem empilhados no mesmo pixel.
        let passo = CGFloat(indiceCascata) * 32
        let origem = NSPoint(
            x: min(v.minX + 80 + passo, v.maxX - tipo.tamanhoInicial.width - 16),
            y: max(v.maxY - 120 - tipo.tamanhoInicial.height - passo, v.minY + 16)
        )
        panel.setFrame(NSRect(origin: origem, size: tipo.tamanhoInicial), display: true)
    }

    func trazerParaFrente() {
        panel.orderFrontRegardless()
    }

    func fechar() {
        panel.orderOut(nil)
    }

    // MARK: rail compacto

    /// Hook de debug (`--rail <widget>`): entra no rail sem precisar arrastar.
    func forcarRail(esquerda: Bool = false) {
        entrarNoRail(esquerda: esquerda)
    }

    func sairDoRail() {
        guard emRail else { return }
        emRail = false
        var destino = frameAntesDoRail ?? NSRect(origin: panel.frame.origin, size: tipo.tamanhoInicial)
        if let v = panel.screen?.visibleFrame {
            // Sai da borda com um respiro, senão o rail dispara de novo.
            destino.origin.x = railNaEsquerda
                ? v.minX + 24
                : v.maxX - destino.width - 24
        }
        restaurandoFrame = true
        panel.setFrame(destino, display: true, animate: !reduzirMovimento)
        restaurandoFrame = false
        salvarFrame()
    }

    private func entrarNoRail(esquerda: Bool) {
        guard !emRail else { return }
        frameAntesDoRail = panel.frame
        emRail = true
        railNaEsquerda = esquerda
        guard let v = panel.screen?.visibleFrame else { return }
        let tamanho = NSSize(width: 42, height: 112)
        let origem = NSPoint(
            x: esquerda ? v.minX : v.maxX - tamanho.width,
            y: min(max(panel.frame.midY - tamanho.height / 2, v.minY), v.maxY - tamanho.height)
        )
        restaurandoFrame = true
        panel.setFrame(NSRect(origin: origem, size: tamanho), display: true, animate: !reduzirMovimento)
        restaurandoFrame = false
    }

    private var reduzirMovimento: Bool {
        NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
    }

    // MARK: NSWindowDelegate — clamp, rail e persistência

    func windowDidMove(_ notification: Notification) {
        guard !restaurandoFrame else { return }
        fixarNaAreaVisivel()
        // Rail só quando o arrasto ASSENTA na borda (debounce), não ao passar por ela.
        debounceRail?.invalidate()
        debounceRail = Timer.scheduledTimer(withTimeInterval: 0.35, repeats: false) { [weak self] _ in
            Task { @MainActor [weak self] in self?.verificarBorda() }
        }
        if !emRail { salvarFrame() }
    }

    func windowDidEndLiveResize(_ notification: Notification) {
        if !emRail { salvarFrame() }
    }

    private func verificarBorda() {
        guard !emRail, let v = panel.screen?.visibleFrame else { return }
        let f = panel.frame
        if f.minX <= v.minX + 2 { entrarNoRail(esquerda: true) }
        else if f.maxX >= v.maxX - 2 { entrarNoRail(esquerda: false) }
    }

    /// Arrastou até a borda? O widget não pode SAIR da área visível (vale para
    /// qualquer display, inclusive externo).
    private func fixarNaAreaVisivel() {
        // `panel.screen` é nil quando a janela está INTEIRAMENTE fora de
        // qualquer tela — exatamente o caso que este resgate existe para
        // corrigir (frame salvo com um monitor externo que não está mais
        // ligado). Sem o fallback, o widget ficava invisível para sempre.
        guard let v = (panel.screen ?? NSScreen.main)?.visibleFrame else { return }
        var f = panel.frame
        f.origin.x = min(max(f.origin.x, v.minX), v.maxX - f.width)
        f.origin.y = min(max(f.origin.y, v.minY), v.maxY - f.height)
        if f.origin != panel.frame.origin {
            restaurandoFrame = true
            panel.setFrameOrigin(f.origin)
            restaurandoFrame = false
        }
    }

    private func salvarFrame() {
        UserDefaults.standard.set(panel.frameDescriptor, forKey: chaveFrame)
    }
}

// MARK: - Gerente global

@MainActor
final class GerenteJanelas {
    static let shared = GerenteJanelas()
    private var paineis: [TipoWidget: PainelWidget] = [:]
    private var abertos = 0

    func abrir(_ tipo: TipoWidget, store: WidgetStore) {
        if let existente = paineis[tipo] {
            existente.trazerParaFrente()
            return
        }
        paineis[tipo] = PainelWidget(tipo: tipo, store: store, indiceCascata: abertos)
        abertos += 1
    }

    func abrirTodos(store: WidgetStore) {
        for tipo in TipoWidget.allCases { abrir(tipo, store: store) }
    }

    func fechar(_ tipo: TipoWidget) {
        paineis[tipo]?.fechar()
    }

    func painel(_ tipo: TipoWidget) -> PainelWidget? {
        paineis[tipo]
    }
}
