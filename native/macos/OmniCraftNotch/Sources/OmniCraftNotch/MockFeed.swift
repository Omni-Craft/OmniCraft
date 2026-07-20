import Foundation

// MARK: - Cenários mockados (sem backend: tudo nasce aqui)

enum MockScenario: String, CaseIterable, Identifiable {
    case tresAtivasUmaAguardando
    case soAtivas
    case vazio
    case degradado
    case contagensIlegiveis
    case uso
    case falha
    case multiplosPedidos

    var id: String { rawValue }

    var label: String {
        switch self {
        case .tresAtivasUmaAguardando: "1 · Três ativas, uma aguardando"
        case .soAtivas: "2 · Só ativas, sem atenção"
        case .vazio: "3 · Vazio"
        case .degradado: "4 · Degradado (piso ≥)"
        case .contagensIlegiveis: "5 · Contagens ilegíveis"
        case .uso: "6 · Uso (com e sem teto)"
        case .falha: "7 · Falha"
        case .multiplosPedidos: "8 · Múltiplos pedidos"
        }
    }
}

enum MockFeed {
    // IDs fixos para o snapshot ser estável entre leituras do mesmo cenário.
    private static let ids: [String] = (0..<12).map { "mock-\($0)" }

    // Utilidades locais mockadas (painel "tudo a um clique"; ações só logam).
    static let servidores: [ServidorLocal] = [
        ServidorLocal(id: "srv1", nome: "vapor api", url: "http://127.0.0.1:8080", rodando: true),
        ServidorLocal(id: "srv2", nome: "site (vite)", url: "http://127.0.0.1:5173", rodando: false),
    ]

    static let comandos: [ComandoSalvo] = [
        ComandoSalvo(id: "cmd1", rotulo: "portão", comando: "bash scripts/ci/gate.sh"),
        ComandoSalvo(id: "cmd2", rotulo: "testes", comando: "swift test"),
        ComandoSalvo(id: "cmd3", rotulo: "release", comando: "swift build -c release"),
    ]

    static let atalhos: [AtalhoLocal] = [
        AtalhoLocal(id: "at1", rotulo: "config do agente", icone: "gearshape"),
        AtalhoLocal(id: "at2", rotulo: "skills", icone: "sparkles"),
        AtalhoLocal(id: "at3", rotulo: "logs", icone: "doc.text.magnifyingglass"),
    ]

