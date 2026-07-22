import SwiftUI

// MARK: - Widget Servidores (porta · framework · projeto · uptime)

struct ServidoresView: View {
    @Environment(WidgetStore.self) private var store

    var body: some View {
        let principais = store.servidores.filter(\.principal)
        let outros = store.servidores.filter { !$0.principal }
        if store.servidores.isEmpty {
            VazioView(icone: "server.rack", texto: "Nenhum servidor local detectado")
        } else {
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(principais) { linha($0) }
                    if !outros.isEmpty {
                        Text("OUTROS OUVINTES")
                            .font(.system(size: 8.5, weight: .bold, design: .monospaced))
                            .tracking(1)
                            .foregroundStyle(.tertiary)
                        ForEach(outros) { linha($0) }
                    }
                }
                .padding(12)
            }
        }
    }

    private func linha(_ servidor: ServidorLocal) -> some View {
        HStack(alignment: .center, spacing: 8) {
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(servidor.host)
                        .font(.system(size: 11.5, weight: .bold, design: .monospaced))
                    if let framework = servidor.framework {
                        Text("· \(framework)")
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundStyle(servidor.rodando ? .secondary : Color.red.opacity(0.8))
                    }
                    if !servidor.rodando {
                        Text("PARADO")
                            .font(.system(size: 8, weight: .bold, design: .monospaced))
                            .foregroundStyle(.red)
                    }
                }
                HStack(spacing: 4) {
                    Image(systemName: "folder")
                        .font(.system(size: 8))
                    Text(servidor.projeto ?? "—")
                        .font(.system(size: 10, design: .monospaced))
                }
                .foregroundStyle(.secondary)
            }
            Spacer(minLength: 8)
            Text(servidor.uptime ?? "—")
                .font(.system(size: 9.5, design: .monospaced))
                .foregroundStyle(.tertiary)
                .fixedSize()
            botao("arrow.up.right.square", "Abrir \(servidor.host) no navegador") {
                store.acaoServidor(servidor, "abrir")
            }
            botao("doc.on.doc", "Copiar a URL de \(servidor.nome)") {
                store.copiar(servidor.url, rotulo: servidor.nome)
            }
            if servidor.rodando {
                botao("stop.circle", "Parar o servidor \(servidor.nome)", cor: .red) {
                    store.acaoServidor(servidor, "parar")
                }
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel(
            "Servidor \(servidor.nome) em \(servidor.host), \(servidor.rodando ? "rodando \(servidor.uptime ?? "")" : "parado"), projeto \(servidor.projeto ?? "desconhecido")")
    }

    private func botao(_ simbolo: String, _ rotulo: String, cor: Color = .secondary,
                       acao: @escaping () -> Void) -> some View {
        Button(action: acao) {
            Image(systemName: simbolo)
                .font(.system(size: 11))
                .foregroundStyle(cor)
                .padding(4)
                .contentShape(Circle())
        }
        .buttonStyle(BotaoCirculoHover())
        .focusable()
        .accessibilityLabel(rotulo)
    }
}

// MARK: - Widget Rotas (grade de pastas do agente, estilo painel Routes)

struct RotasView: View {
    @Environment(WidgetStore.self) private var store

    var body: some View {
        if store.rotas.isEmpty {
            VazioView(icone: "folder", texto: "Nenhuma rota configurada")
        } else {
            ScrollView {
                LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 10), count: 4),
                          spacing: 12) {
                    ForEach(store.rotas) { rota in
                        Button {
                            store.abrirRota(rota)
                        } label: {
                            VStack(spacing: 5) {
                                ZStack {
                                    RoundedRectangle(cornerRadius: 9, style: .continuous)
                                        .fill(cor(rota.corNome).opacity(0.22))
                                        .frame(width: 40, height: 32)
                                    Image(systemName: rota.icone)
                                        .font(.system(size: 13))
                                        .foregroundStyle(cor(rota.corNome))
                                }
                                Text(rota.rotulo)
                                    .font(.system(size: 10, design: .monospaced))
                                    .foregroundStyle(.secondary)
                                    .lineLimit(1)
                                    .minimumScaleFactor(0.8)
                            }
                            .frame(maxWidth: .infinity)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .focusable()
                        .accessibilityLabel("Abrir \(rota.rotulo)")
                    }
                }
                .padding(12)
            }
        }
    }

    private func cor(_ nome: String) -> Color {
        switch nome {
        case "laranja": .orange
        case "azul": .blue
        case "verde": .green
        default: .secondary
        }
    }
}
