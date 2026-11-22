p = 'work62.html'
s = open(p, encoding='utf-8').read()
def rep(a, b, n=1):
    global s
    assert s.count(a) == n, (a[:90], s.count(a)); s = s.replace(a, b)

# ---------- 1. sincronização: junção por campo (três vias) ----------
rep('''function useGithubSync(stateRef, loaded, state, applyRemoteRef) {''', '''/* junção de três vias: base = última versão em comum; o que só um lado mudou fica; se os dois mudaram o mesmo campo, vale o mais novo */
const SYNC_BASE = "rb.sync.base";
const syncBaseGet = () => { try { const t = localStorage.getItem(SYNC_BASE); return t ? JSON.parse(t) : null; } catch (e) { return null; } };
const syncBaseSet = (st) => { try { localStorage.setItem(SYNC_BASE, JSON.stringify(st)); } catch (e) { /* sem espaço: a junção cai para "o mais novo vale" */ } };
const sxObj = (x) => !!x && typeof x === "object" && !Array.isArray(x);
function sxEq(a, b) {
  if (a === b) return true;
  if (!a || !b || typeof a !== "object" || typeof b !== "object") return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  if (Array.isArray(a)) { if (a.length !== b.length) return false; for (let i = 0; i < a.length; i++) if (!sxEq(a[i], b[i])) return false; return true; }
  const ka = Object.keys(a).filter((k) => a[k] !== undefined), kb = Object.keys(b).filter((k) => b[k] !== undefined);
  if (ka.length !== kb.length) return false;
  for (const k of ka) if (!sxEq(a[k], b[k])) return false;
  return true;
}
const sxIds = (arr) => Array.isArray(arr) && arr.every((x) => sxObj(x) && x.id != null) && new Set(arr.map((x) => String(x.id))).size === arr.length;
function syncMerge(base, local, remote, localWins) {
  if (sxEq(local, remote)) return local;
  if (sxEq(base, local)) return remote;
  if (sxEq(base, remote)) return local;
  if (sxObj(local) && sxObj(remote)) {
    const b = sxObj(base) ? base : {};
    const out = {};
    new Set([...Object.keys(local), ...Object.keys(remote)]).forEach((k) => { const v = syncMerge(b[k], local[k], remote[k], localWins); if (v !== undefined) out[k] = v; });
    return out;
  }
  if (sxIds(local) && sxIds(remote)) {
    const bm = new Map((sxIds(base) ? base : []).map((x) => [String(x.id), x]));
    const lm = new Map(local.map((x) => [String(x.id), x])), rm = new Map(remote.map((x) => [String(x.id), x]));
    const order = [...local.map((x) => String(x.id)), ...remote.map((x) => String(x.id)).filter((k) => !lm.has(k))];
    const out = [];
    order.forEach((k) => { const v = syncMerge(bm.get(k), lm.get(k), rm.get(k), localWins); if (v !== undefined) out.push(v); });
    return out;
  }
  return localWins ? local : remote;
}
function useGithubSync(stateRef, loaded, state, applyRemoteRef) {''')

rep('''  const push = (async (force) => {''', '''  const push = (async (force, override) => {''')
rep('''      const doc = { v: 1, by: m.device || "app", updatedAt: m.dirtyAt || Date.now(), state: stateRef.current }; Object.defineProperty''',
    '''      const doc = { v: 1, by: m.device || "app", updatedAt: override ? Date.now() : (m.dirtyAt || Date.now()), state: override || stateRef.current }; Object.defineProperty''')
rep('''      else { syncSave({ sha: res.sha, dirty: syncHash(stateRef.current) !== doc.__h, hash: doc.__h, lastPushAt: Date.now() }); syncStatus("ok"); }''',
    '''      else { syncBaseSet(doc.state); syncSave({ sha: res.sha, ...(override ? { dirty: false } : { dirty: syncHash(stateRef.current) !== doc.__h, hash: doc.__h }), lastPushAt: Date.now() }); syncStatus("ok"); }''')
rep('''    let then = null;
    try {
      const f = await ghGetFile();''', '''    let then = null, merged = null;
    try {
      const f = await ghGetFile();''')
rep('''        if (m.dirty && (m.dirtyAt || 0) >= remoteAt) {
          /* a versão deste aparelho é mais nova: guarda a outra como cópia e envia a daqui */
          try { localStorage.setItem("rb.sync.copia", JSON.stringify({ at: Date.now(), de: "repositório", doc })); } catch (e) { /* ok */ }
          syncSave({ sha: f.sha, lastPullAt: Date.now() }); then = "push";
        } else if (doc.state && typeof doc.state === "object") {
          if (m.dirty) { try { localStorage.setItem("rb.sync.copia", JSON.stringify({ at: Date.now(), de: "aparelho", state: stateRef.current })); } catch (e) { /* ok */ } }
          skip.current = Date.now();
          applyRemoteRef.current && applyRemoteRef.current(doc.state, doc.by, m.device);
          syncSave({ sha: f.sha, dirty: false, lastPullAt: Date.now(), lastBy: doc.by || "" });
          syncStatus("ok");
        } else { syncSave({ sha: f.sha }); then = "push"; }''', '''        if (!doc.state || typeof doc.state !== "object") { syncSave({ sha: f.sha }); then = "push"; }
        else if (!m.dirty) {
          /* nada pendente aqui: vale o arquivo */
          skip.current = Date.now();
          applyRemoteRef.current && applyRemoteRef.current(doc.state, doc.by, m.device);
          syncBaseSet(doc.state);
          syncSave({ sha: f.sha, dirty: false, lastPullAt: Date.now(), lastBy: doc.by || "" });
          syncStatus("ok");
        } else {
          /* os dois lados mudaram: junta campo a campo; guarda as duas versões antes, por segurança */
          try { localStorage.setItem("rb.sync.copia", JSON.stringify({ at: Date.now(), de: "antes da junção", aparelho: stateRef.current, repositorio: doc })); } catch (e) { /* ok */ }
          const base = syncBaseGet();
          const localWins = (m.dirtyAt || 0) >= remoteAt;
          merged = base ? syncMerge(base, stateRef.current, doc.state, localWins) : (localWins ? stateRef.current : doc.state);
          skip.current = Date.now();
          applyRemoteRef.current && applyRemoteRef.current(merged, doc.by, m.device);
          syncSave({ sha: f.sha, lastPullAt: Date.now(), lastBy: doc.by || "" });
          then = "push";
        }''')
