p = 'work64.html'
s = open(p, encoding='utf-8').read()
def rep(a, b, n=1):
    global s
    assert s.count(a) == n, (a[:90], s.count(a)); s = s.replace(a, b)

# 1. setter: observação de meta, objetivo final ou marco
rep('''  const finalFx = { toggleMilestone, addMilestone, removeMilestone, setFinalDue, setFinalShort };''',
    '''  const setObs = (key, id, text, msId) => setState((s) => {
    const t = String(text || "").trim().slice(0, 400) || undefined;
    if (key === "finals" && msId != null) return { ...s, finals: s.finals.map((f) => (f.id === id ? { ...f, milestones: (f.milestones || []).map((m) => (m.id === msId ? { ...m, obs: t } : m)) } : f)) };
    return { ...s, [key]: (s[key] || []).map((g) => (g.id === id ? { ...g, obs: t } : g)) };
  });
  const finalFx = { toggleMilestone, addMilestone, removeMilestone, setFinalDue, setFinalShort, setObs };''')

# 2. componente: linha de observação (toque abre; "editar" troca por caixa de texto)
rep('''function MapGoalRow({ g, finals, onToggle, onRemove }) {''', '''function ObsLine({ text, onSave, small }) {
  const [open, setOpen] = useState(false);
  const [edit, setEdit] = useState(false);
  const [val, setVal] = useState(text || "");
  useEffect(() => { if (!edit) setVal(text || ""); }, [text, edit]);
  const fs = small ? 12 : 12.5;
  if (edit) {
    return (
      <div style={{ marginTop: 6 }} onClick={(e) => e.stopPropagation()}>
        <textarea value={val} onChange={(e) => setVal(e.target.value.slice(0, 400))} rows={4} aria-label="Observação" placeholder="Feito quando… · Como… · Por quê…"
          style={{ ...inputStyle, width: "100%", fontSize: 14, lineHeight: 1.4, resize: "vertical", minHeight: 84 }} />
        <div style={{ display: "flex", gap: 8, marginTop: 6, alignItems: "center" }}>
          <button onClick={() => { onSave(val); setEdit(false); setOpen(true); }} style={{ ...btnPrimary, padding: "8px 14px" }}>Salvar</button>
          <button onClick={() => { setVal(text || ""); setEdit(false); }} style={{ background: "none", border: `1px solid ${T.border}`, color: T.muted, padding: "8px 12px", cursor: "pointer", fontFamily: F.ui }}>Cancelar</button>
          <span style={{ fontSize: 11, color: T.muted, marginLeft: "auto" }}>{val.length}/400</span>
        </div>
      </div>
    );
  }
  if (!text) {
    return onSave ? <button onClick={() => setEdit(true)} style={{ background: "none", border: "none", color: T.muted, fontSize: fs, padding: "6px 0 0", cursor: "pointer", fontFamily: F.ui, textAlign: "left" }}>+ observação</button> : null;
  }
  return (
    <div style={{ marginTop: 4 }}>
      <button onClick={() => setOpen(!open)} aria-expanded={open} aria-label={open ? "Fechar observação" : "Abrir observação"}
        style={{ display: "block", width: "100%", background: "none", border: "none", padding: "2px 0", textAlign: "left", cursor: "pointer", color: T.muted, fontSize: fs, lineHeight: 1.4, fontFamily: "inherit",
          ...(open ? { whiteSpace: "pre-wrap", color: T.text } : { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }) }}>
        📝 {text}
      </button>
      {open && onSave && <button onClick={() => setEdit(true)} style={{ background: "none", border: "none", color: T.accentSoft, fontSize: 12, padding: "4px 0", cursor: "pointer", fontFamily: F.ui, textDecoration: "underline" }}>editar observação</button>}
    </div>
  );
}
function MapGoalRow({ g, finals, onToggle, onRemove, onObs }) {''')
rep('''        {g.ifThen && <div style={{ fontSize: 12, color: T.accentSoft, marginTop: 2 }}>↳ {g.ifThen}</div>}
      </div>
      <div style={{ textAlign: "right", flex: "0 0 auto" }}>''', '''        {g.ifThen && <div style={{ fontSize: 12, color: T.accentSoft, marginTop: 2 }}>↳ {g.ifThen}</div>}
        <ObsLine text={g.obs} onSave={onObs} />
      </div>
      <div style={{ textAlign: "right", flex: "0 0 auto" }}>''')

