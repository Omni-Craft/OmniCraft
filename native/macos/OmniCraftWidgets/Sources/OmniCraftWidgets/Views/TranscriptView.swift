import SwiftUI

/// Transcript da sessão: mensagens em ordem, blocos de ferramenta colapsados,
/// auto-scroll preso no fim que pausa quando a pessoa rola para cima.
struct TranscriptView: View {
    @Environment(WidgetStore.self) private var store
    @Environment(\.accessibilityReduceMotion) private var reduzirMovimento

    @State private var presoNoFim = true
    @State private var blocosExpandidos: Set<String> = []

    private let marcadorFim = "fim-do-transcript"

    var body: some View {
        if let sessao = store.sessaoSelecionada, !sessao.transcript.isEmpty {
            conteudo(sessao)
        } else {
            VazioView(icone: "text.bubble", texto: "Nenhuma conversa nesta sessão")
        }
    }

    private func conteudo(_ sessao: SessaoDetalhe) -> some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(sessao.transcript) { entrada in
                        linha(entrada)
                    }
                    Color.clear.frame(height: 1).id(marcadorFim)
                }
                .padding(12)
                .background(
                    GeometryReader { geo in
                        Color.clear.preference(
                            key: FimVisivelKey.self,
                            value: geo.frame(in: .named("scrollTranscript")).maxY)
                    }
                )
            }
            .coordinateSpace(name: "scrollTranscript")
            .background(
                GeometryReader { geo in
                    Color.clear.preference(key: AlturaViewportKey.self, value: geo.size.height)
                }
            )
            .onPreferenceChange(FimVisivelKey.self) { maxY in
                // Rolou para cima (fim fora do viewport + margem) → pausa o auto-scroll.
                presoNoFim = maxY <= alturaViewport + 48
            }
            .onPreferenceChange(AlturaViewportKey.self) { alturaViewport = $0 }
            .onChange(of: sessao.transcript) {
                guard presoNoFim else { return }
                if reduzirMovimento {
                    proxy.scrollTo(marcadorFim, anchor: .bottom)
                } else {
                    withAnimation(.easeOut(duration: 0.2)) {
                        proxy.scrollTo(marcadorFim, anchor: .bottom)
                    }
                }
            }
            .overlay(alignment: .bottom) {
                if !presoNoFim {
                    Button {
                        presoNoFim = true
                        withAnimation(reduzirMovimento ? nil : .easeOut(duration: 0.25)) {
                            proxy.scrollTo(marcadorFim, anchor: .bottom)
                        }
                    } label: {
                        Label("voltar ao fim", systemImage: "arrow.down.to.line")
                            .font(.system(size: 10.5, weight: .semibold))
                            .padding(.horizontal, 10)
                            .padding(.vertical, 5)
                            .background(.thinMaterial, in: Capsule())
                            .overlay(Capsule().strokeBorder(.separator.opacity(0.5), lineWidth: 1))
                    }
                    .buttonStyle(.plain)
                    .padding(.bottom, 10)
                    .transition(.opacity)
                    .focusable()
                    .accessibilityLabel("Voltar ao fim da conversa")
                }
            }
            .animation(.easeOut(duration: 0.15), value: presoNoFim)
        }
    }

    @State private var alturaViewport: CGFloat = 0

    @ViewBuilder
    private func linha(_ entrada: EntradaTranscript) -> some View {
        switch entrada.conteudo {
        case let .texto(texto):
            VStack(alignment: .leading, spacing: 3) {
                HStack {
                    Text(entrada.autor?.label ?? "")
                        .font(.system(size: 9, weight: .bold, design: .monospaced))
                        .tracking(0.5)
                        .foregroundStyle(entrada.autor == .voce ? Color.green : .secondary)
                        .textCase(.uppercase)
                    Spacer()
                    Text(entrada.hora)
                        .font(.system(size: 9, design: .monospaced))
                        .foregroundStyle(.tertiary)
                }
                (Text(texto) + Text(entrada.emStreaming ? " ▌" : "")
                    .foregroundStyle(.secondary))
                    .font(.system(size: 11, design: .monospaced))
                    .fixedSize(horizontal: false, vertical: true)
                    .opacity(1)
            }
            .accessibilityElement(children: .combine)
            .accessibilityLabel("\(entrada.autor?.label ?? ""), \(entrada.hora): \(texto)\(entrada.emStreaming ? " (em streaming)" : "")")

        case let .ferramenta(bloco):
            blocoFerramenta(bloco, hora: entrada.hora)
        }
    }

    private func blocoFerramenta(_ bloco: BlocoFerramenta, hora: String) -> some View {
        let expandido = blocosExpandidos.contains(bloco.id)
        return VStack(alignment: .leading, spacing: 4) {
            Button {
                if expandido { blocosExpandidos.remove(bloco.id) }
                else { blocosExpandidos.insert(bloco.id) }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: expandido ? "chevron.down" : "chevron.right")
                        .font(.system(size: 8, weight: .bold))
                        .foregroundStyle(.secondary)
                    Text("\(bloco.nome): \(bloco.alvo)")
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                    Spacer()
                    Text(hora)
                        .font(.system(size: 9.5).monospacedDigit())
                        .foregroundStyle(.tertiary)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .focusable()
            .accessibilityLabel("Ferramenta \(bloco.nome), \(bloco.alvo). \(expandido ? "Expandido" : "Colapsado")")

            if expandido, let detalhe = bloco.detalhe {
                Text(detalhe)
                    .font(.system(size: 10.5, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .padding(8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 6))
            }
        }
        .padding(.vertical, 2)
    }

    private struct FimVisivelKey: PreferenceKey {
        static var defaultValue: CGFloat = 0
        static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = nextValue() }
    }

    private struct AlturaViewportKey: PreferenceKey {
        static var defaultValue: CGFloat = 0
        static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = nextValue() }
    }
}
