import AppKit

// MARK: - Atlas de pet (folha + manifesto JSON)

/// Manifesto que acompanha a folha: célula, âncora e as animações por LINHA.
/// ```json
/// { "image": "fucho-sheet.webp", "cell": {"w":288,"h":288},
///   "anchor": "bottom-center",
///   "animations": { "codando": {"row":7,"frames":7,"ms_per_frame":150} } }
/// ```
public struct PetManifesto: Decodable {
    public struct Celula: Decodable { public let w: Int; public let h: Int }
    public struct Animacao: Decodable {
        let row: Int
        let frames: Int
        let msPerFrame: Int

        enum CodingKeys: String, CodingKey {
            case row, frames
            case msPerFrame = "ms_per_frame"
        }
    }

    public let image: String
    public let cell: Celula
    public let anchor: String?
    public let animations: [String: Animacao]
}

/// Uma animação pronta para tocar: quadros já fatiados + ritmo.
///
/// O `msPorQuadro` vem do manifesto; o `fator` do ritmo escolhido multiplica em
/// cima dele (>1 = mais devagar), sem refatiar nada.
public struct PetAnimacao {
    public let quadros: [NSImage]
    public let msPorQuadro: Int

    public func msEfetivo(_ fator: Double) -> Double {
        Double(max(1, msPorQuadro)) * max(0.1, fator)
    }

    /// Quadro correspondente ao instante — ciclo contínuo.
    public func quadro(em data: Date, fator: Double) -> NSImage? {
        guard !quadros.isEmpty else { return nil }
        let passo = Int(data.timeIntervalSinceReferenceDate * 1000 / msEfetivo(fator))
        return quadros[abs(passo) % quadros.count]
    }
}

/// Ritmo do pet. "Manifesto" é a velocidade que a arte declara; os demais
/// desaceleram — animação de canto de tela cansa quando corre demais.
public enum RitmoPet: String, CaseIterable, Identifiable {
    case calmo
    case ameno
    case manifesto

    public var id: String { rawValue }

    public var fator: Double {
        switch self {
        case .calmo: 2.4
        case .ameno: 1.6
        case .manifesto: 1.0
        }
    }

    public var label: String {
        switch self {
        case .calmo: "Calmo"
        case .ameno: "Ameno"
        case .manifesto: "Original"
        }
    }
}

/// Pets disponíveis. Fucho é o mascote do OmniCraft (padrão).
public enum Pet: String, CaseIterable, Identifiable {
    case fucho
    case polly
    case desenhado   // sem folha: o mascote desenhado em código

    public var id: String { rawValue }

    public var label: String {
        switch self {
        case .fucho: "Fucho"
        case .polly: "Polly"
        case .desenhado: "Desenhado"
        }
    }

    /// Folhas do pet no bundle (`<base>.webp` + `<base>.json`), da mais antiga
    /// para a mais nova: em nome repetido, a ÚLTIMA vence (arte mais recente).
    /// Somar as folhas dá o repertório completo — a v2 do Fucho não tem
    /// `codando`, que vem da v1; a v1 não tem `emotes`/`dano`, que vêm da v2.
    public var folhas: [String] {
        switch self {
        case .fucho: ["fucho-sheet", "fucho-v2-sheet"]
        case .polly: ["estrela-sheet", "polly-sheet"]
        case .desenhado: []
        }
    }
}

/// Carrega e fatia os atlas — do bundle do app ou de uma pasta sua.
///
/// **Pet próprio:** coloque `pet.webp`/`pet.png` + `pet.json` (mesmo formato de
/// manifesto) em `~/.config/omnicraft-notch/` e escolha "Meu pet" no menu de
/// debug. Sem manifesto, uma tira horizontal de quadros quadrados também serve.
@MainActor
public enum PetSpritesheet {

    public static var pastaDoUsuario: URL {
        URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent(".config/omnicraft-notch")
    }

    /// Animações já fatiadas por pet, para não refatiar a cada quadro.
    private static var cache: [String: [String: PetAnimacao]] = [:]

