# omnicraft-client

SDK cliente Python para a API do servidor
[omnicraft](https://github.com/omnicraft-ai/omnicraft).

O `omnicraft-client` é um cliente tipado para conduzir sessões do omnicraft
pela API HTTP + SSE do servidor — criando sessões, enviando turnos e fazendo
streaming das respostas. Ele compartilha os tipos `StreamEvent` /
`SessionStreamEventType` que o servidor emite, então os envelopes recebidos em
streaming são validados contra uma única fonte de verdade.

Ele é publicado em conjunto com o pacote principal `omnicraft`, na mesma
versão:

```bash
pip install omnicraft-client
```

Veja o [repositório do omnicraft](https://github.com/omnicraft-ai/omnicraft)
para a documentação completa.
