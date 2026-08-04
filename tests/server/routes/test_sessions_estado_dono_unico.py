"""
Guarda da divisão do ``sessions.py``: o estado tem dono único.

Os caches de sessão vivem em ``_sessions/estado.py`` e são REEXPORTADOS pela
fachada ``sessions.py``. O risco da divisão não é import quebrado — esse falha
alto na hora. É o silencioso: alguém reatribuir um cache num dos módulos, e aí
passam a existir dois dicionários com o mesmo nome. Metade do código escreve
num, metade lê do outro, e a sessão aparece ativa por um caminho e ociosa pelo
outro, sem erro nenhum.

Estes testes prendem as duas propriedades que impedem isso: mesma identidade
nos dois caminhos de import, e mutação num lado visível do outro.

Ver ``docs/plano-divisao-sessions.md``.
"""

from __future__ import annotations

import omnicraft.server.routes.sessions as fachada
from omnicraft.server.routes._sessions import estado


def _nomes_de_estado() -> list[str]:
    """
    Os símbolos públicos-para-o-fork que ``estado.py`` exporta.

    :returns: Nomes privados (um underscore) definidos no módulo de estado.
    """
    return [n for n in dir(estado) if n.startswith("_") and not n.startswith("__")]


def test_estado_nao_esta_vazio() -> None:
    """O próprio teste precisa de um alvo — um módulo vazio passaria à toa."""
    assert len(_nomes_de_estado()) >= 20


def test_fachada_reexporta_todo_o_estado() -> None:
    """Nenhum nome pode sumir do ``sessions.py`` por causa da mudança de casa.

    Os testes deste repositório importam 82 símbolos privados de
    ``sessions.py``; a divisão só é segura enquanto todos continuarem lá.
    """
    ausentes = [n for n in _nomes_de_estado() if not hasattr(fachada, n)]
    assert ausentes == [], f"sumiram da fachada: {ausentes}"


def test_fachada_e_estado_compartilham_o_MESMO_objeto() -> None:
    """Identidade, não igualdade: dois dicionários iguais seriam o bug."""
    divergentes = [n for n in _nomes_de_estado() if getattr(fachada, n) is not getattr(estado, n)]
    assert divergentes == [], f"a fachada tem cópia, não o objeto: {divergentes}"


def test_mutacao_por_um_caminho_e_visivel_pelo_outro() -> None:
    """A prova de comportamento, não só de identidade.

    Escreve pela fachada e lê pelo módulo de estado — é exatamente o que o
    código de produção faz, já que umas rotas vão importar de um lado e outras
    do outro conforme as fatias seguintes forem saindo.
    """
    chave = "conv_teste_dono_unico"
    try:
        fachada._session_status_cache[chave] = "running"
        assert estado._session_status_cache.get(chave) == "running"

        estado._session_status_cache[chave] = "idle"
        assert fachada._session_status_cache.get(chave) == "idle"
    finally:
        estado._session_status_cache.pop(chave, None)