    /// Animações do pet, prontas para tocar. Vazio = use o desenho em código.
    public static func animacoes(de pet: Pet) -> [String: PetAnimacao] {
        if let pronto = cache[pet.rawValue] { return pronto }
        let carregado = carregar(pet)
        cache[pet.rawValue] = carregado
        return carregado
    }

    /// Uma animação pelo nome, com alternativas em ordem de preferência.
    public static func animacao(_ nomes: [String], de pet: Pet) -> PetAnimacao? {
        let todas = animacoes(de: pet)
        for nome in nomes {
            if let a = todas[nome], !a.quadros.isEmpty { return a }
        }
        return nil
    }

    // MARK: Carga

    private static func carregar(_ pet: Pet) -> [String: PetAnimacao] {
        let folhas = arquivos(de: pet)
        guard !folhas.isEmpty else { return [:] }

        // Carrega tudo antes de fatiar: a caixa de recorte precisa valer para
        // TODAS as folhas do pet, senão ele mudaria de tamanho ao trocar de
        // animação (uma vinda da v1, outra da v2).
        var carregadas: [(sheet: NSImage, manifesto: PetManifesto?)] = []
        for (folha, manifestoURL) in folhas {
            guard let sheet = NSImage(contentsOf: folha) else { continue }
            let m = manifestoURL
                .flatMap { try? Data(contentsOf: $0) }
                .flatMap { try? JSONDecoder().decode(PetManifesto.self, from: $0) }
            carregadas.append((sheet, m))
        }
        guard !carregadas.isEmpty else { return [:] }

        // Sem manifesto: tira horizontal de quadros quadrados.
        if carregadas.count == 1, carregadas[0].manifesto == nil {
            let tira = fatiarTira(carregadas[0].sheet)
            return tira.isEmpty ? [:] : ["idle": PetAnimacao(quadros: tira, msPorQuadro: 160)]
        }

        // Caixa de conteúdo unificada (as células têm o mesmo tamanho).
        var caixa: CGRect?
        for (sheet, m) in carregadas {
            guard let m, let c = caixaDeConteudo(sheet, manifesto: m) else { continue }
            caixa = caixa.map { $0.union(c) } ?? c
        }

        var saida: [String: PetAnimacao] = [:]
        for (sheet, manifesto) in carregadas {
            guard let m = manifesto else { continue }
            let recorte = caixa ?? CGRect(x: 0, y: 0, width: m.cell.w, height: m.cell.h)
            for (nome, anim) in m.animations {
                let quadros = (0..<anim.frames).compactMap {
                    recortar(sheet,
                             x: $0 * m.cell.w + Int(recorte.minX),
                             y: anim.row * m.cell.h + Int(recorte.minY),
                             w: Int(recorte.width), h: Int(recorte.height))
                }
                guard !quadros.isEmpty else { continue }
                saida[nome] = PetAnimacao(quadros: quadros, msPorQuadro: max(30, anim.msPerFrame))
            }
        }
        return saida
    }

