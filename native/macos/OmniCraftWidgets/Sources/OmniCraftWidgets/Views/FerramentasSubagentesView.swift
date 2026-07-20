import SwiftUI

// MARK: - Widget Ferramentas

struct FerramentasView: View {
    @Environment(WidgetStore.self) private var store
    @State private var soErros = false

    var body: some View {
        if let sessao = store.sessaoSelecionada, !sessao.ferramentas.isEmpty {
            conteudo(sessao)
        } else {
            VazioView(icone: "wrench.and.screwdriver", texto: "Nenhuma ferramenta chamada ainda")
        }
    }

    private func conteudo(_ sessao: SessaoDetalhe) -> some View {
        let visiveis = soErros
            ? sessao.ferramentas.filter { $0.estado == .falhou }
            : sessao.ferramentas

        return VStack(alignment: .leading, spacing: 0) {
            Picker("Filtro", selection: $soErros) {
                Text("todas").tag(false)
                Text("só erros").tag(true)
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .controlSize(.small)
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .accessibilityLabel("Filtro de ferramentas")

            if visiveis.isEmpty {
                VazioView(icone: "checkmark.circle", texto: "Nenhum erro — tudo verde")
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 8) {
                        ForEach(visiveis) { chamada in
                            linha(chamada)
                        }
                    }
                    .padding(.horizontal, 12)
                    .padding(.bottom, 12)
                }
            }
        }
    }

    private func linha(_ chamada: ChamadaFerramenta) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Image(systemName: glifoFerramenta(chamada.nome))
                    .font(.system(size: 9))
                    .foregroundStyle(.tertiary)
                    .frame(width: 12)
                Text(chamada.nome)
                    .font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                Text(chamada.alvo)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Spacer(minLength: 8)
                Text(chamada.duracao ?? "—")
                    .font(.system(size: 9.5, design: .monospaced))
                    .foregroundStyle(.tertiary)
                    .fixedSize()
                BadgeEstado(chamada.estado)
            }
            if let erro = chamada.primeiraLinhaErro {
                // SÓ a primeira linha do erro — sem stack trace.
                Text(erro)
                    .font(.system(size: 9.5, design: .monospaced))
                    .foregroundStyle(.red)
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .padding(.leading, 18)
            }
        }
        .padding(.vertical, 3)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(
            "\(chamada.nome) \(chamada.alvo), \(chamada.estado.label), duração \(chamada.duracao ?? "desconhecida")"
            + (chamada.primeiraLinhaErro.map { ". Erro: \($0)" } ?? ""))
    }
}

// MARK: - Widget Subagentes

struct SubagentesView: View {
    @Environment(WidgetStore.self) private var store

    var body: some View {
        if let sessao = store.sessaoSelecionada, !sessao.subagentes.isEmpty {
            ScrollView {
                VStack(alignment: .leading, spacing: 6) {
                    // Quem precisa de atenção sobe — com ícone E cor, nunca só cor.
                    ForEach(ordenados(sessao.subagentes)) { agente in
                        no(agente, nivel: 0)
                    }
                }
                .padding(12)
            }
        } else {
            VazioView(icone: "point.3.filled.connected.trianglepath.dotted",
                      texto: "Nenhum subagente nesta sessão")
        }
    }

    private func ordenados(_ agentes: [Subagente]) -> [Subagente] {
        agentes.sorted { a, b in
            if a.precisaAtencao != b.precisaAtencao { return a.precisaAtencao }
            return false
        }
    }

    @ViewBuilder
    private func no(_ agente: Subagente, nivel: Int) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                if nivel > 0 {
                    Image(systemName: "arrow.turn.down.right")
                        .font(.system(size: 8))
                        .foregroundStyle(.tertiary)
                }
                VStack(alignment: .leading, spacing: 1) {
                    HStack(spacing: 6) {
                        if agente.precisaAtencao {
                            Image(systemName: "bell.fill")
                                .font(.system(size: 9))
                                .foregroundStyle(.orange)
                        }
                        Text(agente.nome)
                            .font(.system(size: 11, weight: .bold, design: .monospaced))
                        Text(agente.haQuantoTempo)
                            .font(.system(size: 9, design: .monospaced))
                            .foregroundStyle(.tertiary)
                    }
                    Text(agente.tarefa)
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.tail)
                }
                Spacer(minLength: 8)
                BadgeEstado(agente.estado)
            }
            .padding(8)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .fill(agente.precisaAtencao ? Color.orange.opacity(0.12) : .clear)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .strokeBorder(agente.precisaAtencao ? Color.orange.opacity(0.5) : .clear,
                                  lineWidth: 1)
            )
            .padding(.leading, CGFloat(nivel) * 18)  // um nível de indentação basta
            .accessibilityElement(children: .combine)
            .accessibilityLabel(
                "\(agente.nome), \(agente.tarefa), \(agente.estado.label), \(agente.haQuantoTempo)"
                + (agente.precisaAtencao ? ". Precisa de atenção" : ""))

            ForEach(agente.filhos) { filho in
                AnyView(no(filho, nivel: min(nivel + 1, 1)))
            }
        }
    }
}
