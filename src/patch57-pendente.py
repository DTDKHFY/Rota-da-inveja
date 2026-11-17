import sys
p = sys.argv[1]; s = open(p).read()
def rep(old, new, n=1):
    global s
    c = s.count(old)
    if c != n: raise SystemExit(f"esperava {n}, achei {c}: {old[:90]!r}")
    s = s.replace(old, new)
# 1. folha da missão: abre sempre no topo (título inteiro)
rep("""function MapSheet({ title, kicker, color, onClose, children, backLabel }) {
  return ReactDOM.createPortal(""", """function MapSheet({ title, kicker, color, onClose, children, backLabel }) {
  const body = useRef(null);
  useEffect(() => { const t = setTimeout(() => { if (body.current) body.current.scrollTop = 0; }, 60); return () => clearTimeout(t); }, []);
  return ReactDOM.createPortal(""")
rep("""      <div role="dialog" aria-label={title} style={{ position: "fixed", left: 0, right: 0, bottom: 0, zIndex: 59, maxWidth: 640, margin: "0 auto", maxHeight: "86vh", overflowY: "auto",""",
    """      <div ref={body} role="dialog" aria-label={title} style={{ position: "fixed", left: 0, right: 0, bottom: 0, zIndex: 59, maxWidth: 640, margin: "0 auto", maxHeight: "84vh", overflowY: "auto", overflowAnchor: "none",""")
# 2. campo de hora com rótulo; textos certos
rep('"Monte os blocos em Editar agenda"', '"Adicione o primeiro objetivo logo abaixo"')
rep('"Nenhum bloco hoje"', '"Nenhum objetivo hoje"')
rep("""        <input id="ag-new-time" type="time" aria-label="Hora do objetivo" value={t} onChange={(e) => setT(e.target.value)} style={{ ...inputStyle, flex: "0 0 104px", colorScheme: T.light ? "light" : "dark" }} />""",
"""        <label style={{ flex: "0 0 112px", position: "relative", display: "block" }}>
          {!t && <span aria-hidden="true" style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", fontSize: 14, color: T.muted, pointerEvents: "none", background: T.panel2, paddingRight: 30 }}>🕐 Hora</span>}
          <input id="ag-new-time" type="time" aria-label="Hora do objetivo" value={t} onChange={(e) => setT(e.target.value)} style={{ ...inputStyle, width: "100%", boxSizing: "border-box", colorScheme: T.light ? "light" : "dark" }} />
        </label>""")
# 3. árvore no zoom: sem camada borrada depois que a câmera chega
rep(""".rb svg{will-change:transform}""", """.rb.rb-moving svg{will-change:transform}""")
rep("""      if (cam.anim) { cam.anim.onfinish = null; try { cam.anim.cancel(); } catch (e) { /* já acabou */ } cam.anim = null; }""",
"""      if (cam.anim) { cam.anim.onfinish = null; try { cam.anim.cancel(); } catch (e) { /* já acabou */ } cam.anim = null; }
      try { (svg.getAnimations ? svg.getAnimations() : []).forEach((a) => a.cancel()); } catch (e) { /* sem suporte */ }
      svg.style.transform = "";""")
# 4. mapa: etiqueta do mês sem ponto sobrando
rep("""{c.short} {c.goals.length ? `${c.done}/${c.goals.length}` : "·"}""", """{c.short}{c.goals.length ? ` ${c.done}/${c.goals.length}` : ""}""")
# 5. atributos: nome numa coluna fixa, botões quebram alinhados entre si
rep("""        <div key={a} style={{ display: "flex", gap: 5, flexWrap: "wrap", alignItems: "center" }}>
          <span style={{ fontSize: 12, color: ATTR_HUE[a], fontWeight: 700, minWidth: compact ? 74 : 84 }}>{ATTR_INFO[a].icon} {a}</span>
          {(SUBS[a] || []).map(([id, name]) => {""", """        <div key={a} style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
          <span style={{ fontSize: 12, color: ATTR_HUE[a], fontWeight: 700, flex: `0 0 ${compact ? 84 : 92}px`, paddingTop: compact ? 5 : 7 }}>{ATTR_INFO[a].icon} {a}</span>
          <div style={{ flex: 1, minWidth: 0, display: "flex", gap: 5, flexWrap: "wrap" }}>
          {(SUBS[a] || []).map(([id, name]) => {""")
rep("""                style={{ border: `1px solid ${on ? ATTR_HUE[a] : T.border}`, background: on ? `${ATTR_HUE[a]}26` : "transparent", color: on ? ATTR_HUE[a] : T.text, padding: compact ? "4px 8px" : "6px 9px", fontSize: compact ? 11 : 13, cursor: "pointer", fontFamily: F.ui }}>{name}</button>
            );
          })}
        </div>""", """                style={{ border: `1px solid ${on ? ATTR_HUE[a] : T.border}`, background: on ? `${ATTR_HUE[a]}26` : "transparent", color: on ? ATTR_HUE[a] : T.text, padding: compact ? "4px 8px" : "6px 9px", fontSize: compact ? 11 : 13, cursor: "pointer", fontFamily: F.ui }}>{name}</button>
            );
          })}
          </div>
        </div>""")
# 6. Caixa: botão + menor e o aviso de renda com espaço para ele
rep("""bottom: "calc(72px + env(safe-area-inset-bottom, 0px))", width: 62, height: 62, borderRadius: "50%\"""", """bottom: "calc(70px + env(safe-area-inset-bottom, 0px))", width: 54, height: 54, borderRadius: "50%\"""")
open(p, "w").write(s); print("ok")