    /// Caixa (em coordenadas da célula, origem no topo-esquerda) que contém
    /// pixels visíveis em QUALQUER quadro declarado no manifesto.
    private static func caixaDeConteudo(_ sheet: NSImage, manifesto m: PetManifesto) -> CGRect? {
        guard let cg = sheet.cgImage(forProposedRect: nil, context: nil, hints: nil) else { return nil }
        let largura = cg.width, altura = cg.height
        guard let ctx = CGContext(data: nil, width: largura, height: altura,
                                  bitsPerComponent: 8, bytesPerRow: largura * 4,
                                  space: CGColorSpaceCreateDeviceRGB(),
                                  bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue),
              let base = ctx.data
        else { return nil }
        ctx.draw(cg, in: CGRect(x: 0, y: 0, width: largura, height: altura))
        let px = base.bindMemory(to: UInt8.self, capacity: largura * altura * 4)

        var minX = m.cell.w, minY = m.cell.h, maxX = -1, maxY = -1
        let limiar: UInt8 = 12   // ignora franja quase transparente

        for (_, anim) in m.animations {
            let y0 = anim.row * m.cell.h
            guard y0 + m.cell.h <= altura else { continue }
            for quadro in 0..<anim.frames {
                let x0 = quadro * m.cell.w
                guard x0 + m.cell.w <= largura else { continue }
                for dy in 0..<m.cell.h {
                    let linha = (y0 + dy) * largura * 4
                    for dx in 0..<m.cell.w {
                        guard px[linha + (x0 + dx) * 4 + 3] > limiar else { continue }
                        if dx < minX { minX = dx }
                        if dx > maxX { maxX = dx }
                        if dy < minY { minY = dy }
                        if dy > maxY { maxY = dy }
                    }
                }
            }
        }
        guard maxX >= minX, maxY >= minY else { return nil }

        // Uma folga de 2 px para o recorte não encostar no traço.
        let folga = 2
        minX = max(0, minX - folga); minY = max(0, minY - folga)
        maxX = min(m.cell.w - 1, maxX + folga); maxY = min(m.cell.h - 1, maxY + folga)
        return CGRect(x: minX, y: minY, width: maxX - minX + 1, height: maxY - minY + 1)
    }

    /// Folhas do pet no bundle; se houver arquivo na pasta do usuário, ele vence
    /// (e substitui tudo — um pet próprio não se mistura com o do bundle).
    private static func arquivos(de pet: Pet) -> [(folha: URL, manifesto: URL?)] {
        let pasta = pastaDoUsuario
        for ext in ["webp", "png", "gif"] {
            let custom = pasta.appendingPathComponent("pet.\(ext)")
            if FileManager.default.fileExists(atPath: custom.path) {
                let manifesto = pasta.appendingPathComponent("pet.json")
                return [(custom, FileManager.default.fileExists(atPath: manifesto.path) ? manifesto : nil)]
            }
        }
        return pet.folhas.compactMap { base in
            guard let folha = Bundle.module.url(forResource: base, withExtension: "webp", subdirectory: "Pets")
            else { return nil }
            return (folha, Bundle.module.url(forResource: base, withExtension: "json", subdirectory: "Pets"))
        }
    }

    // MARK: Recorte

    /// Recorta uma célula em coordenadas de PIXEL, com origem no topo-esquerda.
    private static func recortar(_ sheet: NSImage, x: Int, y: Int, w: Int, h: Int) -> NSImage? {
        guard let rep = sheet.representations.first else { return nil }
        let alturaTotal = CGFloat(rep.pixelsHigh)
        let larguraTotal = CGFloat(rep.pixelsWide)
        guard CGFloat(x + w) <= larguraTotal, CGFloat(y + h) <= alturaTotal else { return nil }

        // AppKit desenha com origem embaixo: inverte o Y do manifesto.
        let origem = CGRect(x: CGFloat(x), y: alturaTotal - CGFloat(y + h),
                            width: CGFloat(w), height: CGFloat(h))
        let destino = CGSize(width: CGFloat(w), height: CGFloat(h))

        let quadro = NSImage(size: destino)
        quadro.lockFocus()
        NSGraphicsContext.current?.imageInterpolation = .high
        sheet.draw(in: CGRect(origin: .zero, size: destino),
                   from: origem, operation: .sourceOver, fraction: 1)
        quadro.unlockFocus()
        return quadro
    }

    /// Sem manifesto: tira horizontal de quadros quadrados.
    private static func fatiarTira(_ sheet: NSImage) -> [NSImage] {
        guard let rep = sheet.representations.first else { return [] }
        let largura = CGFloat(rep.pixelsWide), altura = CGFloat(rep.pixelsHigh)
        guard altura > 0, largura >= altura else { return [] }
        let total = max(1, Int((largura / altura).rounded()))
        guard total <= 64 else { return [] }
        return (0..<total).compactMap {
            recortar(sheet, x: $0 * Int(altura), y: 0, w: Int(altura), h: Int(altura))
        }
    }
}
