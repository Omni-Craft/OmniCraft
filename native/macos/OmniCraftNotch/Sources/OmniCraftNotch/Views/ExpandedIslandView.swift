import SwiftUI

/// A "ilha": fila global de atenção no topo, janelas de limite, lista de sessões
/// e utilidades locais a um clique.
struct ExpandedIslandView: View {
    @Environment(HUDStore.self) private var store

    private let metrics = NotchMetrics.current()

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            header

            if !store.snapshot.janelasLimite.isEmpty {
                janelasView
            }

            if store.visibleSessions.isEmpty {
                emptyState
            } else {
                if let atual = store.pedidoAtual {
                    AttentionCardView(sessao: atual.sessao, pedido: atual.pedido)
                }
                sessionList
            }

            utilidadesView
        }
        .padding(.horizontal, 14)
        .padding(.bottom, 12)
        .frame(width: max(470, metrics.width + 260), alignment: .leading)
        .islandBackground(bottomRadius: 24)
    }

    // MARK: Header (faixa ao lado da câmera) + "há Xs"

    private var header: some View {
        HStack(spacing: 0) {
            Text(store.snapshot.counts.pillText)
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(store.snapshot.counts.isUnavailable ? .secondary : .primary)
                .lineLimit(1)
                .padding(.leading, 6)

            Spacer(minLength: metrics.hasNotch ? metrics.width + 20 : 24)

            if store.feedSource == .live, let gerado = store.lastGeneratedAt {
                TimelineView(.periodic(from: .now, by: 1)) { contexto in
                    Text(tempoRelativo(desde: gerado, agora: contexto.date))
                        .font(.system(size: 9.5).monospacedDigit())
                        .foregroundStyle(.tertiary)
                        .accessibilityLabel("Feed atualizado \(tempoRelativo(desde: gerado, agora: contexto.date))")
                }
                .padding(.trailing, 6)
            }

            Button {
                store.collapseManually()
            } label: {
                Image(systemName: "chevron.up")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(.secondary)
                    .padding(6)
                    .contentShape(Circle())
            }
            .buttonStyle(HoverCircleButtonStyle())
            .focusable()
            .accessibilityLabel("Colapsar")
            .accessibilityHint("Volta para a cápsula compacta")
        }
        .frame(height: metrics.height + 8)
    }

    private func tempoRelativo(desde: Date, agora: Date) -> String {
        let s = max(Int(agora.timeIntervalSince(desde)), 0)
        if s < 60 { return "há \(s) s" }
        return "há \(s / 60) min"
    }

    // MARK: Janelas de limite do provedor ("5 h ▓▓ 52% · reseta em 2 h 05")

    private var janelasView: some View {
        VStack(spacing: 4) {
            ForEach(store.snapshot.janelasLimite) { janela in
                HStack(spacing: 8) {
                    Text(janela.rotulo)
                        .font(.system(size: 10, weight: .semibold).monospacedDigit())
                        .foregroundStyle(.secondary)
                        .frame(width: 28, alignment: .leading)

                    if let fracao = janela.fracaoUsada {
                        GeometryReader { geo in
                            ZStack(alignment: .leading) {
                                Capsule().fill(.quaternary)
                                Capsule()
                                    .fill(corJanela(fracao))
                                    .frame(width: max(geo.size.width * fracao, 3))
                            }
                        }
                        .frame(height: 3)
                        Text("\(Int(fracao * 100))%")
                            .font(.system(size: 9.5, weight: .semibold).monospacedDigit())
                            .foregroundStyle(corJanela(fracao))
                            .frame(width: 32, alignment: .trailing)
                    } else {
                        // Janela ilegível: nunca inventa barra nem número.
                        Text("— janela ilegível")
                            .font(.system(size: 9.5))
                            .foregroundStyle(.secondary)
                        Spacer(minLength: 0)
                    }

                    Text(janela.reset ?? "—")
                        .font(.system(size: 9.5))
                        .foregroundStyle(.tertiary)
                        .fixedSize()
                }
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(acessibilidadeJanela(janela))
            }
        }
        .padding(.horizontal, 6)
    }

    private func corJanela(_ fracao: Double) -> Color {
        switch fracao {
        case ..<0.6: .green
        case ..<0.85: .orange
        default: .red
        }
    }

    private func acessibilidadeJanela(_ janela: JanelaLimite) -> String {
        if let fracao = janela.fracaoUsada {
            return "Janela de \(janela.rotulo): \(Int(fracao * 100)) por cento usado. \(janela.reset ?? "")"
        }
        return "Janela de \(janela.rotulo): ilegível"
    }

    // MARK: Sessões

    /// Lista com rolagem quando o feed real traz mais sessões do que cabe na tela:
    /// nenhuma linha some (regra do degraded). Até 8 sessões, o layout aprovado.
    private var maxListHeight: CGFloat {
        (NSScreen.main.map { $0.visibleFrame.height * 0.6 }) ?? 560
    }

    /// A sessão do card atual sai da lista (já está em destaque acima).
    private var sessoesEmLista: [AgentSession] {
        let idNoCard = store.pedidoAtual?.sessao.id
        return store.visibleSessions.filter { $0.id != idNoCard }
    }

    @ViewBuilder
    private var sessionList: some View {
        if sessoesEmLista.count <= 8 {
            sessionRows
        } else {
            ScrollView(showsIndicators: false) {
                sessionRows
            }
            .frame(height: maxListHeight)
        }
    }

    private var sessionRows: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(sessoesEmLista) { session in
                SessionRowView(session: session)
            }
        }
    }

    private var emptyState: some View {
        HStack(spacing: 8) {
            Image(systemName: "moon.zzz")
                .foregroundStyle(.secondary)
            Text("Nenhuma sessão no momento")
                .font(.system(size: 12))
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .center)
        .padding(.vertical, 12)
        .accessibilityElement(children: .combine)
    }

    // MARK: Utilidades locais ("tudo a um clique" — ações visuais/log)

    private enum Utilidade: String, CaseIterable, Identifiable {
        case servidores, comandos, atalhos
        var id: String { rawValue }
        var rotulo: String {
            switch self {
            case .servidores: "servers"
            case .comandos: "comandos"
            case .atalhos: "atalhos"
            }
        }
        var icone: String {
            switch self {
            case .servidores: "server.rack"
            case .comandos: "command"
            case .atalhos: "folder"
            }
        }
    }

    @State private var utilidadeAberta: Utilidade?

    private var utilidadesView: some View {
        VStack(alignment: .leading, spacing: 8) {
            Rectangle().fill(.white.opacity(0.08)).frame(height: 1)

            HStack(spacing: 6) {
                ForEach(Utilidade.allCases) { utilidade in
                    botaoUtilidade(utilidade)
                }
                Spacer(minLength: 0)
            }

            switch utilidadeAberta {
            case .servidores: listaServidores
            case .comandos: listaComandos
            case .atalhos: listaAtalhos
            case nil: EmptyView()
            }
        }
    }

    private func botaoUtilidade(_ utilidade: Utilidade) -> some View {
        let aberta = utilidadeAberta == utilidade
        return Button {
            utilidadeAberta = aberta ? nil : utilidade
        } label: {
            HStack(spacing: 4) {
                Image(systemName: utilidade.icone)
                    .font(.system(size: 9, weight: .semibold))
                Text(utilidade.rotulo)
                    .font(.system(size: 10, weight: .semibold))
            }
            .padding(.horizontal, 9)
            .padding(.vertical, 4)
            .background(Capsule().fill(.white.opacity(aberta ? 0.14 : 0.06)))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .foregroundStyle(aberta ? .primary : .secondary)
        .focusable()
        .accessibilityLabel("\(utilidade.rotulo)\(aberta ? ", aberto" : "")")
    }

    private var listaServidores: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(store.servidores) { servidor in
                HStack(spacing: 8) {
                    // Estado do servidor: ícone + texto, nunca só a cor do ponto.
                    Image(systemName: servidor.rodando ? "circle.fill" : "circle")
                        .font(.system(size: 7))
                        .foregroundStyle(servidor.rodando ? .green : .secondary)
                    VStack(alignment: .leading, spacing: 0) {
                        Text("\(servidor.nome) · \(servidor.rodando ? "rodando" : "parado")")
                            .font(.system(size: 11, weight: .medium))
                        Text(servidor.url)
                            .font(.system(size: 9.5, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                    Spacer(minLength: 8)
                    botaoMini("abrir") { store.acaoServidor(servidor, "abrir") }
                    botaoMini("copiar") { store.copiar(servidor.url, rotulo: servidor.nome) }
                    if servidor.rodando {
                        botaoMini("parar") { store.acaoServidor(servidor, "parar") }
                    }
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel(
                    "Servidor \(servidor.nome), \(servidor.rodando ? "rodando" : "parado"), \(servidor.url)")
            }
        }
        .padding(.horizontal, 4)
    }

    private var listaComandos: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(store.comandos) { comando in
                HStack(spacing: 8) {
                    Text(comando.rotulo)
                        .font(.system(size: 11, weight: .medium))
                        .frame(width: 64, alignment: .leading)
                    Text(comando.comando)
                        .font(.system(size: 9.5, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Spacer(minLength: 8)
                    botaoMini("copiar") { store.copiar(comando.comando, rotulo: comando.rotulo) }
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel("Comando \(comando.rotulo): \(comando.comando)")
            }
        }
        .padding(.horizontal, 4)
    }

    private var listaAtalhos: some View {
        HStack(spacing: 6) {
            ForEach(store.atalhos) { atalho in
                Button {
                    store.abrirAtalho(atalho)
                } label: {
                    HStack(spacing: 4) {
                        Image(systemName: atalho.icone)
                            .font(.system(size: 9))
                        Text(atalho.rotulo)
                            .font(.system(size: 10))
                    }
                    .padding(.horizontal, 8)
                    .padding(.vertical, 4)
                    .background(Capsule().fill(.white.opacity(0.06)))
                    .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
                .focusable()
                .accessibilityLabel("Abrir \(atalho.rotulo)")
            }
        }
        .padding(.horizontal, 4)
    }

    private func botaoMini(_ rotulo: String, acao: @escaping () -> Void) -> some View {
        Button(rotulo, action: acao)
            .buttonStyle(.plain)
            .font(.system(size: 9.5, weight: .semibold))
            .foregroundStyle(.secondary)
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .background(Capsule().fill(.white.opacity(0.08)))
            .focusable()
    }
}

/// Hover visível em botões de ícone, sem o cinza nativo do macOS.
struct HoverCircleButtonStyle: ButtonStyle {
    @State private var isHovering = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .background(Circle().fill(.primary.opacity(isHovering ? 0.1 : 0)))
            .opacity(configuration.isPressed ? 0.6 : 1)
            .onHover { isHovering = $0 }
            .animation(.easeOut(duration: 0.12), value: isHovering)
    }
}
