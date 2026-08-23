"""A estrategia. E A UNICA PASTA QUE O AGENTE PODE ALTERAR.

O contrato e estreito de proposito: recebes barras e parametros, devolves um
sinal por barra. Nao ves retornos, nao ves metricas, nao decides a janela de
tempo. Tudo isso vive no arnes, fora do teu alcance — nao por desconfianca do
agente em particular, mas porque um otimizador que pode mexer na regua acaba
sempre por mexer na regua.
"""
from .sinal import gerar_sinais
from .risco import tamanho_posicao

__all__ = ["gerar_sinais", "tamanho_posicao"]
