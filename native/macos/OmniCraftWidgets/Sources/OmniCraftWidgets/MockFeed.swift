import Foundation

// MARK: - Cenários mockados (sem backend: tudo nasce aqui)

enum CenarioWidgets: String, CaseIterable, Identifiable {
    case streaming
    case aguardandoPermissao
    case subagentes
    case uso
    case ferramentas
    case tarefas
    case boardMigrando
    case degradado
    case vazio
    case extremos

    var id: String { rawValue }

    var label: String {
        switch self {
        case .streaming: "1 · Transcript em streaming"
        case .aguardandoPermissao: "2 · Aguardando permissão"
        case .subagentes: "3 · Orquestrador + subagentes"
        case .uso: "4 · Uso (teto · sem teto · sem dado)"
        case .ferramentas: "5 · Ferramentas (exec/ok/falha)"
        case .tarefas: "6 · Tarefas"
        case .boardMigrando: "7 · Board cheio (migração)"
        case .degradado: "8 · Degradado (≥ e desconhecido)"
        case .vazio: "9 · Vazio"
        case .extremos: "10 · Extremos"
        }
    }
}

enum MockFeed {
    // Janelas de limite compartilhadas pelos cenários com dado de uso.
    static let janelasPadrao: [JanelaLimite] = [
        JanelaLimite(id: "5h", rotulo: "5 h", fracaoUsada: 0.52, reset: "reseta em 2 h 05"),
        JanelaLimite(id: "7d", rotulo: "7 d", fracaoUsada: 0.18, reset: "renova qua 18:48"),
    ]

    // Servidores locais mockados (widget Servidores; ações visuais/log).
    static let servidores: [ServidorLocal] = [
        ServidorLocal(id: "srv1", nome: "API Vapor", host: "localhost:8080",
                      framework: "Vapor", projeto: "app-mobile", uptime: "há 40 min",
                      rodando: true),
        ServidorLocal(id: "srv2", nome: "Site", host: "localhost:5173",
                      framework: "Vite", projeto: "devcraft-site", uptime: "há 1 h 10",
                      rodando: true),
        ServidorLocal(id: "srv3", nome: "Docs", host: "localhost:4321",
                      framework: "Astro", projeto: "devcraft-site", uptime: nil,
                      rodando: false),
        ServidorLocal(id: "srv4", nome: "Prisma Studio", host: "localhost:5555",
                      framework: "prisma-studio", projeto: "notas-app", uptime: "há 1 h 40",
                      rodando: true, principal: false),
    ]

    // Rotas do agente (widget Rotas, estilo painel Routes).
    static let rotas: [RotaLocal] = [
        RotaLocal(id: "rt1", rotulo: "Skills", icone: "sparkles", corNome: "laranja"),
        RotaLocal(id: "rt2", rotulo: "Config", icone: "gearshape", corNome: "azul"),
        RotaLocal(id: "rt3", rotulo: "Hooks", icone: "link", corNome: "laranja"),
        RotaLocal(id: "rt4", rotulo: "Logs", icone: "doc.text", corNome: "cinza"),
        RotaLocal(id: "rt5", rotulo: "MCP", icone: "puzzlepiece.extension", corNome: "azul"),
        RotaLocal(id: "rt6", rotulo: "Sessões", icone: "tray.full", corNome: "verde"),
        RotaLocal(id: "rt7", rotulo: "Raiz", icone: "house", corNome: "cinza"),
        RotaLocal(id: "rt8", rotulo: "Plugins", icone: "powerplug", corNome: "azul"),
    ]

    // MARK: sessão base reutilizada

    private static func refCI(_ estado: EstadoSessao = .emExecucao) -> SessaoRef {
        SessaoRef(id: "s-ci", titulo: "corrigir flake do CI", projeto: "OmniCraft",
                  agente: "fucho", estado: estado, haQuantoTempo: "há 2 min")
    }

    private static let transcriptBase: [EntradaTranscript] = [
        EntradaTranscript(id: "t1", autor: .voce,
                          conteudo: .texto("O teste de integração do CI está intermitente, investiga e corrige."),
                          hora: "14:28"),
        EntradaTranscript(id: "t2", autor: .agente,
                          conteudo: .texto("Vou reproduzir a falha localmente primeiro para isolar a causa."),
                          hora: "14:29"),
        EntradaTranscript(id: "t3", autor: nil,
                          conteudo: .ferramenta(BlocoFerramenta(
                            id: "b1", nome: "Bash", alvo: "npm test",
                            detalhe: "297 passed, 1 flaky (timeout em auth.spec.ts:88)")),
                          hora: "14:30"),
        EntradaTranscript(id: "t4", autor: nil,
                          conteudo: .ferramenta(BlocoFerramenta(
                            id: "b2", nome: "Read", alvo: "auth.spec.ts",
                            detalhe: "218 linhas lidas")),
                          hora: "14:31"),
    ]

