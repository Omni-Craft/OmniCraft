import Foundation

/// O que o mascote está dizendo — cada estado toca uma animação do pet.
///
/// É o contrato entre quem sabe o que os agentes estão fazendo (o HUD da notch,
/// os widgets) e quem desenha o bicho. Quem chama traduz o próprio estado para
/// cá; o pacote não conhece sessões, feeds nem regras de produto.
public enum EstadoMascote: Equatable, Sendable {
    case oculto        // nada para mostrar
    case atencao       // alguma sessão aguarda VOCÊ → acena
    case erro          // alguma sessão falhou
    case trabalhando   // pelo menos uma sessão em execução → coda
    case concluido     // terminou desde a última vez que você olhou → comemora
    case ocioso        // sessões existem, nenhuma trabalhando

    /// Animações do atlas, em ordem de preferência (a 1ª que existir toca).
    /// A lista cobre os dois vocabulários das folhas — o antigo (`codando`,
    /// `acenar`) e o novo (`emotes`, `dano`, `nadar_dir`) — para qualquer pet
    /// ter uma pose adequada mesmo sem o nome ideal.
    public var animacoes: [String] {
        switch self {
        case .atencao: ["acenar", "tremendo", "emotes_a", "emotes", "pensando", "idle"]
        case .erro: ["erro", "dano", "morte", "emotes_b", "expressoes", "idle"]
        case .trabalhando: ["codando", "nadar_dir", "nadar", "ataque", "frente", "pensando", "idle"]
        case .concluido: ["feliz", "comer", "emotes", "emotes_a", "acenar", "idle"]
        case .ocioso, .oculto: ["idle"]
        }
    }
}
