import Foundation

// ============================================================================
// Quem está escutando nesta máquina, de verdade.
//
// A aba "servidores" mostrava fixtures: portas que ninguém abriu, projetos que
// não existem, e um botão de parar ao lado. Aqui a lista vem do `lsof`, e o que
// não dá para descobrir fica `nil` — a UI já sabe desenhar "—".
// ============================================================================

/// Um processo escutando numa porta TCP.
struct Ouvinte: Equatable {
    var pid: Int32
    var comando: String
    var porta: Int
}

/// Lê os ouvintes locais com `lsof`, e o dono de cada porta com `ps`.
struct ServidorDetector {
    /// `lsof` pode ficar preso num mount de rede; a aba abre sem ele.
    private static let timeout: TimeInterval = 2.0

    /// Portas de servidor de desenvolvimento nunca são privilegiadas, e as
    /// efêmeras (o lado de saída de uma conexão) não são serviço nenhum.
    private static let faixaUtil = 1024..<49152

    /// Frameworks reconhecidos pelo nome do processo ou pela linha de comando.
    /// A ordem importa: `next` antes de `node`, porque um é caso do outro.
    private static let frameworks: [(agulha: String, rotulo: String)] = [
        ("vite", "Vite"), ("next", "Next.js"), ("astro", "Astro"), ("nuxt", "Nuxt"),
        ("remix", "Remix"), ("webpack", "webpack"), ("storybook", "Storybook"),
        ("prisma", "Prisma Studio"), ("uvicorn", "Uvicorn"), ("gunicorn", "Gunicorn"),
        ("hypercorn", "Hypercorn"), ("flask", "Flask"), ("django", "Django"),
        ("rails", "Rails"), ("puma", "Puma"), ("vapor", "Vapor"), ("cargo", "Cargo"),
        ("air", "Air"), ("php", "PHP"), ("python", "Python"), ("node", "Node"),
        ("bun", "Bun"), ("deno", "Deno"), ("ruby", "Ruby"), ("java", "Java"),
    ]

    /// O que está escutando agora.
    ///
    /// - Returns: A lista, ou `nil` quando o `lsof` não respondeu — que é
    ///   diferente de "ninguém está escutando" e a UI precisa distinguir.
    func detectar() -> [ServidorLocal]? {
        guard let saida = executar("/usr/sbin/lsof", ["-nP", "-iTCP", "-sTCP:LISTEN", "-Fpcn"])
        else { return nil }

        let ouvintes = analisar(saida)
        let porPid = Dictionary(grouping: ouvintes, by: \.pid)
        let meuPid = ProcessInfo.processInfo.processIdentifier

        return porPid.compactMap { pid, portas -> ServidorLocal? in
            // A própria ilha e o servidor do OmniCraft não são "seu servidor".
            guard pid != meuPid, let menor = portas.map(\.porta).min() else { return nil }
            let comando = portas.first?.comando ?? "?"
            let detalhe = linhaDeComando(pid)
            // Um daemon do sistema roda a partir de "/": isso não é projeto
            // nenhum, e "—" diz mais que uma barra solta.
            let cwd = diretorioDeTrabalho(pid).flatMap { $0 == "/" ? nil : $0 }
            return ServidorLocal(
                id: "pid-\(pid)-\(menor)",
                nome: nome(comando: comando, cwd: cwd),
                host: "localhost:\(menor)",
                framework: framework(comando: comando, linha: detalhe),
                projeto: cwd.map { ($0 as NSString).lastPathComponent },
                uptime: idade(pid),
                rodando: true,
                // "Principal" é o que dá para atribuir a um projeto seu; o
                // resto (daemons do sistema, apps) vai para outros ouvintes.
                principal: cwd.map { $0.hasPrefix(NSHomeDirectory()) } ?? false,
                pid: pid)
        }
        .sorted { ($0.principal ? 0 : 1, $0.host) < ($1.principal ? 0 : 1, $1.host) }
    }