# 3. mapa: semana e mês passam o setter
rep('''<MapGoalRow key={g.id} g={g} finals={finals} onToggle={() => toggleItem("weeklies", g.id)} onRemove={() => removeItem("weeklies", g.id)} />''',
    '''<MapGoalRow key={g.id} g={g} finals={finals} onToggle={() => toggleItem("weeklies", g.id)} onRemove={() => removeItem("weeklies", g.id)} onObs={fx && fx.setObs ? (t) => fx.setObs("weeklies", g.id, t) : null} />''')
rep('''<MapGoalRow key={g.id} g={g} finals={finals} onToggle={() => toggleItem("monthlies", g.id)} onRemove={() => removeItem("monthlies", g.id)} />''',
    '''<MapGoalRow key={g.id} g={g} finals={finals} onToggle={() => toggleItem("monthlies", g.id)} onRemove={() => removeItem("monthlies", g.id)} onObs={fx && fx.setObs ? (t) => fx.setObs("monthlies", g.id, t) : null} />''')

# 4. objetivo final: observação do próprio objetivo e de cada marco
rep('''      <div style={{ fontSize: 12, fontWeight: 700, color: T.gold, letterSpacing: 0.4, margin: "12px 0 4px" }}>MARCOS · {st.msDone}/{st.msTotal}</div>''',
    '''      {fx.setObs && <ObsLine text={f.obs} onSave={(t) => fx.setObs("finals", f.id, t)} />}
      <div style={{ fontSize: 12, fontWeight: 700, color: T.gold, letterSpacing: 0.4, margin: "12px 0 4px" }}>MARCOS · {st.msDone}/{st.msTotal}</div>''')
rep('''            <span style={{ flex: 1, fontSize: 13, color: m.done ? T.muted : T.text, textDecoration: m.done ? "line-through" : "none" }}>{m.title}</span>''',
    '''            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 13, color: m.done ? T.muted : T.text, textDecoration: m.done ? "line-through" : "none" }}>{m.title}</div>
              {(m.obs || fx.setObs) && <ObsLine small text={m.obs} onSave={fx.setObs ? (t) => fx.setObs("finals", f.id, t, m.id) : null} />}
            </div>''')

# 5. lista (modo ☰): linha de observação; editável quando a lista sabe a chave
rep('''function GoalList({ title, hint, items, isFinal, attrNames, onToggle, onAdd, onRemove, accent, glow, finals, fx, routeEnd, reviewTop }) {''',
    '''function GoalList({ title, hint, items, isFinal, attrNames, onToggle, onAdd, onRemove, accent, glow, finals, fx, routeEnd, reviewTop, obsKey }) {''')
rep('''              {link && <div style={{ fontSize: 12, color: CAT.final.color, marginTop: 2 }}>◆ serve a: {link.title}</div>}''',
    '''              {link && <div style={{ fontSize: 12, color: CAT.final.color, marginTop: 2 }}>◆ serve a: {link.title}</div>}
              <ObsLine text={g.obs} onSave={fx && fx.setObs && obsKey ? (t) => fx.setObs(obsKey, g.id, t) : null} />''')
rep('''          onRemove={(id) => removeItem("weeklies", id)}
        />
        <WeekPlanList plan={state.weekPlan} />''', '''          onRemove={(id) => removeItem("weeklies", id)}
          fx={fx} obsKey="weeklies"
        />
        <WeekPlanList plan={state.weekPlan} />''')
rep('''          onRemove={(id) => removeItem("monthlies", id)}
        />
      </Block>''', '''          onRemove={(id) => removeItem("monthlies", id)}
          fx={fx} obsKey="monthlies"
        />
      </Block>''')
rep('''          fx={fx} routeEnd={state.deadline} reviewTop={<FinalTimeline state={state} />}''',
    '''          fx={fx} obsKey="finals" routeEnd={state.deadline} reviewTop={<FinalTimeline state={state} />}''')

# 6. próximas semanas: a observação aparece embaixo do título
rep('''              <span style={{ flex: 1 }}>{g.title}</span>
            </div>
          ))}
        </div>
      ))}''', '''              <span style={{ flex: 1 }}>{g.title}{g.obs && <span style={{ display: "block", fontSize: 12, color: T.muted, marginTop: 2 }}>📝 {g.obs}</span>}</span>
            </div>
          ))}
        </div>
      ))}''')
open(p, 'w', encoding='utf-8').write(s)
print('ok')
