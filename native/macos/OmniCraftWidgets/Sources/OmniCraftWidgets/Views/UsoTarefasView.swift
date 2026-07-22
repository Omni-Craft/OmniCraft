import SwiftUI

// MARK: - Widget Uso

/// Regras do notch valem idênticas: barra SÓ com gasto E teto ("orçamento do
/// agente"); sem teto, valor em texto; sem dado, — (nunca 0).
struct UsoView: View {
    @Environment(WidgetStore.self) private var store

    var body: some View {
        if let sessao = store.sessaoSelecionada {
            conteudo(sessao.uso)
        } else {
            VazioView(icone: "gauge.with.needle", texto: "Nenhuma sessão selecionada")
        }
    }

    @ViewBuilder
    private func conteudo(_ uso: UsoSessao?) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if !store.snapshot.janelasLimite.isEmpty {
                    janelasLimite(store.snapshot.janelasLimite)
                }
                gasto(uso)
                if let uso, temAlgumToken(uso) {
                    detalhamentoTokens(uso)
                }
            }
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    /// Janelas de rate-limit do provedor ("5 h · 52% · reseta em 2 h 05") —
    /// denominador REAL, então a barra é legítima; ilegível vira —.
    private func janelasLimite(_ janelas: [JanelaLimite]) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Janelas de limite")
                .font(.system(size: 10, weight: .bold, design: .monospaced))
                .tracking(1)
                .foregroundStyle(.secondary)
                .textCase(.uppercase)
            ForEach(janelas) { janela in
                HStack(spacing: 8) {
                    Text(janela.rotulo)
                        .font(.system(size: 10, weight: .bold, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .frame(width: 26, alignment: .leading)
                    if let fracao = janela.fracaoUsada {
                        GeometryReader { geo in
                            ZStack(alignment: .leading) {
                                Capsule().fill(.quaternary)
                                Capsule()
                                    .fill(corBarra(fracao))
                                    .frame(width: max(geo.size.width * fracao, 3))
                            }
                        }
                        .frame(height: 3)
                        Text("\(Int(fracao * 100))%")
                            .font(.system(size: 9.5, weight: .bold, design: .monospaced))
                            .foregroundStyle(corBarra(fracao))
                            .frame(width: 30, alignment: .trailing)
                    } else {
                        Text("— janela ilegível")
                            .font(.system(size: 9.5, design: .monospaced))
                            .foregroundStyle(.secondary)
                        Spacer(minLength: 0)
                    }
                    Text(janela.reset ?? "—")
                        .font(.system(size: 9.5, design: .monospaced))
                        .foregroundStyle(.tertiary)
                        .fixedSize()
                }
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(janela.fracaoUsada.map {
                    "Janela de \(janela.rotulo): \(Int($0 * 100)) por cento usado. \(janela.reset ?? "")"
                } ?? "Janela de \(janela.rotulo): ilegível")
            }
        }
    }

    @ViewBuilder
    private func gasto(_ uso: UsoSessao?) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Gasto")
                .font(.system(size: 10, weight: .bold, design: .monospaced))
                .tracking(1)
                .foregroundStyle(.secondary)
                .textCase(.uppercase)

            if let fracao = uso?.fracao, let uso {
                // Com teto: chip de %, barra progressiva e rótulo "orçamento do agente"
                // (estilo "5H 52% used" da referência — aqui o denominador EXISTE).
                HStack(spacing: 8) {
                    Text("\(Int(fracao * 100))% usado")
                        .font(.system(size: 10, weight: .bold, design: .monospaced))
                        .foregroundStyle(corBarra(fracao))
                    Spacer(minLength: 8)
                    Text("\(Fmt.usd(uso.gastoUSD)) / \(Fmt.usd(uso.tetoUSD))")
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
                GeometryReader { geo in
                    ZStack(alignment: .leading) {
                        Capsule().fill(.quaternary)
                        Capsule()
                            .fill(corBarra(fracao))
                            .frame(width: max(geo.size.width * fracao, 3))
                    }
                }
                .frame(height: 4)
                Text("orçamento do agente")
                    .font(.system(size: 9.5, design: .monospaced))
                    .foregroundStyle(.tertiary)
                    .accessibilityLabel(
                        "Gasto de \(Fmt.usd(uso.gastoUSD)), \(Int(fracao * 100)) por cento do orçamento do agente de \(Fmt.usd(uso.tetoUSD))")
            } else if let gasto = uso?.gastoUSD {
                // Sem teto: valor em texto, nunca barra (sem denominador não há %).
                Text(Fmt.usd(gasto))
                    .font(.system(size: 20, weight: .bold, design: .monospaced))
                Text("sem teto")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .accessibilityLabel("Gasto de \(Fmt.usd(gasto)), sem teto conhecido")
            } else {
                // Sem dado nenhum.
                Text("—")
                    .font(.system(size: 20, weight: .bold, design: .monospaced))
                    .foregroundStyle(.secondary)
                Text("sem dado de uso")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
        }
        .accessibilityElement(children: .combine)
    }

    private func detalhamentoTokens(_ uso: UsoSessao) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Tokens")
                .font(.system(size: 10, weight: .bold, design: .monospaced))
                .tracking(1)
                .foregroundStyle(.secondary)
                .textCase(.uppercase)
            Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 4) {
                linhaToken("entrada", uso.tokensEntrada)
                linhaToken("saída", uso.tokensSaida)
                linhaToken("cache (leitura)", uso.tokensCacheLeitura)
                linhaToken("cache (criação)", uso.tokensCacheCriacao)
            }
        }
    }

    private func linhaToken(_ rotulo: String, _ valor: Int?) -> some View {
        GridRow {
            Text(rotulo)
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(.secondary)
            Text(Fmt.tokens(valor))
                .font(.system(size: 10, weight: .semibold, design: .monospaced))
                .gridColumnAlignment(.trailing)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Tokens de \(rotulo): \(Fmt.tokens(valor))")
    }

    private func temAlgumToken(_ uso: UsoSessao) -> Bool {
        uso.tokensEntrada != nil || uso.tokensSaida != nil
            || uso.tokensCacheLeitura != nil || uso.tokensCacheCriacao != nil
    }

    private func corBarra(_ fracao: Double) -> Color {
        switch fracao {
        case ..<0.6: .green
        case ..<0.85: .orange
        default: .red
        }
    }
}