    /// Manda SIGTERM para o dono da porta.
    ///
    /// - Returns: `nil` quando o sinal foi entregue, ou o motivo da recusa.
    func parar(_ servidor: ServidorLocal) -> String? {
        guard let pid = servidor.pid else { return "sem processo conhecido" }
        if kill(pid, SIGTERM) == 0 { return nil }
        return errno == EPERM ? "sem permissão para encerrar" : "o processo já não existe"
    }

    // MARK: - Leitura

    /// A saída `-F` do lsof vem em linhas de um caractere: `p` abre um
    /// processo, `c` nomeia o comando, `n` é cada endereço dele.
    func analisar(_ saida: String) -> [Ouvinte] {
        var ouvintes: [Ouvinte] = []
        var pid: Int32?
        var comando = "?"
        for linha in saida.split(separator: "\n") {
            let corpo = String(linha.dropFirst())
            switch linha.first {
            case "p": pid = Int32(corpo)
            case "c": comando = corpo
            case "n":
                guard let pid, let porta = porta(de: corpo), Self.faixaUtil.contains(porta) else {
                    continue
                }
                ouvintes.append(Ouvinte(pid: pid, comando: comando, porta: porta))
            default: continue
            }
        }
        return ouvintes
    }

    /// `*:5173` · `127.0.0.1:8080` · `[::1]:3000` — a porta é o que vem depois
    /// dos dois-pontos finais.
    private func porta(de endereco: String) -> Int? {
        guard let corte = endereco.lastIndex(of: ":") else { return nil }
        return Int(endereco[endereco.index(after: corte)...])
    }

    private func nome(comando: String, cwd: String?) -> String {
        cwd.map { ($0 as NSString).lastPathComponent } ?? comando
    }

    private func framework(comando: String, linha: String?) -> String? {
        let agulha = (comando + " " + (linha ?? "")).lowercased()
        return Self.frameworks.first { agulha.contains($0.agulha) }?.rotulo
    }

    private func linhaDeComando(_ pid: Int32) -> String? {
        executar("/bin/ps", ["-o", "command=", "-p", "\(pid)"])?
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func diretorioDeTrabalho(_ pid: Int32) -> String? {
        // `-a` faz o filtro ser E, não OU: só o cwd deste processo.
        guard let saida = executar("/usr/sbin/lsof", ["-a", "-d", "cwd", "-Fn", "-p", "\(pid)"])
        else { return nil }
        return saida.split(separator: "\n")
            .first { $0.hasPrefix("n") }
            .map { String($0.dropFirst()) }
    }

    /// "há 40 min" a partir do tempo decorrido que o `ps` reporta.
    private func idade(_ pid: Int32) -> String? {
        guard let bruto = executar("/bin/ps", ["-o", "etime=", "-p", "\(pid)"])?
            .trimmingCharacters(in: .whitespacesAndNewlines),
            let segundos = Self.segundos(deEtime: bruto)
        else { return nil }
        if segundos < 60 { return "há \(segundos) s" }
        if segundos < 3600 { return "há \(segundos / 60) min" }
        let horas = segundos / 3600
        let minutos = (segundos % 3600) / 60
        return minutos == 0 ? "há \(horas) h" : "há \(horas) h \(String(format: "%02d", minutos))"
    }

    /// `MM:SS` · `HH:MM:SS` · `D-HH:MM:SS` — o formato do `ps`.
    static func segundos(deEtime bruto: String) -> Int? {
        let partes = bruto.split(separator: "-")
        let dias = partes.count == 2 ? Int(partes[0]) ?? 0 : 0
        let relogio = (partes.last?.split(separator: ":") ?? []).compactMap { Int($0) }
        guard (2...3).contains(relogio.count) else { return nil }
        let segundosDoDia =
            relogio.count == 3
            ? relogio[0] * 3600 + relogio[1] * 60 + relogio[2]
            : relogio[0] * 60 + relogio[1]
        return dias * 86400 + segundosDoDia
    }

    private func executar(_ caminho: String, _ args: [String]) -> String? {
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
        guard grupo.wait(timeout: .now() + Self.timeout) == .success else {
            processo.terminate()
            return nil
        }
        return String(data: dados, encoding: .utf8)
    }
}
