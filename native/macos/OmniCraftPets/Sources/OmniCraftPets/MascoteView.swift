import SwiftUI

/// O pet da ilha: **coda** enquanto um agente trabalha, **acena** quando alguém
/// espera por você, **comemora** quando termina e mostra **erro** quando falha.
///
/// A arte vem do atlas do pet escolhido (Fucho é o mascote do OmniCraft); sem
/// atlas, cai no mascote desenhado em código — nenhuma arte de terceiros.
public struct MascoteView: View {
    let estado: EstadoMascote
    var pet: Pet = .fucho
    var altura: CGFloat = 22
    var ritmo: RitmoPet = .ameno

    public init(estado: EstadoMascote, pet: Pet = .fucho, altura: CGFloat = 22, ritmo: RitmoPet = .ameno) {
        self.estado = estado; self.pet = pet; self.altura = altura; self.ritmo = ritmo
    }

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    /// Grade do desenho de fallback (pt), alinhada a 2 pt.
    private let tamanho = CGSize(width: 18, height: 15)

    /// Quadros por segundo do desenho de fallback.
    private static let passosPorSegundo: Double = 6

    public var body: some View {
        Group {
            if let animacao = PetSpritesheet.animacao(estado.animacoes, de: pet) {
                atlas(animacao)
            } else {
                desenhado()
            }
        }
        .accessibilityHidden(true)   // o texto do pill já comunica o estado
    }

    // MARK: Pet do atlas

    @ViewBuilder
    private func atlas(_ animacao: PetAnimacao) -> some View {
        if reduceMotion || animacao.quadros.count == 1 {
            // Sem ciclo: primeira pose, parada.
            quadroDoPet(animacao.quadros[0])
        } else {
            TimelineView(.animation(minimumInterval: animacao.msEfetivo(ritmo.fator) / 1000)) { tl in
                quadroDoPet(animacao.quadro(em: tl.date, fator: ritmo.fator) ?? animacao.quadros[0])
            }
        }
    }

    private func quadroDoPet(_ imagem: NSImage) -> some View {
        Image(nsImage: imagem)
            .resizable()
            .scaledToFit()
            .frame(width: altura, height: altura)
    }

    // MARK: Fallback desenhado em código

    @ViewBuilder
    private func desenhado() -> some View {
        Group {
            if estado == .trabalhando && !reduceMotion {
                TimelineView(.animation(minimumInterval: ritmo.fator / Self.passosPorSegundo)) { tl in
                    canvas(quadro: passo(em: tl.date) % 4)
                }
            } else {
                canvas(quadro: 0)
            }
        }
        .frame(width: tamanho.width, height: tamanho.height)
    }

    private func passo(em data: Date) -> Int {
        Int(data.timeIntervalSinceReferenceDate * Self.passosPorSegundo / ritmo.fator)
    }

    private func canvas(quadro: Int) -> some View {
        Canvas { ctx, size in desenhar(&ctx, size: size, quadro: quadro) }
    }

    private var corPrincipal: Color {
        switch estado {
        case .concluido: Color(red: 0.30, green: 0.85, blue: 0.45)
        case .atencao: .orange
        case .erro: Color(red: 0.95, green: 0.35, blue: 0.35)
        case .trabalhando, .ocioso, .oculto: Color.white.opacity(0.92)
        }
    }

    private func desenhar(_ ctx: inout GraphicsContext, size: CGSize, quadro: Int) {
        let cor = corPrincipal
        let quique: CGFloat = (quadro == 1 || quadro == 3) ? -1 : 0

        let corpo = CGRect(x: 2, y: 2 + quique, width: 14, height: 9)
        ctx.fill(Path(roundedRect: corpo, cornerRadius: 3.5, style: .continuous), with: .color(cor))

        let antenaX: CGFloat = quadro == 1 ? 7 : (quadro == 3 ? 9 : 8)
        ctx.fill(Path(ellipseIn: CGRect(x: antenaX, y: quique, width: 2, height: 2)),
                 with: .color(cor.opacity(0.75)))

        if estado == .concluido {
            var check = Path()
            check.move(to: CGPoint(x: 6, y: 6.5 + quique))
            check.addLine(to: CGPoint(x: 8, y: 8.5 + quique))
            check.addLine(to: CGPoint(x: 12, y: 4.5 + quique))
            ctx.stroke(check, with: .color(.black.opacity(0.75)),
                       style: StrokeStyle(lineWidth: 1.8, lineCap: .round, lineJoin: .round))
        } else {
            let piscando = (estado == .trabalhando && quadro == 2)
            let alturaOlho: CGFloat = piscando ? 1 : 3
            let yOlho: CGFloat = 5 + quique + (piscando ? 1 : 0)
            for x in [CGFloat(5), CGFloat(10)] {
                ctx.fill(Path(roundedRect: CGRect(x: x, y: yOlho, width: 3, height: alturaOlho),
                              cornerRadius: 1),
                         with: .color(.black.opacity(0.8)))
            }
        }

        let base = 11 + quique
        let (esq, dir): (CGFloat, CGFloat) = switch quadro {
        case 0: (3, 1)
        case 1: (2, 2)
        case 2: (1, 3)
        default: (2, 2)
        }
        ctx.fill(Path(CGRect(x: 4.5, y: base, width: 3, height: esq)), with: .color(cor))
        ctx.fill(Path(CGRect(x: 10.5, y: base, width: 3, height: dir)), with: .color(cor))
    }
}
