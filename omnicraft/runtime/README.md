# runtime

O motor de execução — como um agente **roda**. Dado um spec e a entrada do usuário, ele conduz o loop de raciocínio do agente: invocando LLMs, chamando ferramentas, gerenciando skills e produzindo respostas.

O runtime é uma biblioteca, não um serviço. O servidor é o seu host principal, mas ele também pode ser usado diretamente para desenvolvimento local, embutido em outras aplicações, ou invocado a partir de testes.