rep('''    if (then === "push") await push(true);
  });''', '''    if (then === "push") await push(true, merged);
  });''')

# ---------- 2. semanais agendadas: weekPlan guarda as das semanas futuras ----------
rep('''  weeklies: [],
''', '''  weeklies: [],
  weekPlan: [],
''')
rep('''    weeklies: Array.isArray(p.weeklies) ? p.weeklies : [],''', '''    weeklies: Array.isArray(p.weeklies) ? p.weeklies : [],
    weekPlan: Array.isArray(p.weekPlan) ? p.weekPlan.filter((g) => g && typeof g.week === "string") : [],''')
# na virada (e a cada processamento), as metas planejadas para esta semana entram na lista da semana
rep('''    const after = levelFromXP(xp).lvl;
    if (after < before) log("leveldown",''', '''    let weekPlan = Array.isArray(s.weekPlan) ? s.weekPlan : [];
    if (weekPlan.some((g) => g.week <= thisWeek)) {
      const have = new Set(weeklies.map((g) => String(g.id)));
      weeklies = [...weeklies, ...weekPlan.filter((g) => g.week === thisWeek && !have.has(String(g.id)))];
      weekPlan = weekPlan.filter((g) => g.week > thisWeek);
    }

    const after = levelFromXP(xp).lvl;
    if (after < before) log("leveldown",''')
rep('''history: hist.slice(0, 120), weeklies, monthlies, fixedWeek, finals,''', '''history: hist.slice(0, 120), weeklies, weekPlan, monthlies, fixedWeek, finals,''')
rep('''        weeklies: wipeGoals ? [] : (s.weeklies || []).map(clean),''', '''        weeklies: wipeGoals ? [] : (s.weeklies || []).map(clean),
        weekPlan: wipeGoals ? [] : (s.weekPlan || []).map(clean),''')

# lista das próximas semanas (só leitura), no mapa e na lista
rep('''function GoalsArea(props) {''', '''function WeekPlanList({ plan }) {
  const [open, setOpen] = useState(false);
  const list = (plan || []).slice().sort((a, b) => (a.week < b.week ? -1 : a.week > b.week ? 1 : 0));
  if (!list.length) return null;
  const weeks = [];
  list.forEach((g) => { const w = weeks.find((x) => x.k === g.week); if (w) w.items.push(g); else weeks.push({ k: g.week, items: [g] }); });
  const dm = (k) => `${k.slice(8)}/${k.slice(5, 7)}`;
  return (
    <div style={{ marginTop: 12, borderTop: `1px solid ${T.border}`, paddingTop: 4 }}>
      <button onClick={() => setOpen(!open)} aria-expanded={open} style={{ background: "none", border: "none", color: T.accentSoft, fontFamily: F.ui, fontSize: 14, cursor: "pointer", padding: "8px 0", width: "100%", textAlign: "left", minHeight: 44 }}>
        {open ? "▾" : "▸"} Próximas semanas · {list.length} meta(s) planejada(s)
      </button>
      {open && weeks.map((w) => (
        <div key={w.k} style={{ marginTop: 8 }}>
          <div style={{ fontSize: 12, color: T.muted, letterSpacing: ".04em" }}>{dm(w.k)} A {dm(addDays(w.k, 6))} · {w.items.length} meta(s)</div>
          {w.items.map((g) => (
            <div key={g.id} style={{ display: "flex", gap: 8, alignItems: "baseline", fontSize: 14, padding: "4px 0", color: T.text, lineHeight: 1.35 }}>
              <span style={{ color: RANK_COLOR[g.diff] || T.muted, fontWeight: 700, minWidth: 14 }}>{g.diff}</span>
              <span style={{ flex: 1 }}>{g.title}</span>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}
function GoalsArea(props) {''')
rep('''          <button onClick={() => setOpen("new:semana")} style={{ ...btnPrimary, width: "100%", marginTop: 12 }}>+ Nova meta da semana</button>''',
    '''          <button onClick={() => setOpen("new:semana")} style={{ ...btnPrimary, width: "100%", marginTop: 12 }}>+ Nova meta da semana</button>
          <WeekPlanList plan={state.weekPlan} />''')
rep('''          onRemove={(id) => removeItem("weeklies", id)}
        />
      </section>''', '''          onRemove={(id) => removeItem("weeklies", id)}
        />
        <WeekPlanList plan={state.weekPlan} />
      </section>''')
open(p, 'w', encoding='utf-8').write(s)
print('ok')