    private static let ferramentasBase: [ChamadaFerramenta] = [
        ChamadaFerramenta(id: "f1", nome: "Bash", alvo: "npm test",
                          duracao: "42 s", estado: .concluida),
        ChamadaFerramenta(id: "f2", nome: "Read", alvo: "auth.spec.ts",
                          duracao: "0,1 s", estado: .concluida),
        ChamadaFerramenta(id: "f3", nome: "Edit", alvo: "auth.spec.ts — timeout 5s → 15s",
                          duracao: nil, estado: .executando),
    ]

    private static let tarefasBase: [Tarefa] = [
        Tarefa(id: "k1", titulo: "Reproduzir a falha localmente", estado: .concluida),
        Tarefa(id: "k2", titulo: "Isolar o teste intermitente", estado: .concluida),
        Tarefa(id: "k3", titulo: "Corrigir o timeout do auth.spec", estado: .emAndamento),
        Tarefa(id: "k4", titulo: "Rodar a suíte 10× para confirmar", estado: .pendente),
        Tarefa(id: "k5", titulo: "Abrir PR com a correção", estado: .pendente),
    ]

    // MARK: snapshot por cenário

    static func snapshot(for cenario: CenarioWidgets) -> SnapshotWidgets {
        switch cenario {
        case .streaming:
            var transcript = transcriptBase
            transcript.append(EntradaTranscript(
                id: "t5", autor: .agente,
                conteudo: .texto("Encontrei: o mock do relógio não é resetado entre os casos, então"),
                hora: "14:32", emStreaming: true))
            return SnapshotWidgets(sessoes: [
                SessaoDetalhe(ref: refCI(), transcript: transcript,
                              ferramentas: ferramentasBase,
                              uso: UsoSessao(gastoUSD: 3.25, tokensEntrada: 12_400,
                                             tokensSaida: 8_920, tokensCacheLeitura: 86_000,
                                             tokensCacheCriacao: 14_200),
                              tarefas: tarefasBase),
                SessaoDetalhe(ref: SessaoRef(id: "s-docs", titulo: "documentar API pública",
                                             projeto: "OmniCraft", agente: "escriba",
                                             estado: .emExecucao, haQuantoTempo: "há 8 min")),
            ])

        case .aguardandoPermissao:
            var ref = refCI(.aguardandoVoce)
            ref.motivoAtencao = "Permitir rodar `git push` no repositório?"
            return SnapshotWidgets(sessoes: [
                SessaoDetalhe(ref: ref, transcript: transcriptBase,
                              ferramentas: ferramentasBase,
                              uso: UsoSessao(gastoUSD: 3.25),
                              tarefas: tarefasBase),
                SessaoDetalhe(ref: SessaoRef(id: "s-lint", titulo: "lint do monorepo",
                                             projeto: "devcraft-site", agente: "faxina",
                                             estado: .emExecucao, haQuantoTempo: "há 1 min")),
            ])

        case .subagentes:
            let filhos = [
                Subagente(id: "sa1", nome: "explorador", tarefa: "mapear os módulos afetados",
                          estado: .concluida, haQuantoTempo: "há 6 min"),
                Subagente(id: "sa2", nome: "cirurgião", tarefa: "aplicar a correção no auth",
                          estado: .aguardandoVoce, haQuantoTempo: "há 40 s",
                          filhos: [
                            Subagente(id: "sa2a", nome: "testador", tarefa: "rodar a suíte após cada edit",
                                      estado: .emExecucao, haQuantoTempo: "há 30 s"),
                            Subagente(id: "sa2b", nome: "revisor", tarefa: "conferir efeitos colaterais",
                                      estado: .ocioso, haQuantoTempo: "há 4 min"),
                          ]),
                Subagente(id: "sa3", nome: "documentador", tarefa: "atualizar o changelog",
                          estado: .emExecucao, haQuantoTempo: "há 2 min"),
            ]
            return SnapshotWidgets(sessoes: [
                SessaoDetalhe(ref: refCI(), transcript: transcriptBase,
                              subagentes: filhos, tarefas: tarefasBase),
            ])

        case .uso:
            return SnapshotWidgets(sessoes: [
                SessaoDetalhe(ref: SessaoRef(id: "s-teto", titulo: "com teto (barra)",
                                             projeto: "OmniCraft", agente: "fucho",
                                             estado: .emExecucao, haQuantoTempo: "há 3 min"),
                              uso: UsoSessao(gastoUSD: 7.80, tetoUSD: 10.00,
                                             tokensEntrada: 42_000, tokensSaida: 15_000,
                                             tokensCacheLeitura: 876_000, tokensCacheCriacao: 40_500)),
                SessaoDetalhe(ref: SessaoRef(id: "s-semteto", titulo: "sem teto (só valor)",
                                             projeto: "OmniCraft", agente: "fucho",
                                             estado: .emExecucao, haQuantoTempo: "há 5 min"),
                              uso: UsoSessao(gastoUSD: 3.25, tokensEntrada: 12_400,
                                             tokensSaida: 8_920)),
                SessaoDetalhe(ref: SessaoRef(id: "s-semdado", titulo: "sem dado nenhum",
                                             projeto: "OmniCraft", agente: "fucho",
                                             estado: .emExecucao, haQuantoTempo: "há 1 min"),
                              uso: nil),
            ], janelasLimite: janelasPadrao)

        case .ferramentas:
            var ferramentas = ferramentasBase
            ferramentas.append(ChamadaFerramenta(
                id: "f4", nome: "Bash", alvo: "git push origin fix/flake-ci",
                duracao: "0,8 s", estado: .falhou,
                primeiraLinhaErro: "remote: Permission to omnicraft/ci.git denied to runner."))
            return SnapshotWidgets(sessoes: [
                SessaoDetalhe(ref: refCI(), transcript: transcriptBase,
                              ferramentas: ferramentas, tarefas: tarefasBase),
            ])

        case .tarefas:
            let tarefas = [
                Tarefa(id: "q1", titulo: "Ler o design da feature", estado: .concluida),
                Tarefa(id: "q2", titulo: "Modelar a entidade no domínio", estado: .concluida),
                Tarefa(id: "q3", titulo: "Escrever os testes do caso de uso", estado: .concluida),
                Tarefa(id: "q4", titulo: "Implementar o repositório SwiftData", estado: .emAndamento),
                Tarefa(id: "q5", titulo: "Ligar a ViewModel na View", estado: .pendente),
                Tarefa(id: "q6", titulo: "Rodar o portão de qualidade", estado: .pendente),
                Tarefa(id: "q7", titulo: "Commit e arquivar a change", estado: .pendente),
            ]
            return SnapshotWidgets(sessoes: [
                SessaoDetalhe(ref: SessaoRef(id: "s-feat", titulo: "feature de lembretes",
                                             projeto: "app-mobile", agente: "fucho",
                                             estado: .emExecucao, haQuantoTempo: "há 12 min"),
                              tarefas: tarefas),
            ])

        case .boardMigrando:
            return SnapshotWidgets(sessoes: [
                SessaoDetalhe(ref: SessaoRef(id: "m1", titulo: "migrar módulo de auth",
                                             projeto: "OmniCraft", agente: "fucho",
                                             estado: .emExecucao, haQuantoTempo: "há 4 min",
                                             subestado: "executando ferramenta")),
                // Esta é a sessão que MIGRA de coluna (o store alterna o estado dela).
                SessaoDetalhe(ref: SessaoRef(id: "m-migra", titulo: "corrigir flake do CI",
                                             projeto: "OmniCraft", agente: "fucho",
                                             estado: .emExecucao, haQuantoTempo: "agora")),
                SessaoDetalhe(ref: SessaoRef(id: "m2", titulo: "escrever testes do parser",
                                             projeto: "devcraft-site", agente: "testador",
                                             estado: .emExecucao, haQuantoTempo: "há 9 min",
                                             subestado: "compactando · 45 s")),
                SessaoDetalhe(ref: SessaoRef(id: "m3", titulo: "atualizar dependências",
                                             projeto: "app-mobile", agente: "faxina",
                                             estado: .aguardandoVoce,
                                             motivoAtencao: "Instalar `swift-log` como dependência?",
                                             haQuantoTempo: "há 3 min")),
                SessaoDetalhe(ref: SessaoRef(id: "m4", titulo: "deploy do backend",
                                             projeto: "OmniCraft", agente: "entregador",
                                             estado: .falhou, haQuantoTempo: "há 18 min")),
                SessaoDetalhe(ref: SessaoRef(id: "m5", titulo: "gerar changelog da release",
                                             projeto: "app-mobile", agente: "escriba",
                                             estado: .concluida, haQuantoTempo: "há 31 min")),
                SessaoDetalhe(ref: SessaoRef(id: "m6", titulo: "auditoria de segredos",
                                             projeto: "OmniCraft", agente: "vigia",
                                             estado: .concluida, haQuantoTempo: "há 1 h")),
                SessaoDetalhe(ref: SessaoRef(id: "m7", titulo: "parada sem pedir nada",
                                             projeto: "devcraft-site", agente: "fucho",
                                             estado: .ocioso,
                                             motivoAtencao: "parada há 25 min sem pedir nada",
                                             haQuantoTempo: "há 25 min")),
            ])

        case .degradado:
            return SnapshotWidgets(
                sessoes: [
                    SessaoDetalhe(ref: SessaoRef(id: "d1", titulo: "indexar repositório",
                                                 projeto: "OmniCraft", agente: "fucho",
                                                 estado: .emExecucao, haQuantoTempo: "há 2 min")),
                    // Regra 3: estado não resolvido aparece como desconhecido, nunca some.
                    SessaoDetalhe(ref: SessaoRef(id: "d2", titulo: "sessão sem telemetria",
                                                 projeto: "OmniCraft", agente: "?",
                                                 estado: .desconhecido,
                                                 motivoAtencao: "estado não resolvido",
                                                 haQuantoTempo: nil),
                                  uso: nil),
                ],
                contagensSaoPiso: true,
                janelasLimite: [
                    JanelaLimite(id: "5h", rotulo: "5 h", fracaoUsada: 0.52, reset: "reseta em 2 h 05"),
                    JanelaLimite(id: "7d", rotulo: "7 d", fracaoUsada: nil, reset: nil),
                ])

        case .vazio:
            return SnapshotWidgets(sessoes: [])

        case .extremos:
            let tituloGigante = "reorganizar-completamente-a-arquitetura-de-sincronizacao-offline-first-do-monorepo-sem-quebrar-nenhum-cliente-legado"
            var transcript = transcriptBase
            for i in 0..<40 {
                transcript.append(EntradaTranscript(
                    id: "tx\(i)", autor: i.isMultiple(of: 2) ? .agente : .voce,
                    conteudo: .texto("Mensagem longa nº \(i) para forçar rolagem e testar o auto-scroll preso no fim da lista."),
                    hora: "15:\(String(format: "%02d", i % 60))"))
            }
            var ferramentas: [ChamadaFerramenta] = []
            for i in 0..<30 {
                ferramentas.append(ChamadaFerramenta(
                    id: "fx\(i)", nome: i.isMultiple(of: 3) ? "Bash" : "Edit",
                    alvo: "alvo demorado nº \(i) com um caminho comprido/demais/para/uma/linha/só.swift",
                    duracao: "\(i) s", estado: i == 7 ? .falhou : .concluida,
                    primeiraLinhaErro: i == 7
                        ? "error: a mensagem de erro é absurdamente comprida e precisa ser truncada em exatamente uma linha sem stack trace nem quebra"
                        : nil))
            }
            return SnapshotWidgets(sessoes: [
                SessaoDetalhe(ref: SessaoRef(id: "x1", titulo: tituloGigante,
                                             projeto: "monorepo-legado-profundo", agente: "fucho",
                                             estado: .emExecucao, haQuantoTempo: "há 3 h"),
                              transcript: transcript,
                              ferramentas: ferramentas,
                              uso: UsoSessao(gastoUSD: 42.10, tetoUSD: 50,
                                             tokensEntrada: 2_400_000, tokensSaida: 890_000,
                                             tokensCacheLeitura: 12_000_000, tokensCacheCriacao: 340_000),
                              tarefas: (0..<14).map {
                                  Tarefa(id: "kx\($0)", titulo: "Tarefa nº \($0) — \(tituloGigante)",
                                         estado: $0 < 6 ? .concluida : ($0 == 6 ? .emAndamento : .pendente))
                              }),
            ])
        }
    }
}
