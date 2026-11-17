"""Gera a versão instalável (PWA) do Sistema a partir do HTML do artefato.

- troca o Babel no navegador por JavaScript já compilado (abre bem mais rápido no celular)
- aponta manifesto e ícones para arquivos de verdade
- registra o service worker para funcionar sem internet
Uso: python3 build.py <html-do-artefato> <app-compilado.js> <pasta-destino>
"""
import json
import re
import sys

src, compiled, out = sys.argv[1], sys.argv[2], sys.argv[3]
html = open(src, encoding="utf-8").read()
js = open(compiled, encoding="utf-8").read()

# 1) script do app: JSX → JS já compilado
start = html.index('<script type="text/babel"')
end = html.index("</script>", start) + len("</script>")
html = html[:start] + "<script>\n" + js + "\n</script>" + html[end:]

# 2) sem Babel no navegador
html = re.sub(r'\s*<script src="https://cdnjs\.cloudflare\.com/ajax/libs/babel-standalone/[^"]+"></script>', "", html)

# 3) manifesto e ícones em arquivo
html = re.sub(r'<link rel="apple-touch-icon" href="data:[^"]+" />', '<link rel="apple-touch-icon" href="apple-touch-icon.png" />', html)
html = re.sub(r'<link rel="icon" type="image/png" href="data:[^"]+" />', '<link rel="icon" type="image/png" href="icon-192.png" />', html)
html = re.sub(r'<link rel="manifest" href="data:[^"]+" />', '<link rel="manifest" href="manifest.webmanifest" />', html)
assert "data:application/manifest" not in html and "babel-standalone" not in html

# 4) service worker
html = html.replace(
    "</body>",
    """<script>
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () { navigator.serviceWorker.register("sw.js").catch(function () {}); });
  }
</script>
</body>""",
)

open(f"{out}/index.html", "w", encoding="utf-8").write(html)

manifest = {
    "name": "Sistema — Rota da Inveja",
    "short_name": "Sistema",
    "description": "Missões, caixa, diário e evolução do caçador.",
    "lang": "pt-BR",
    "start_url": "./",
    "scope": "./",
    "display": "standalone",
    "orientation": "portrait",
    "background_color": "#0b0507",
    "theme_color": "#0b0507",
    "icons": [
        {"src": "icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}
open(f"{out}/manifest.webmanifest", "w", encoding="utf-8").write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
print("index.html", len(html))
