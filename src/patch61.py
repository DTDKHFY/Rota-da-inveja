p='work61.html'; s=open(p,encoding='utf-8').read()
def rep(a,b):
    global s; assert s.count(a)==1,(a[:70],s.count(a)); s=s.replace(a,b)
# hash barato do conteúdo
rep('function useGithubSync(stateRef, loaded, state, applyRemoteRef) {',
'''const syncHash = (o) => { let str = ""; try { str = JSON.stringify(o); } catch (e) { return ""; } let h = 5381; for (let i = 0; i < str.length; i++) h = ((h << 5) + h + str.charCodeAt(i)) | 0; return `${str.length}:${h >>> 0}`; };
function useGithubSync(stateRef, loaded, state, applyRemoteRef) {''')
# ao enviar, guarda o hash do que foi enviado
rep('else { syncSave({ sha: res.sha, dirty: false, lastPushAt: Date.now() }); syncStatus("ok"); }',
    'else { syncSave({ sha: res.sha, dirty: syncHash(stateRef.current) !== doc.__h, hash: doc.__h, lastPushAt: Date.now() }); syncStatus("ok"); }')
rep('const doc = { v: 1, by: m.device || "app", updatedAt: m.dirtyAt || Date.now(), state: stateRef.current };',
    'const doc = { v: 1, by: m.device || "app", updatedAt: m.dirtyAt || Date.now(), state: stateRef.current }; Object.defineProperty(doc, "__h", { value: syncHash(doc.state), enumerable: false });')
# só marca pendente se o conteúdo mudou de verdade
rep('''    if (skip.current) { skip.current = false; return; }
    if (!syncOn()) return;
    syncSave({ dirty: true, dirtyAt: Date.now() });''','''    if (skip.current) { skip.current = false; syncSave({ hash: syncHash(state) }); return; }
    if (!syncOn()) return;
    const h = syncHash(state);
    if (h === syncMeta().hash) return;
    if (!syncMeta().dirty) syncSave({ dirty: true, dirtyAt: Date.now() });''')
open(p,'w',encoding='utf-8').write(s); print('ok')
