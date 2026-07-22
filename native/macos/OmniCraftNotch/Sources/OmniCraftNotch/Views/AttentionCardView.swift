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

            if let questionario = pedido.questionario {
                QuestionarioView(sessao: sessao, pedido: pedido, questionario: questionario)
                if fila.count > 1 { pager }
            } else {
                requestBlock(pedido)
            }
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
        let enviando = store.pendingRequestIDs.contains(request.id)
        return VStack(alignment: .leading, spacing: 8) {
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

            // O comando/diff pedido, visível ANTES de aprovar (lição do VibeIsland).
            if let detalhe = request.detalhe {
                Text(detalhe)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.primary.opacity(0.8))
                    .lineLimit(4)
                    .padding(8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(.black.opacity(0.35), in: RoundedRectangle(cornerRadius: 8))
                    .accessibilityLabel("Detalhe do pedido: \(detalhe)")
            }

            HStack(spacing: 8) {
                Button(enviando ? "enviando…" : "✓ Aprovar") { store.approve(request, in: sessao) }
                    .buttonStyle(AttentionButtonStyle(kind: .primary))
                    .accessibilityLabel("Aprovar: \(request.question)")

                Button("✓ Aprovar tudo nesta sessão") { store.approveAll(in: sessao) }
                    .buttonStyle(AttentionButtonStyle(kind: .secondary))
                    .accessibilityLabel("Aprovar todos os pedidos da sessão \(sessao.title)")

                Button("✕ Rejeitar") { store.reject(request, in: sessao) }
                    .buttonStyle(AttentionButtonStyle(kind: .secondary))
                    .accessibilityLabel("Rejeitar: \(request.question)")

                Spacer(minLength: 0)

                Button {
                    store.irParaTerminal(sessao)
                } label: {
                    HStack(spacing: 3) {
                        Image(systemName: "terminal")
                            .font(.system(size: 9))
                        Text("terminal")
                            .font(.system(size: 10, weight: .medium))
                    }
                    .foregroundStyle(.secondary)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .focusable()
                .accessibilityLabel("Ir para o terminal da sessão \(sessao.title)")
            }
            .disabled(enviando)

            // Uma decisão que não chegou ao servidor precisa dizer isso: o card
            // continua na pilha e o motivo fica à vista.
            if let falha = store.falhaPorPedido[request.id] {
                HStack(spacing: 4) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .font(.system(size: 9))
                    Text("Não foi enviado — \(falha)")
                        .font(.system(size: 10))
                        .fixedSize(horizontal: false, vertical: true)
                }
                .foregroundStyle(.orange)
                .accessibilityLabel("A decisão não foi enviada: \(falha)")
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

/// Questionário estruturado inline: seções, caixas de seleção
/// ou escolha única, progresso "N de M respondidas" e Enviar — tudo visual.
struct QuestionarioView: View {
    @Environment(HUDStore.self) private var store
    let sessao: AgentSession
    let pedido: AttentionRequest
    let questionario: Questionario

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Circle().fill(.orange).frame(width: 5, height: 5)
                Text("PERGUNTA PENDENTE")
                    .font(.system(size: 9, weight: .bold, design: .monospaced))
                    .tracking(1)
                    .foregroundStyle(.orange)
                Spacer(minLength: 8)
                if let pendente = pedido.pendenteHa {
                    Text(pendente)
                        .font(.system(size: 9.5))
                        .foregroundStyle(.tertiary)
                }
                Button {
                    store.irParaTerminal(sessao)
                } label: {
                    HStack(spacing: 3) {
                        Image(systemName: "terminal")
                            .font(.system(size: 8))
                        Text("responder no terminal")
                            .font(.system(size: 9.5, weight: .medium))
                    }
                    .foregroundStyle(.secondary)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .focusable()
                .accessibilityLabel("Responder no terminal da sessão \(sessao.title)")
            }

            Text(pedido.question)
                .font(.system(size: 12, weight: .semibold))

            ForEach(questionario.perguntas) { pergunta in
                perguntaView(pergunta)
            }

            HStack {
                Text("\(store.respondidas(questionario)) de \(questionario.perguntas.count) respondidas")
                    .font(.system(size: 10).monospacedDigit())
                    .foregroundStyle(.secondary)
                Spacer()
                Button("Enviar") {
                    store.enviarQuestionario(pedido, in: sessao)
                }
                .buttonStyle(AttentionButtonStyle(kind: .primary))
                .disabled(store.respondidas(questionario) < questionario.perguntas.count)
                .opacity(store.respondidas(questionario) < questionario.perguntas.count ? 0.4 : 1)
                .accessibilityLabel("Enviar respostas do questionário")
            }
        }
        .accessibilityElement(children: .contain)
    }

    private func perguntaView(_ pergunta: PerguntaForm) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(pergunta.secao.uppercased())
                .font(.system(size: 8.5, weight: .bold, design: .monospaced))
                .tracking(1)
                .foregroundStyle(.tertiary)
            Text(pergunta.titulo)
                .font(.system(size: 11.5, weight: .semibold))
            if let instrucao = pergunta.instrucao {
                Text(instrucao)
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
            }
            VStack(spacing: 4) {
                ForEach(pergunta.opcoes) { opcao in
                    opcaoView(opcao, em: pergunta)
                }
            }
        }
    }

    private func opcaoView(_ opcao: OpcaoForm, em pergunta: PerguntaForm) -> some View {
        let marcada = store.marcada(opcao, em: pergunta)
        return Button {
            store.alternarOpcao(opcao, em: pergunta)
        } label: {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                // Caixa (múltipla) ou círculo (única) — ícone + estado no rótulo.
                Image(systemName: pergunta.multipla
                      ? (marcada ? "checkmark.square.fill" : "square")
                      : (marcada ? "largecircle.fill.circle" : "circle"))
                    .font(.system(size: 11))
                    .foregroundStyle(marcada ? .orange : .secondary)
                VStack(alignment: .leading, spacing: 1) {
                    Text(opcao.rotulo)
                        .font(.system(size: 11, weight: .medium))
                    if let descricao = opcao.descricao {
                        Text(descricao)
                            .font(.system(size: 9.5))
                            .foregroundStyle(.secondary)
                    }
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 9)
            .padding(.vertical, 6)
            .background(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .fill(.white.opacity(marcada ? 0.10 : 0.04))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .strokeBorder(marcada ? Color.orange.opacity(0.45) : .clear, lineWidth: 1)
            )
            .contentShape(RoundedRectangle(cornerRadius: 8))
        }
        .buttonStyle(.plain)
        .focusable()
        .accessibilityLabel(
            "\(opcao.rotulo)\(opcao.descricao.map { ", \($0)" } ?? ""), \(marcada ? "marcada" : "desmarcada")")
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
