# Código-fonte do Sistema — Rota da Inveja

O site publicado é `../index.html` (versão compilada, ilegível). **Edite sempre `sistema-rota-da-inveja.html`**, nunca o index.html.

## Arquivos
- `sistema-rota-da-inveja.html` — fonte atual (v65; React 18 + JSX dentro de `<script type="text/babel">`).
- `../arvore/` — pinturas da árvore: `e01…e20.webp` (um estágio a cada 5% da rota) e miniaturas `t01…t20.webp`. Trocar uma pintura = trocar o arquivo e subir a versão do sw.js; o código não muda.
- `patch65.py` — v65: Trading no Caixa (lucro pela conta × pelas operações) e árvore em 20 estágios.
- `patch66.py` — v66: árvore viva (vento nos galhos, folhas, vaga-lumes, névoa, runas, chuva). Posições de cada pintura na tabela `TREE_FX`.
- `build.py` — gera o index.html instalável (troca o Babel por JS compilado, manifesto, ícones, service worker).
- `patch57-pendente.py` — correções pequenas aprovadas para estudo, **não publicadas**: título cortado da Missão Diária, rótulo do campo de hora, chips dos meses, zoom borrado da árvore, seletor de atributo e botão + do Caixa. Aplicar só com o ok do Rafael.

## Como montar e publicar
```bash
# 1. extrair o JSX
python3 -c "import re;s=open('src/sistema-rota-da-inveja.html',encoding='utf-8').read();open('app.jsx','w',encoding='utf-8').write(re.search(r'<script type=\"text/babel\" data-presets=\"env,react\">(.*?)</script>',s,re.S).group(1))"
# 2. compilar
npx esbuild app.jsx --loader:.jsx=jsx --target=es2019 --minify --outfile=app.min.js
# 3. montar (gera index.html e manifest.webmanifest na pasta de saída)
mkdir -p out && python3 src/build.py src/sistema-rota-da-inveja.html app.min.js out
# 4. subir a versão do cache em sw.js (sistema-vNN → vNN+1), copiar out/index.html para a raiz, commit e push
```

## Regras
- Sugerir e explicar antes de editar; esperar o ok.
- Problema é só o design: manter estrutura das telas, regras, árvore, rotas e animações.
- Os dados ficam no navegador do celular (localStorage `sistema_v1`); nunca mandar limpar sem backup.