// MARK: - Widget Tarefas

struct TarefasView: View {
    @Environment(WidgetStore.self) private var store

    var body: some View {
        if let sessao = store.sessaoSelecionada, !sessao.tarefas.isEmpty {
            conteudo(sessao.tarefas)
        } else {
            VazioView(icone: "checklist", texto: "Nenhuma tarefa neste turno")
        }
    }

    private func conteudo(_ tarefas: [Tarefa]) -> some View {
        let concluidas = tarefas.filter { $0.estado == .concluida }.count
        return VStack(alignment: .leading, spacing: 0) {
            Text("\(concluidas) de \(tarefas.count)")
                .font(.system(size: 10.5, weight: .bold, design: .monospaced))
                .foregroundStyle(.secondary)
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .accessibilityLabel("\(concluidas) de \(tarefas.count) tarefas concluídas")

            ScrollView {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(tarefas) { tarefa in
                        linha(tarefa)
                    }
                }
                .padding(.horizontal, 12)
                .padding(.bottom, 12)
            }
        }
    }

    private func linha(_ tarefa: Tarefa) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: tarefa.estado.symbolName)
                .font(.system(size: 10))
                .foregroundStyle(cor(tarefa.estado))
            Text(tarefa.titulo)
                .font(.system(size: 10.5, design: .monospaced)
                    .weight(tarefa.estado == .emAndamento ? .bold : .regular))
                .foregroundStyle(tarefa.estado == .concluida ? .secondary : .primary)
                .strikethrough(tarefa.estado == .concluida, color: .secondary)
                .lineLimit(2)
            Spacer(minLength: 0)
        }
        .padding(.vertical, 5)
        .padding(.horizontal, 8)
        .background(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(tarefa.estado == .emAndamento ? Color.green.opacity(0.12) : .clear)
        )
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(tarefa.titulo), \(tarefa.estado.label)")
    }

    private func cor(_ estado: EstadoTarefa) -> Color {
        switch estado {
        case .pendente: .secondary
        case .emAndamento: .green
        case .concluida: .green
        }
    }
}