    static func snapshot(for scenario: MockScenario) -> FeedSnapshot {
        switch scenario {
        case .tresAtivasUmaAguardando:
            FeedSnapshot(
                counts: .exact(active: 3, waiting: 1),
                sessions: [
                    AgentSession(
                        id: ids[0], title: "corrigir flake do CI", state: .aguardandoVoce,
                        runnerOnline: true, host: "macbook",
                        requests: [AttentionRequest(
                            id: ids[8], title: "Aprovação necessária",
                            question: "Permitir rodar `git push` no repositório?")],
                        diffMais: 12, diffMenos: 5),
                    AgentSession(id: ids[1], title: "migrar módulo de auth", state: .emExecucao,
                                 runnerOnline: true, host: "macbook",
                                 usage: Usage(spentUSD: 0.42),
                                 ferramentaAtual: "Bash · npm test",
                                 diffMais: 58, diffMenos: 3),
                    AgentSession(id: ids[2], title: "escrever testes do parser", state: .emExecucao,
                                 runnerOnline: true, host: "mac-mini",
                                 usage: Usage(spentUSD: 1.10),
                                 ferramentaAtual: "Edit · parser.spec.ts"),
                    AgentSession(id: ids[3], title: "refatorar camada de rede", state: .emExecucao,
                                 runnerOnline: true, host: "macbook"),
                ],
                janelasLimite: [
                    JanelaLimite(id: "5h", rotulo: "5 h", fracaoUsada: 0.52, reset: "reseta em 2 h 05"),
                    JanelaLimite(id: "7d", rotulo: "7 d", fracaoUsada: 0.18, reset: "renova qua 18:48"),
                ])

        case .soAtivas:
            FeedSnapshot(
                counts: .exact(active: 2, waiting: 0),
                sessions: [
                    AgentSession(id: ids[0], title: "gerar changelog da release", state: .emExecucao,
                                 runnerOnline: true, host: "macbook",
                                 usage: Usage(spentUSD: 0.08)),
                    AgentSession(id: ids[1], title: "atualizar dependências", state: .emExecucao,
                                 runnerOnline: true, host: "mac-mini"),
                ])

        case .vazio:
            FeedSnapshot(counts: .exact(active: 0, waiting: 0), sessions: [])

        case .degradado:
            FeedSnapshot(
                counts: .floor(active: 3, waiting: 0),
                sessions: [
                    AgentSession(id: ids[0], title: "indexar repositório", state: .emExecucao,
                                 runnerOnline: true, host: "macbook",
                                 ferramentaAtual: "Grep · TODO"),
                    AgentSession(id: ids[1], title: "sessão sem telemetria", state: .desconhecido,
                                 runnerOnline: nil, host: nil),
                    AgentSession(id: ids[2], title: "compilar targets watchOS", state: .emExecucao,
                                 runnerOnline: true, host: "mac-mini"),
                ],
                janelasLimite: [
                    JanelaLimite(id: "5h", rotulo: "5 h", fracaoUsada: 0.52, reset: "reseta em 2 h 05"),
                    // Janela ilegível: fração desconhecida → — (nunca barra inventada).
                    JanelaLimite(id: "7d", rotulo: "7 d", fracaoUsada: nil, reset: nil),
                ])

        case .contagensIlegiveis:
            FeedSnapshot(
                counts: .unavailable,
                sessions: [
                    AgentSession(id: ids[0], title: "sessão parcialmente lida", state: .desconhecido,
                                 runnerOnline: nil, host: nil),
                ])

        case .uso:
            FeedSnapshot(
                counts: .exact(active: 3, waiting: 0),
                sessions: [
                    AgentSession(id: ids[0], title: "gasto com teto (barra)", state: .emExecucao,
                                 runnerOnline: true, host: "macbook",
                                 usage: Usage(spentUSD: 7.80, capUSD: 10.00)),
                    AgentSession(id: ids[1], title: "gasto sem teto (só valor)", state: .emExecucao,
                                 runnerOnline: true, host: "macbook",
                                 usage: Usage(spentUSD: 3.25)),
                    AgentSession(id: ids[2], title: "sem dado de uso", state: .emExecucao,
                                 runnerOnline: true, host: "mac-mini"),
                ])

        case .falha:
            FeedSnapshot(
                counts: .exact(active: 1, waiting: 0),
                sessions: [
                    AgentSession(id: ids[0], title: "deploy do backend", state: .falhou,
                                 runnerOnline: false, host: "mac-mini",
                                 usage: Usage(spentUSD: 0.91)),
                    AgentSession(id: ids[1], title: "lint do monorepo", state: .emExecucao,
                                 runnerOnline: true, host: "macbook"),
                ])

        case .multiplosPedidos:
            // Pedidos de SESSÕES DIFERENTES: a fila de atenção é global.
            FeedSnapshot(
                counts: .exact(active: 1, waiting: 2),
                sessions: [
                    AgentSession(
                        id: ids[0], title: "corrigir flake do CI", state: .aguardandoVoce,
                        runnerOnline: true, host: "macbook",
                        requests: [
                            AttentionRequest(id: ids[8], title: "Aprovação necessária",
                                             question: "Permitir rodar `git push` no repositório?"),
                            AttentionRequest(id: ids[9], title: "Aprovação necessária",
                                             question: "Instalar o pacote `swift-log` como dependência?"),
                        ],
                        diffMais: 12, diffMenos: 5),
                    AgentSession(
                        id: ids[1], title: "documentar API pública", state: .aguardandoVoce,
                        runnerOnline: true, host: "macbook",
                        requests: [
                            AttentionRequest(id: ids[10], title: "Aprovação necessária",
                                             question: "Sobrescrever o arquivo `Package.resolved`?"),
                        ]),
                    AgentSession(id: ids[2], title: "otimizar queries", state: .emExecucao,
                                 runnerOnline: true, host: "mac-mini",
                                 ferramentaAtual: "Bash · explain analyze"),
                ])
        }
    }
}
