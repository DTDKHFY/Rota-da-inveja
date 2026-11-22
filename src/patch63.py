p = 'work63.html'
s = open(p, encoding='utf-8').read()
def rep(a, b, n=1):
    global s
    assert s.count(a) == n, (a[:90], s.count(a)); s = s.replace(a, b)

# 1. sincronização: sem base (1ª vez, aparelho novo), SOMA as duas versões em vez de escolher uma
rep('''          merged = base ? syncMerge(base, stateRef.current, doc.state, localWins) : (localWins ? stateRef.current : doc.state);''',
    '''          merged = syncMerge(base || {}, stateRef.current, doc.state, localWins);''')

# 2. progresso da rota em %: média dos objetivos finais; dentro de cada um, os marcos + o próprio final
rep('''/* a tocha anda um pouco por dia:''', '''function routeProgress(finals) {
  const fs = finals || [];
  if (!fs.length) return 0;
  const per = fs.map((f) => { if (f.done) return 1; const ms = f.milestones || []; return ms.filter((m) => m.done).length / (ms.length + 1); });
  return per.reduce((a, b) => a + b, 0) / fs.length;
}
/* a tocha anda um pouco por dia:''')

# 3. mapa: até 13 acampamentos (rota de 1 ano); tocha pelo %, ampulheta pelo tempo
rep('''  while (mk <= endM && months.length < 8) {''', '''  while (mk <= endM && months.length < 13) {''')
rep('''  const walk = mapWalk(lay, months, start, state.deadline, today);
  const torch = walk.pos;''', '''  const totalDays = Math.max(1, daysBetween(start, state.deadline) + 1);
  const rp = routeProgress(state.finals);
  const tp = today < start ? 0 : Math.min(1, (daysBetween(start, today) + 1) / totalDays);
  const torchDay = rp <= 0 ? addDays(start, -1) : addDays(start, Math.max(0, Math.round(rp * totalDays) - 1));
  const walk = mapWalk(lay, months, start, state.deadline, torchDay);
  const torch = walk.pos;
  const clock = mapWalk(lay, months, start, state.deadline, today).pos;
  const rpP = Math.round(rp * 100), tpP = Math.round(tp * 100), gap = rpP - tpP;
  const nextQ = Math.min(4, Math.floor(tp * 4) + 1);
  const nextQDate = addDays(start, Math.round((nextQ / 4) * totalDays) - 1);''')
rep('''        <div style={{ flex: 1, fontSize: 13, color: T.muted }}>Semana {weekNo} · {wDone}/{wl.length} metas · fim da rota em {daysLeft} d</div>''',
    '''        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 14, fontWeight: 700, color: T.text }}>
            Rota {rpP}% · tempo {tpP}% · <span style={{ color: gap >= 0 ? T.good : T.danger }}>{gap > 0 ? `${gap} pts à frente` : gap < 0 ? `${-gap} pts atrás` : "no ritmo"}</span>
          </div>
          <div style={{ fontSize: 12, color: T.muted, marginTop: 2 }}>Meta: {nextQ * 25}% até {fmtDM(nextQDate)} · semana {weekNo} · {wDone}/{wl.length} metas · fim em {daysLeft} d</div>
        </div>''')
rep('''          <div key={c.key} className="mp-rise" style={chipStyle(c.left ? c.x - 66 : c.x + 18, c.y - 30, { color: "#eef2f7", background: "rgba(11,5,7,.72)", animationDelay: `${0.3 + i * 0.25}s` })}>''',
    '''          <div key={c.key} className="mp-rise" style={chipStyle(c.left ? c.x - 66 : c.x + 18, c.y - (months.length > 8 ? 22 : 30), { color: "#eef2f7", background: "rgba(11,5,7,.72)", animationDelay: `${0.3 + Math.min(i, 8) * 0.2}s`, ...(months.length > 8 ? { fontSize: 10, padding: "0 4px" } : {}) })}>''')
rep('''        <div className="mp-rise" style={chipStyle(torch.x + (chipRight ? 16 : -150), torch.y + 14, { color: "#0b1a33", background: "rgba(255,246,200,.92)", animationDelay: "1.2s" })}>VOCÊ · SEMANA {weekNo} · {wDone}/{wl.length}</div>''',
    '''        {Math.hypot(clock.x - torch.x, clock.y - torch.y) > 18 && (
          <div className="mp-rise" aria-hidden="true" style={chipStyle(clock.x - 22, clock.y - 9, { color: "#e5e7eb", background: "rgba(11,5,7,.62)", border: "1px dashed #e5e7eb", fontSize: 10, animationDelay: "1.4s" })}>⌛ HOJE</div>
        )}
        <div className="mp-rise" style={chipStyle(torch.x + (chipRight ? 16 : -150), torch.y + 14, { color: "#0b1a33", background: "rgba(255,246,200,.92)", animationDelay: "1.2s" })}>VOCÊ · {rpP}% · SEMANA {weekNo} · {wDone}/{wl.length}</div>''')
rep('''          <button key={`h${c.key}`} className="mp-hot" onClick={() => setOpen(`m:${c.key}`)} aria-label={`Abrir ${c.name}`} style={{ ...pct(c.x, c.y - 10), width: "12%", height: "8%" }} />''',
    '''          <button key={`h${c.key}`} className="mp-hot" onClick={() => setOpen(`m:${c.key}`)} aria-label={`Abrir ${c.name}`} style={{ ...pct(c.x, c.y - 10), width: "12%", height: months.length > 8 ? "6.8%" : "8%" }} />''')
open(p, 'w', encoding='utf-8').write(s)
rep('''            {c.short} {c.goals.length ? `${c.done}/${c.goals.length}` : "·"}''', '''            {c.short}{months.length > 12 && c.key === months[months.length - 1].key ? ` ${c.key.slice(2, 4)}` : ""} {c.goals.length ? `${c.done}/${c.goals.length}` : "·"}''')
open(p, 'w', encoding='utf-8').write(s)
print('ok')
