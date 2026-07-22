import Foundation

// MARK: - Detecção local (sem servidor, sem hooks, sem conta)

/// Descobre sessões de agente lendo a própria máquina: `ps` acha o processo do
/// agente **preso a um TTY** (sessão de terminal; headless/background fica de
/// fora), `lsof` mapeia o processo para o transcript que ele mantém aberto (ou,
/// no caso do Claude Code, para o diretório de trabalho) e o transcript `.jsonl`
/// fornece os metadados.
///
/// A regra que define a fonte: **uma sessão É um processo de agente vivo**. A
/// vivacidade vem do sistema operacional, nunca da data do arquivo — o mtime só
/// distingue *trabalhando* de *ocioso* dentro de uma sessão já viva.
///
/// Honestidade (mesma regra do resto do app): o que não dá para saber daqui
/// fica `nil` e vira `—` na tela. Custo, teto e pedidos de aprovação **não** são
/// observáveis localmente, então não são inventados — quem tem isso é o feed do
/// servidor.
actor LocalDetector {

    /// Tipo de agente reconhecido.
    enum Agente: String {
        case claude
        case codex

        var rotulo: String {
            switch self {
            case .claude: "Claude Code"
            case .codex: "Codex"
            }
        }
    }

    /// Janela em que uma escrita no transcript conta como "trabalhando agora".
    /// Escrita de transcript é intermitente: o agente pode parecer em execução
    /// por até ~30 s depois do fim real do turno. É o preço de não usar hooks.
    private static let janelaAtividade: TimeInterval = 30

    /// Sessão parada há mais que isto não é mais mostrada.
    private static let idadeMaxima: TimeInterval = 6 * 3600

    private static let psTimeout: TimeInterval = 2.0
    private static let lsofTimeout: TimeInterval = 2.0

    private let fm = FileManager.default

    // MARK: Entrada única

    /// Um retrato do que está rodando agora. `nil` = não deu para ler a máquina
    /// (ps/lsof falharam) — o chamador mostra "contagens indisponíveis", nunca
    /// uma lista velha como se fosse o agora.
    func detectar() -> FeedSnapshot? {
        guard let processos = descobrirProcessos() else { return nil }

        let agora = Date()
        var sessoes: [AgentSession] = []

        for processo in processos {
            guard let transcript = processo.transcript ?? transcriptMaisRecente(paraCwd: processo.cwd) else {
                // Processo vivo mas sem transcript legível: a sessão existe, e
                // omiti-la seria mentir por omissão. Entra sem metadados.
                sessoes.append(AgentSession(
                    id: processo.chave,
                    title: nome(deCwd: processo.cwd) ?? processo.agente.rotulo,
                    state: .desconhecido,
                    runnerOnline: true,
                    host: Self.hostLocal,
                    ferramentaAtual: processo.agente.rotulo
                ))
                continue
            }

            let info = lerCauda(de: transcript)
            let ultimaAtividade = info.atividade ?? modificado(transcript) ?? agora
            guard agora.timeIntervalSince(ultimaAtividade) < Self.idadeMaxima else { continue }

            let trabalhando = agora.timeIntervalSince(ultimaAtividade) < Self.janelaAtividade
            let subagentes = contarSubagentes(doTranscript: transcript, agora: agora)

            sessoes.append(AgentSession(
                id: transcript,
                title: nome(deCwd: processo.cwd) ?? nome(deTranscript: transcript),
                state: trabalhando ? .emExecucao : .ocioso,
                runnerOnline: true,      // o processo está vivo — isto nós sabemos
                host: Self.hostLocal,
                usage: Usage(),          // custo/teto não são observáveis daqui → "—"
                requests: [],            // pedido de aprovação idem: nunca inventado
                ferramentaAtual: [processo.agente.rotulo, info.modelo]
                    .compactMap { $0?.isEmpty == false ? $0 : nil }
                    .joined(separator: " · "),
                subestado: subagentes > 0
                    ? (subagentes == 1 ? "1 subagente" : "\(subagentes) subagentes")
                    : nil,
                atualizadoHa: Self.idade(de: ultimaAtividade, ate: agora)
            ))
        }

        // Rota 2: transcripts escritos agora há pouco. O Claude Code de desktop
        // (Claude.app) não é um processo por sessão nem mantém o `.jsonl`
        // aberto — a rota do processo não o enxerga. Aqui a vivacidade é
        // INFERIDA da escrita recente, então `runnerOnline` fica nulo (vira "—"):
        // sabemos que a sessão escreveu agora, não que o processo está vivo.
        let jaVistos = Set(sessoes.map(\.id))
        sessoes.append(contentsOf: escanearTranscriptsRecentes(agora: agora, ignorando: jaVistos))

        // Trabalhando primeiro, depois mais recentes.
        sessoes.sort { a, b in
            if (a.state == .emExecucao) != (b.state == .emExecucao) { return a.state == .emExecucao }
            return a.title.localizedCaseInsensitiveCompare(b.title) == .orderedAscending
        }

        let ativas = sessoes.filter { $0.state == .emExecucao }.count
        // "aguardando" fica em 0 porque a espera por você não é detectável
        // localmente — o número é exato para o que esta fonte consegue ver.
        return FeedSnapshot(counts: .exact(active: ativas, waiting: 0), sessions: sessoes)
    }

    // MARK: Descoberta de processos

    private struct ProcessoVivo {
        let agente: Agente
        let transcript: String?
        let cwd: String?
        let chave: String
    }

    private func descobrirProcessos() -> [ProcessoVivo]? {
        guard let psOut = executar("/bin/ps", ["-Ao", "pid=,ppid=,tty=,command="], timeout: Self.psTimeout)
        else { return nil }

        var candidatos: [(pid: String, tty: String, agente: Agente)] = []
        for linha in psOut.split(whereSeparator: \.isNewline) {
            let partes = linha.trimmingCharacters(in: .whitespacesAndNewlines)
                .split(maxSplits: 3, whereSeparator: \.isWhitespace)
            guard partes.count == 4 else { continue }
            let pid = String(partes[0])
            let tty = String(partes[2])
            let comando = String(partes[3]).trimmingCharacters(in: .whitespacesAndNewlines)
            // Sem TTY = sessão headless/serviço: não é alguém trabalhando num terminal.
            guard tty != "??", !comando.isEmpty else { continue }
            if ehClaude(comando) { candidatos.append((pid, tty, .claude)) }
            else if ehCodex(comando) { candidatos.append((pid, tty, .codex)) }
        }
        guard !candidatos.isEmpty else { return [] }

        let blocos = lsofPorPid(candidatos.map(\.pid))
        var resultado: [ProcessoVivo] = []
        var reivindicados = Set<String>()

        for (pid, tty, agente) in candidatos {
            let bloco = blocos[pid] ?? ""
            let cwd = diretorioDeTrabalho(em: bloco)

            // Subagentes do Claude rodam em .claude/worktrees/agent-*/ — são
            // detalhe da sessão-mãe, não sessões próprias.
            if agente == .claude, let cwd, cwd.contains("/.claude/worktrees/agent-") { continue }

            let transcript: String?
            switch agente {
            case .claude: transcript = melhorTranscriptClaude(em: bloco, cwd: cwd)
            case .codex:  transcript = melhorTranscriptCodex(em: bloco)
            }

            // Uma sessão por terminal: a chave evita listar o mesmo trabalho duas vezes.
            let chave = "\(agente.rawValue):\(transcript ?? cwd ?? tty)"
            guard reivindicados.insert(chave).inserted else { continue }
            resultado.append(ProcessoVivo(agente: agente, transcript: transcript, cwd: cwd, chave: chave))
        }
        return resultado
    }

    private func ehClaude(_ comando: String) -> Bool {
        let c = comando.lowercased()
        if c.contains("/.local/bin/claude") { return true }
        guard let primeiro = c.split(separator: " ").first.map(String.init) else { return false }
        return primeiro == "claude" || primeiro.hasSuffix("/claude")
    }

    private func ehCodex(_ comando: String) -> Bool {
        let c = comando.lowercased()
        guard let primeiro = c.split(separator: " ").first.map(String.init) else { return false }
        return primeiro == "codex" || primeiro.hasSuffix("/codex") || c.contains("/codex/codex")
    }

    /// Um `lsof` para todos os pids de uma vez; a saída `-Fn` é fatiada nos
    /// marcadores `p<pid>` (um processo por bloco).
    private func lsofPorPid(_ pids: [String]) -> [String: String] {
        guard !pids.isEmpty,
              let saida = executar("/usr/sbin/lsof",
                                   ["-a", "-p", pids.joined(separator: ","), "-Fn"],
                                   timeout: Self.lsofTimeout)
        else { return [:] }

        var blocos: [String: String] = [:]
        var pidAtual: String?
        var acumulado = ""
        for linha in saida.split(whereSeparator: \.isNewline) {
            if linha.first == "p" {
                if let p = pidAtual { blocos[p] = acumulado }
                pidAtual = String(linha.dropFirst())
                acumulado = ""
            } else {
                acumulado += linha + "\n"
            }
        }
        if let p = pidAtual { blocos[p] = acumulado }
        return blocos
    }

    private func diretorioDeTrabalho(em bloco: String) -> String? {
        let linhas = bloco.split(whereSeparator: \.isNewline).map(String.init)
        for i in linhas.indices where linhas[i] == "fcwd" && linhas.indices.contains(i + 1) {
            let seguinte = linhas[i + 1]
            guard seguinte.first == "n" else { continue }
            let valor = String(seguinte.dropFirst()).trimmingCharacters(in: .whitespacesAndNewlines)
            if valor.hasPrefix("/") { return valor }
        }
        return nil
    }

    private func caminhos(em bloco: String, contendo fragmento: String) -> [String] {
        bloco.split(whereSeparator: \.isNewline).compactMap {
            guard $0.first == "n" else { return nil }
            let valor = String($0.dropFirst()).trimmingCharacters(in: .whitespacesAndNewlines)
            return valor.contains(fragmento) && valor.hasSuffix(".jsonl") ? valor : nil
        }
    }

    /// O Claude Code escreve e fecha o transcript, então normalmente o `lsof`
    /// não mostra `.jsonl` aberto — daí o caminho alternativo pelo cwd.
    private func melhorTranscriptClaude(em bloco: String, cwd: String?) -> String? {
        let todos = caminhos(em: bloco, contendo: "/.claude/projects/")
        if todos.count > 1, let cwd {
            let codificado = cwd.replacingOccurrences(of: "/", with: "-")
            if let preferido = todos.first(where: { $0.contains(codificado) }) { return preferido }
        }
        return todos.first
    }

    private func melhorTranscriptCodex(em bloco: String) -> String? {
        // O nome do arquivo de rollout embute o timestamp: o maior é o mais novo.
        caminhos(em: bloco, contendo: "/.codex/sessions/").max {
            URL(fileURLWithPath: $0).deletingPathExtension().lastPathComponent
                < URL(fileURLWithPath: $1).deletingPathExtension().lastPathComponent
        }
    }

    // MARK: Rota 2 — transcripts escritos agora há pouco

    /// Sessão sem processo próprio (app de desktop) só pode ser vista pelo que
    /// ela escreve. Janela curta de propósito: transcript parado há meia hora
    /// não é "um agente rodando agora", e encher a ilha de sessão velha seria
    /// ruído — as do processo (rota 1) mantêm a folga longa porque ali a
    /// vivacidade é fato, não inferência.
    private static let idadeOciosaInferida: TimeInterval = 30 * 60

    private func escanearTranscriptsRecentes(agora: Date, ignorando: Set<String>) -> [AgentSession] {
        var achadas: [AgentSession] = []
        for (caminho, agente) in transcriptsCandidatos() {
            guard !ignorando.contains(caminho) else { continue }
            guard let mtime = modificado(caminho),
                  agora.timeIntervalSince(mtime) < Self.idadeOciosaInferida else { continue }

            let info = lerCauda(de: caminho)
            let atividade = info.atividade ?? mtime
            guard agora.timeIntervalSince(atividade) < Self.idadeOciosaInferida else { continue }

            let trabalhando = agora.timeIntervalSince(atividade) < Self.janelaAtividade
            achadas.append(AgentSession(
                id: caminho,
                title: info.titulo ?? nome(deCwd: info.cwd) ?? nome(deTranscript: caminho),
                state: trabalhando ? .emExecucao : .ocioso,
                runnerOnline: nil,       // inferido da escrita: processo não confirmado → "—"
                host: Self.hostLocal,
                usage: Usage(),
                requests: [],
                ferramentaAtual: [agente.rotulo, info.modelo]
                    .compactMap { $0?.isEmpty == false ? $0 : nil }
                    .joined(separator: " · "),
                subestado: {
                    let n = contarSubagentes(doTranscript: caminho, agora: agora)
                    guard n > 0 else { return nil }
                    return n == 1 ? "1 subagente" : "\(n) subagentes"
                }(),
                atualizadoHa: Self.idade(de: atividade, ate: agora)
            ))
        }
        return achadas
    }

    /// `~/.claude/projects/<projeto>/<sessão>.jsonl` e os rollouts do Codex.
    /// Subagentes ficam em `<sessão>/subagents/` — um nível abaixo, então a
    /// varredura rasa já os deixa de fora (eles são detalhe, não sessão).
    private func transcriptsCandidatos() -> [(String, Agente)] {
        var saida: [(String, Agente)] = []

        let projetos = Self.home.appendingPathComponent(".claude/projects")
        if let pastas = try? fm.contentsOfDirectory(at: projetos, includingPropertiesForKeys: nil) {
            for pasta in pastas {
                guard let arquivos = try? fm.contentsOfDirectory(at: pasta, includingPropertiesForKeys: nil)
                else { continue }
                for arquivo in arquivos where arquivo.pathExtension == "jsonl" {
                    saida.append((arquivo.path, .claude))
                }
            }
        }

        let codex = Self.home.appendingPathComponent(".codex/sessions")
        if let e = fm.enumerator(at: codex, includingPropertiesForKeys: nil) {
            for caso in e {
                guard let url = caso as? URL, url.pathExtension == "jsonl" else { continue }
                saida.append((url.path, .codex))
            }
        }
        return saida
    }

    // MARK: Transcript pelo diretório (Claude Code)

    /// `~/.claude/projects/<cwd-codificado>/*.jsonl` mais recente.
    private func transcriptMaisRecente(paraCwd cwd: String?) -> String? {
        guard let cwd else { return nil }
        let raiz = Self.home.appendingPathComponent(".claude/projects")
        let codificado = cwd.replacingOccurrences(of: "/", with: "-")
        let pasta = raiz.appendingPathComponent(codificado)
        guard let arquivos = try? fm.contentsOfDirectory(at: pasta, includingPropertiesForKeys: [.contentModificationDateKey])
        else { return nil }
        return arquivos
            .filter { $0.pathExtension == "jsonl" }
            .compactMap { url -> (String, Date)? in
                guard let m = modificado(url.path) else { return nil }
                return (url.path, m)
            }
            .max { $0.1 < $1.1 }?.0
    }

    /// Subagentes do Claude vivem em `<sessão>/subagents/agent-*.jsonl`.
    private func contarSubagentes(doTranscript transcript: String, agora: Date) -> Int {
        let url = URL(fileURLWithPath: transcript)
        let pasta = url.deletingPathExtension().appendingPathComponent("subagents")
        guard let arquivos = try? fm.contentsOfDirectory(at: pasta, includingPropertiesForKeys: nil)
        else { return 0 }
        return arquivos.filter { arquivo in
            guard arquivo.pathExtension == "jsonl", let m = modificado(arquivo.path) else { return false }
            return agora.timeIntervalSince(m) < Self.janelaAtividade
        }.count
    }

    // MARK: Leitura do transcript

    private struct InfoTranscript {
        var modelo: String?
        var atividade: Date?
        var cwd: String?
        var titulo: String?
    }

    private static let isoParser: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    /// Lê só a cauda do `.jsonl` (transcripts crescem muito): modelo e horário
    /// da última entrada de conversa. Entradas de sistema (resumo, compactação)
    /// são manutenção — não contam como atividade.
    private func lerCauda(de caminho: String) -> InfoTranscript {
        var info = InfoTranscript()
        guard let fh = FileHandle(forReadingAtPath: caminho) else { return info }
        defer { try? fh.close() }

        let tamanho = (try? fh.seekToEnd()) ?? 0
        let aLer = UInt64(min(tamanho, 131_072))
        try? fh.seek(toOffset: tamanho - aLer)
        guard let dados = try? fh.readToEnd(), let texto = String(data: dados, encoding: .utf8)
        else { return info }

        for linha in texto.split(separator: "\n").reversed() {
            if info.modelo == nil,
               let r = linha.range(of: #""model":"([^"]+)""#, options: .regularExpression) {
                info.modelo = String(linha[r].dropFirst(9).dropLast(1))
                    .replacingOccurrences(of: "claude-", with: "")
            }
            if info.atividade == nil || info.cwd == nil || info.titulo == nil,
               let obj = try? JSONSerialization.jsonObject(with: Data(linha.utf8)) as? [String: Any] {
                if info.cwd == nil, let c = obj["cwd"] as? String, !c.isEmpty { info.cwd = c }
                if info.titulo == nil {
                    // Título dado pela pessoa vale mais que o gerado pela IA.
                    if let t = obj["customTitle"] as? String, !t.isEmpty { info.titulo = t }
                    else if let t = obj["aiTitle"] as? String, !t.isEmpty { info.titulo = t }
                }
                if info.atividade == nil {
                    if let tipo = obj["type"] as? String, tipo == "user" || tipo == "assistant",
                       let ts = obj["timestamp"] as? String {
                        info.atividade = Self.isoParser.date(from: ts)
                    } else if let payload = obj["payload"] as? [String: Any],
                              let tipo = payload["type"] as? String,
                              tipo == "user_message" || tipo == "agent_message",
                              let ts = obj["timestamp"] as? String {
                        info.atividade = Self.isoParser.date(from: ts)
                    }
                }
            }
            if info.modelo != nil && info.atividade != nil && info.cwd != nil && info.titulo != nil { break }
        }

        // Em transcript longo o modelo pode só aparecer no começo.
        if info.modelo == nil, tamanho > aLer {
            try? fh.seek(toOffset: 0)
            if let cabeca = try? fh.read(upToCount: 65_536),
               let texto = String(data: cabeca, encoding: .utf8),
               let r = texto.range(of: #""model":"([^"]+)""#, options: .regularExpression) {
                info.modelo = String(texto[r].dropFirst(9).dropLast(1))
                    .replacingOccurrences(of: "claude-", with: "")
            }
        }
        return info
    }

    // MARK: Utilidades

    private static let home = URL(fileURLWithPath: NSHomeDirectory())

    private static let hostLocal: String = {
        let nome = ProcessInfo.processInfo.hostName
        return nome.hasSuffix(".local") ? String(nome.dropLast(6)) : nome
    }()

    private func modificado(_ caminho: String) -> Date? {
        (try? fm.attributesOfItem(atPath: caminho)[.modificationDate]) as? Date
    }

    private func nome(deCwd cwd: String?) -> String? {
        guard let cwd, !cwd.isEmpty else { return nil }
        let nome = (cwd as NSString).lastPathComponent
        return nome.isEmpty ? nil : nome
    }

    /// Nome legível a partir do caminho codificado do transcript.
    private func nome(deTranscript caminho: String) -> String {
        let pasta = URL(fileURLWithPath: caminho).deletingLastPathComponent().lastPathComponent
        let ultimo = pasta.split(separator: "-").last.map(String.init)
        return ultimo?.isEmpty == false ? ultimo! : "sessão"
    }

    /// "há 12 s" · "há 4 min" · "há 2 h" — no formato do resto do app.
    private static func idade(de data: Date, ate agora: Date) -> String {
        let s = Int(max(0, agora.timeIntervalSince(data)))
        if s < 60 { return "há \(s) s" }
        if s < 3600 { return "há \(s / 60) min" }
        return "há \(s / 3600) h"
    }

    private func executar(_ caminho: String, _ args: [String], timeout: TimeInterval) -> String? {
        let processo = Process()
        processo.executableURL = URL(fileURLWithPath: caminho)
        processo.arguments = args
        let pipe = Pipe()
        processo.standardOutput = pipe
        processo.standardError = Pipe()
        do { try processo.run() } catch { return nil }

        // Ler em outra fila: pipe cheio trava o processo filho se ninguém drena.
        var dados = Data()
        let grupo = DispatchGroup()
        grupo.enter()
        DispatchQueue.global(qos: .utility).async {
            dados = pipe.fileHandleForReading.readDataToEndOfFile()
            processo.waitUntilExit()
            grupo.leave()
        }
        guard grupo.wait(timeout: .now() + timeout) == .success else {
            processo.terminate()
            return nil
        }
        return String(data: dados, encoding: .utf8)
    }
}
