/* ==================== SINCRONIZAÇÃO COM GITHUB (v60) ====================
   O estado inteiro vai para um arquivo estado.json num repositório PRIVADO do Rafael.
   A chave de acesso fica só neste aparelho (localStorage "rb.sync"); nunca vai para o estado nem para o backup.
   Formato do arquivo: { v, by, updatedAt, state }. Quem edita fora do app (o Claude) grava by: "claude". */
const SYNC_KEY = "rb.sync";
const syncBus = { meta: null, status: "", error: "", subs: new Set(), emit() { this.subs.forEach((f) => { try { f(); } catch (e) { /* ok */ } }); } };
function syncMeta() {
  if (!syncBus.meta) { try { syncBus.meta = JSON.parse(localStorage.getItem(SYNC_KEY) || "null") || {}; } catch (e) { syncBus.meta = {}; } }
  return syncBus.meta;
}
function syncSave(p) {
  syncBus.meta = { ...syncMeta(), ...p };
  try { localStorage.setItem(SYNC_KEY, JSON.stringify(syncBus.meta)); } catch (e) { /* sem espaço */ }
  syncBus.emit();
}
function syncStatus(s, err) { syncBus.status = s; syncBus.error = err || ""; syncBus.emit(); }
const syncOn = () => { const m = syncMeta(); return !!(m.on && m.token && m.repo); };
const b64e = (str) => { const bytes = new TextEncoder().encode(str); let bin = ""; for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000)); return btoa(bin); };
const b64d = (b64) => { const bin = atob(String(b64).replace(/\s/g, "")); const bytes = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i); return new TextDecoder().decode(bytes); };
function ghReq(method, url, body, raw, token) {
  const m = syncMeta();
  return fetch(`https://api.github.com${url}`, {
    method, cache: "no-store",
    headers: { Authorization: `Bearer ${token || m.token}`, Accept: raw ? "application/vnd.github.raw+json" : "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
}
const ghErr = (st) => (st === 401 ? "chave inválida ou expirada" : st === 403 ? "a chave não tem permissão neste repositório" : st === 404 ? "repositório não encontrado (ou a chave não enxerga ele)" : `GitHub respondeu ${st}`);
async function ghGetFile() {
  const m = syncMeta(); const path = `/repos/${m.repo}/contents/${m.path || "estado.json"}`;
  const r = await ghReq("GET", path);
  if (r.status === 404) return { missing: true };
  if (!r.ok) throw new Error(ghErr(r.status));
  const j = await r.json();
  let text = j.content && j.encoding === "base64" ? b64d(j.content) : "";
  if (!text) { const r2 = await ghReq("GET", path, null, true); if (!r2.ok) throw new Error(ghErr(r2.status)); text = await r2.text(); }
  let doc = null; try { doc = JSON.parse(text); } catch (e) { throw new Error("estado.json no repositório está com JSON inválido"); }
  return { sha: j.sha, doc };
}
async function ghPutFile(doc, sha) {
  const m = syncMeta();
  const r = await ghReq("PUT", `/repos/${m.repo}/contents/${m.path || "estado.json"}`, { message: `sincronização · ${doc.by} · ${new Date(doc.updatedAt).toISOString()}`, content: b64e(JSON.stringify(doc, null, 1)), ...(sha ? { sha } : {}) });
  if (r.status === 409 || r.status === 422) return { conflict: true };
  if (!r.ok) throw new Error(ghErr(r.status));
  const j = await r.json();
  return { sha: j.content && j.content.sha };
}

/* liga o app ao repositório: busca ao abrir, ao voltar e a cada minuto; envia 4 s depois de cada mudança */
function useGithubSync(stateRef, loaded, state, applyRemoteRef) {
  const skip = useRef(false), busy = useRef(false), timer = useRef(null);
  const push = (async (force) => {
    const m = syncMeta();
    if (!syncOn() || busy.current || !stateRef.current) return;
    if (!m.dirty && !force) return;
    busy.current = true; syncStatus("enviando");
    let retry = false;
    try {
      const doc = { v: 1, by: m.device || "app", updatedAt: m.dirtyAt || Date.now(), state: stateRef.current };
      const res = await ghPutFile(doc, m.sha);
      if (res.conflict) retry = true;
      else { syncSave({ sha: res.sha, dirty: false, lastPushAt: Date.now() }); syncStatus("ok"); }
    } catch (e) { syncStatus("erro", String(e.message || e)); }
    busy.current = false;
    if (retry) await pull(); /* alguém mudou o arquivo: busca, resolve e reenvia */
  });
  const pull = (async () => {
    const m = syncMeta();
    if (!syncOn() || busy.current || !loaded.current) return;
    busy.current = true; syncStatus("buscando");
    let then = null;
    try {
      const f = await ghGetFile();
      if (f.missing) { syncSave({ sha: null }); then = "push"; }
      else if (f.sha === m.sha) { syncSave({ lastPullAt: Date.now() }); syncStatus("ok"); then = m.dirty ? "push" : null; }
      else {
        const doc = f.doc || {};
        const remoteAt = Number(doc.updatedAt) || 0;
        if (m.dirty && (m.dirtyAt || 0) >= remoteAt) {
          /* a versão deste aparelho é mais nova: guarda a outra como cópia e envia a daqui */
          try { localStorage.setItem("rb.sync.copia", JSON.stringify({ at: Date.now(), de: "repositório", doc })); } catch (e) { /* ok */ }
          syncSave({ sha: f.sha, lastPullAt: Date.now() }); then = "push";
        } else if (doc.state && typeof doc.state === "object") {
          if (m.dirty) { try { localStorage.setItem("rb.sync.copia", JSON.stringify({ at: Date.now(), de: "aparelho", state: stateRef.current })); } catch (e) { /* ok */ } }
          skip.current = true;
          applyRemoteRef.current && applyRemoteRef.current(doc.state, doc.by, m.device);
          syncSave({ sha: f.sha, dirty: false, lastPullAt: Date.now(), lastBy: doc.by || "" });
          syncStatus("ok");
        } else { syncSave({ sha: f.sha }); then = "push"; }
      }
    } catch (e) { syncStatus("erro", String(e.message || e)); }
    busy.current = false;
    if (then === "push") await push(true);
  });
  /* cada mudança local marca "pendente" e agenda o envio */
  useEffect(() => {
    if (!state || !loaded.current) return;
    if (skip.current) { skip.current = false; return; }
    if (!syncOn()) return;
    syncSave({ dirty: true, dirtyAt: Date.now() });
    clearTimeout(timer.current);
    timer.current = setTimeout(() => push(), 4000);
  }, [state]);
  useEffect(() => {
    const first = setTimeout(() => pull(), 800);
    const iv = setInterval(() => { if (document.visibilityState === "visible") pull(); }, 60000);
    const onVis = () => { if (document.visibilityState === "visible") pull(); else if (syncMeta().dirty) push(); };
    const onNow = () => pull();
    document.addEventListener("visibilitychange", onVis);
    window.addEventListener("rb-sync-now", onNow);
    return () => { clearTimeout(first); clearInterval(iv); document.removeEventListener("visibilitychange", onVis); window.removeEventListener("rb-sync-now", onNow); };
  }, []);
}

/* painel em Ajustes */
function SyncPanel() {
  const [, force] = useState(0);
  useEffect(() => { const f = () => force((x) => x + 1); syncBus.subs.add(f); const iv = setInterval(f, 15000); return () => { syncBus.subs.delete(f); clearInterval(iv); }; }, []);
  const m = syncMeta();
  const [repo, setRepo] = useState(m.repo || "DTDKHFY/sistema-dados");
  const [token, setToken] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const ago = (t) => { if (!t) return "nunca"; const s = Math.round((Date.now() - t) / 1000); return s < 60 ? "agora" : s < 3600 ? `há ${Math.round(s / 60)} min` : `há ${Math.round(s / 3600)} h`; };
  const connect = async () => {
    const tk = token.trim() || m.token; const rp = repo.trim();
    if (!/^[\w.-]+\/[\w.-]+$/.test(rp)) { setMsg("Repositório no formato usuário/nome."); return; }
    if (!tk) { setMsg("Cole a chave de acesso."); return; }
    setBusy(true); setMsg("Conferindo…");
    try {
      const r = await ghReq("GET", `/repos/${rp}`, null, false, tk);
      if (!r.ok) { setMsg(`Não deu: ${ghErr(r.status)}.`); setBusy(false); return; }
      const j = await r.json();
      if (!j.private) { setMsg("Esse repositório é PÚBLICO. Seus dados ficariam abertos na internet. Torne-o privado no GitHub e tente de novo."); setBusy(false); return; }
      if (j.permissions && j.permissions.push === false) { setMsg("A chave só lê. Dê permissão de escrita em Contents."); setBusy(false); return; }
      const device = m.device || `app-${Math.random().toString(36).slice(2, 8)}`;
      syncSave({ on: true, token: tk, repo: rp, path: "estado.json", device, sha: null, dirty: true, dirtyAt: Date.now() });
      setToken(""); setMsg("Ligado. Enviando seus dados…");
      window.dispatchEvent(new Event("rb-sync-now"));
    } catch (e) { setMsg("Sem conexão com o GitHub agora."); }
    setBusy(false);
  };
  const st = syncBus.status, err = syncBus.error;
  const lbl = { fontSize: 13, color: T.muted };
  if (syncOn()) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 10, padding: "4px 2px", fontSize: 14 }}>
        <div><b style={{ color: st === "erro" ? T.danger : T.good }}>{st === "erro" ? "Erro" : st === "enviando" ? "Enviando…" : st === "buscando" ? "Buscando…" : "Ligado"}</b> · {m.repo}</div>
        {err && <div role="alert" style={{ color: T.danger, fontSize: 13.5 }}>{err}</div>}
        <div style={lbl}>Enviado: {ago(m.lastPushAt)} · conferido: {ago(m.lastPullAt)}{m.dirty ? " · há mudanças a enviar" : ""}</div>
        {m.lastBy === "claude" && <div style={{ ...lbl, color: T.gold }}>A última atualização veio do Claude.</div>}
        <div style={{ display: "flex", gap: 8 }}>
          <button onClick={() => window.dispatchEvent(new Event("rb-sync-now"))} style={{ ...btnPrimary, flex: 1, minHeight: 44 }}>Sincronizar agora</button>
          <button onClick={() => { syncSave({ on: false }); syncStatus(""); }} style={{ flex: 1, minHeight: 44, background: "none", border: `1px solid ${T.border}`, color: T.text, fontFamily: "inherit", fontWeight: 700, cursor: "pointer" }}>Desligar</button>
        </div>
        <button onClick={() => { if (window.confirm ? true : true) { syncSave({ on: false, token: "" }); syncStatus(""); } }} style={{ alignSelf: "flex-start", background: "none", border: "none", color: T.muted, textDecoration: "underline", fontFamily: "inherit", fontSize: 13, minHeight: 40, cursor: "pointer" }}>Apagar a chave deste aparelho</button>
        <div style={lbl}>Os dados vão para o arquivo estado.json do repositório privado. A chave fica só neste aparelho e não entra no backup.</div>
      </div>
    );
  }
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10, padding: "4px 2px", fontSize: 14, lineHeight: 1.45 }}>
      <div style={lbl}>Salva tudo sozinho num repositório <b>privado</b> do seu GitHub. O Claude lê e edita esse arquivo, e o app busca as mudanças ao abrir e a cada minuto.</div>
      <ol style={{ margin: 0, paddingLeft: 18, fontSize: 13.5, color: T.text }}>
        <li>No GitHub: Settings → Developer settings → Personal access tokens → <b>Fine-grained tokens</b> → Generate new token.</li>
        <li>Repository access: <b>Only select repositories</b> → sistema-dados.</li>
        <li>Permissions → Repository → <b>Contents: Read and write</b>. Validade: 1 ano.</li>
        <li>Gere, copie e cole abaixo. Não mande a chave para ninguém.</li>
      </ol>
      <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 13 }}>Repositório
        <input value={repo} onChange={(e) => setRepo(e.target.value)} autoCapitalize="off" autoCorrect="off" spellCheck={false} style={{ ...inputStyle, width: "100%" }} />
      </label>
      <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 13 }}>Chave de acesso
        <input value={token} onChange={(e) => setToken(e.target.value)} type="password" autoComplete="off" placeholder="github_pat_…" style={{ ...inputStyle, width: "100%" }} />
      </label>
      <button onClick={connect} disabled={busy} style={{ ...btnPrimary, minHeight: 46, opacity: busy ? 0.6 : 1 }}>Ligar sincronização</button>
      {msg && <div role="status" style={{ fontSize: 13.5, color: /Não|PÚBLICO|só lê|Sem conexão|formato|Cole/.test(msg) ? T.danger : T.good }}>{msg}</div>}
    </div>
  );
}
