const $=id=>document.getElementById(id);
const PROJECT_PLATFORM=window.PROJECT_PLATFORM||'windows-x64';
const IS_ANDROID=PROJECT_PLATFORM==='android-arm64';
let uiConfig={cards:{}};let applyingCardState=false;let workerData=null;let selectedBuildId=null;let versionValid=false;let androidVersionValid=false;let androidWorkerData=null;let androidSigningData=null;let androidAboutConfigData=null;let androidAboutConfigReady=false;let androidAboutConfigValid=false;let androidAboutConfigTimer=null;const requeuePending=new Set();
async function jsonFetch(url,options={}){const r=await fetch(url,options);let d=null;try{d=await r.json()}catch{}if(!r.ok)throw new Error(typeof d?.detail==='string'?d.detail:JSON.stringify(d?.detail||d||r.statusText));return d}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function formatAge(sec){if(!Number.isFinite(sec))return 'unbekannt';if(sec<60)return `${Math.round(sec)} s`;if(sec<3600)return `${Math.round(sec/60)} min`;return `${(sec/3600).toFixed(1)} h`}
function formatBytes(n){n=Number(n||0);if(n<1024)return `${n} B`;if(n<1024**2)return `${(n/1024).toFixed(1)} KiB`;return `${(n/1024**2).toFixed(1)} MiB`}
async function persistCard(id,state){uiConfig.cards[id]={...(uiConfig.cards[id]||{}),...state};try{await jsonFetch('/api/ui-config',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({cards:{[id]:uiConfig.cards[id]}})})}catch(e){console.warn(e)}}
function bindCards(){document.querySelectorAll('details[data-card-id]').forEach(card=>{card.addEventListener('toggle',()=>{if(applyingCardState)return;const id=card.dataset.cardId;if(id==='worker'||id==='queue'){if(card.open)persistCard(id,{open:true,mode:'pinned-open'});else persistCard(id,{open:false,mode:'auto'});}else persistCard(id,{open:card.open,mode:'manual'});});});}
function setCardOpen(id,open){const el=document.querySelector(`details[data-card-id="${id}"]`);if(!el)return;applyingCardState=true;el.open=!!open;queueMicrotask(()=>applyingCardState=false)}
function applyInitialCardState(){for(const [id,state] of Object.entries(uiConfig.cards||{})){if(id==='worker'||id==='queue'){if(state.mode==='pinned-open')setCardOpen(id,true);}else setCardOpen(id,state.open!==false)}}
function applyAutoCards(){if(!workerData)return;if(!IS_ANDROID){const wc=uiConfig.cards.worker||{mode:'auto'};if(wc.mode!=='pinned-open')setCardOpen('worker',!workerData.all_ok)}const qc=uiConfig.cards.queue||{mode:'auto'};if(qc.mode!=='pinned-open')setCardOpen('queue',!workerData.version_match)}
async function loadUiConfig(){uiConfig=await jsonFetch('/api/ui-config');applyInitialCardState()}
async function loadLatest(force=false,use=true){try{const d=await jsonFetch('/api/latest'+(force?'?force=true':''));$('latestVersion').textContent=d.version;$('latestSource').textContent=`Quelle: ${d.source}${d.esr?' · ESR '+d.esr:''}`;if(use){if(IS_ANDROID){$('androidVersionInput').value=d.version;await checkAndroidVersion()}else{$('versionInput').value=d.version;await checkVersion()}}}catch(e){$('latestVersion').textContent='Fehler';$('latestSource').textContent=e.message}}
async function checkVersion(){const v=$('versionInput').value.trim();versionValid=false;$('buildButton').disabled=true;if(!v){$('versionHint').textContent='Bitte Firefox-Version eingeben.';return}try{const d=await jsonFetch(`/api/version/${encodeURIComponent(v)}/exists`);versionValid=d.exists;$('versionHint').textContent=d.exists?`✓ Firefox ${v}: offizielles Mozilla-Source-Archiv gefunden.`:`Firefox ${v}: Release-Source nicht gefunden.`;refreshBuildButton()}catch(e){$('versionHint').textContent=e.message}}
function refreshBuildButton(){ $('buildButton').disabled=!(versionValid&&workerData?.all_ok&&window.aboutConfigPanel?.ready&&window.aboutConfigPanel?.valid&&window.bookmarkPanel?.ready) }
function androidBuildType(){return document.querySelector('input[name="androidBuildType"]:checked')?.value||'debug'}
function refreshAndroidBuildButton(){const releaseOk=androidBuildType()!=='release'||androidSigningData?.valid;$('androidBuildButton').disabled=!(androidVersionValid&&androidWorkerData?.all_ok&&releaseOk&&androidAboutConfigReady&&androidAboutConfigValid)}
function updateAndroidBuildTypeUI(){const type=androidBuildType();$('androidBuildSummary').textContent=type==='release'?'Fenix · ARM64 · Release-APK':'Fenix · ARM64 · Debug-APK';$('androidReleaseHint').textContent=type==='release'?'Release verwendet die Firefox-Release-App-ID. Wegen deiner eigenen Signatur kann diese APK die offiziell von Mozilla signierte Firefox-App nicht aktualisieren; für die direkte Installation muss eine anders signierte Installation derselben App-ID entfernt werden.':'Debug kann parallel zur normalen Firefox-App installiert werden.';refreshAndroidBuildButton()}
function androidAboutConfigFromForm(){return {schema:1,sync_server:{account_server:{enabled:$('androidAccountServerEnabled').checked,value:$('androidAccountServerValue').value.trim()},sync_server:{enabled:$('androidSyncServerEnabled').checked,value:$('androidSyncServerValue').value.trim()}}}}
function validAndroidServer(enabled,value){if(!enabled)return true;if(!value||/\s/.test(value))return false;try{const u=new URL(value);return (u.protocol==='https:'||u.protocol==='http:')&&!!u.hostname&&!u.username&&!u.password}catch{return false}}
function updateAndroidAboutConfigUI(message=null,isError=false){const cfg=androidAboutConfigFromForm();const account=cfg.sync_server.account_server;const sync=cfg.sync_server.sync_server;const accountOk=validAndroidServer(account.enabled,account.value);const syncOk=validAndroidServer(sync.enabled,sync.value);androidAboutConfigValid=accountOk&&syncOk;const active=(account.enabled?1:0)+(sync.enabled?1:0);$('androidAboutConfigCount').textContent=`${active} aktiv · 2 Einträge`;$('androidAccountServerRow').classList.toggle('is-disabled',!account.enabled);$('androidSyncServerRow').classList.toggle('is-disabled',!sync.enabled);$('androidAccountServerValue').classList.toggle('pref-invalid',!accountOk);$('androidSyncServerValue').classList.toggle('pref-invalid',!syncOk);const status=$('androidAboutConfigStatus');status.classList.toggle('pref-error-text',isError||!androidAboutConfigValid);status.textContent=message||(androidAboutConfigValid?`${active} aktive Fenix-Einstellungen · Änderungen werden serverseitig gespeichert.`:'Aktivierte Server benötigen eine gültige http(s)-URL ohne Zugangsdaten.');refreshAndroidBuildButton();return androidAboutConfigValid}
async function loadAndroidAboutConfig(){androidAboutConfigReady=false;refreshAndroidBuildButton();try{const d=await jsonFetch('/api/android/about-config');androidAboutConfigData=d;const account=d.sync_server.account_server;const sync=d.sync_server.sync_server;$('androidAccountServerEnabled').checked=!!account.enabled;$('androidAccountServerValue').value=account.value??'';$('androidSyncServerEnabled').checked=!!sync.enabled;$('androidSyncServerValue').value=sync.value??'';androidAboutConfigReady=true;updateAndroidAboutConfigUI('Android/Fenix-Einstellungen geladen.')}catch(e){androidAboutConfigReady=false;androidAboutConfigValid=false;updateAndroidAboutConfigUI(e.message,true);throw e}}
async function saveAndroidAboutConfig(){if(androidAboutConfigTimer){clearTimeout(androidAboutConfigTimer);androidAboutConfigTimer=null}const cfg=androidAboutConfigFromForm();if(!updateAndroidAboutConfigUI())throw new Error('Android Sync Server enthält ungültige Werte.');$('androidAboutConfigStatus').textContent='Android/Fenix-Einstellungen werden gespeichert …';try{const d=await jsonFetch('/api/android/about-config',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});androidAboutConfigData=d;androidAboutConfigReady=true;updateAndroidAboutConfigUI(`${(d.sync_server.account_server.enabled?1:0)+(d.sync_server.sync_server.enabled?1:0)} aktive Fenix-Einstellungen · serverseitig gespeichert.`);return d}catch(e){androidAboutConfigReady=false;updateAndroidAboutConfigUI(e.message,true);throw e}}
function scheduleAndroidAboutConfigSave(){androidAboutConfigReady=true;updateAndroidAboutConfigUI('Änderungen werden gespeichert …');if(androidAboutConfigTimer)clearTimeout(androidAboutConfigTimer);if(!androidAboutConfigValid)return;androidAboutConfigTimer=setTimeout(()=>{saveAndroidAboutConfig().catch(()=>{})},600)}
async function checkAndroidVersion(){
  const v=$('androidVersionInput').value.trim();androidVersionValid=false;refreshAndroidBuildButton();
  $('androidCardTitle').textContent='Android bauen';
  if(!v){$('androidVersionHint').textContent='Bitte Firefox-Version eingeben.';return}
  try{const d=await jsonFetch(`/api/version/${encodeURIComponent(v)}/exists`);androidVersionValid=!!d.exists;$('androidVersionHint').textContent=d.exists?`✓ Mozilla-Source für Firefox ${v} verfügbar.`:`Firefox ${v}: Source-Archiv nicht gefunden.`}catch(e){$('androidVersionHint').textContent=e.message}
  refreshAndroidBuildButton();
}
async function loadAndroidWorker(){
  try{
    const d=await jsonFetch('/api/worker/android');androidWorkerData=d;if(IS_ANDROID)workerData=d;
    if(IS_ANDROID){const b=$('workerBadge');b.textContent=d.all_ok?(d.state==='BUILDING'?'Android-Worker beschäftigt':'Android-Worker bereit'):d.online?'Android-Worker prüfen':'Android-Worker offline';b.className='badge '+(d.all_ok?'badge-ok':d.online?'badge-warn':'badge-error')}
    $('androidWorkerSummary').textContent=d.all_ok?`${d.state==='BUILDING'?'⚙ beschäftigt':'✓ bereit'} · v${d.worker_version}`:d.online?`⚠ prüfen · v${d.worker_version||'?'}`:'⚠ offline';
    const text=(x)=>esc(x??'unbekannt');
    $('androidWorkerDetails').innerHTML=`<dt>Container / Host</dt><dd>${d.online?'✓ online':'⚠ offline'} · Linux x86_64</dd><dt>Worker-Version</dt><dd class="${d.version_match?'state-ok':'state-warn'}">${text(d.worker_version)}</dd><dt>Projektstand</dt><dd>${text(d.project_version)}</dd>${d.configured_project_version?`<dt>Container-Version</dt><dd>${text(d.configured_project_version)}</dd>`:''}${d.worker_hostname?`<dt>Worker-Container</dt><dd><code>${text(d.worker_hostname)}</code></dd>`:''}${d.heartbeat_age_seconds!==undefined?`<dt>Letztes Lebenszeichen</dt><dd>vor ${text(d.heartbeat_age_seconds)} s</dd>`:''}<dt>Status</dt><dd>${text(d.state)} · ${text(d.detail)}</dd><dt>Queue</dt><dd>${text(d.queue_visible)} wartend</dd>${d.job_id?`<dt>Aktiver Job</dt><dd>${text(d.job_id)}</dd>`:''}`;
    $('androidWorkerMessage').textContent=!d.version_match&&d.online?'Worker-Code wird im Leerlauf automatisch aktualisiert.':'Automatische Worker-Updates sind aktiv.';
    if(IS_ANDROID)applyAutoCards();refreshAndroidBuildButton();
  }catch(e){androidWorkerData={all_ok:false,version_match:false};if(IS_ANDROID){workerData=androidWorkerData;const b=$('workerBadge');b.textContent='Android-Worker-Prüfung fehlgeschlagen';b.className='badge badge-error'};$('androidWorkerSummary').textContent='⚠ Prüfung fehlgeschlagen';$('androidWorkerDetails').innerHTML=`<dt>Fehler</dt><dd>${esc(e.message)}</dd>`;refreshAndroidBuildButton()}
}
async function loadAndroidSigning(){
  try{
    const d=await jsonFetch('/api/android/signing');androidSigningData=d;
    $('androidSigningDetails').innerHTML=d.valid?`<dt>Keystore</dt><dd class="state-ok">✓ vorhanden</dd><dt>Alias</dt><dd>${esc(d.alias)}</dd><dt>SHA-256</dt><dd><code>${esc(d.fingerprint_sha256)}</code></dd>${d.created_at?`<dt>Erstellt</dt><dd>${esc(new Date(d.created_at).toLocaleString())}</dd>`:''}`:`<dt>Keystore</dt><dd class="${d.exists?'state-bad':'state-warn'}">${d.exists?'⚠ ungültig':'nicht vorhanden'}</dd>${d.error?`<dt>Fehler</dt><dd>${esc(d.error)}</dd>`:''}`;
    $('androidSigningGenerate').disabled=!!d.exists;
    $('androidSigningExport').disabled=!d.valid;
    refreshAndroidBuildButton();
  }catch(e){androidSigningData={valid:false};$('androidSigningDetails').innerHTML=`<dt>Fehler</dt><dd>${esc(e.message)}</dd>`;refreshAndroidBuildButton()}
}
async function generateAndroidSigning(){
  if(!confirm('Einen neuen individuellen Android-Release-Keystore für diese Installation erzeugen? Danach bitte unbedingt ein verschlüsseltes Backup exportieren.'))return;
  const b=$('androidSigningGenerate');b.disabled=true;$('androidSigningMessage').textContent='Release-Keystore wird erzeugt …';
  try{const d=await jsonFetch('/api/android/signing/generate',{method:'POST'});$('androidSigningMessage').textContent=`Keystore erzeugt. SHA-256: ${d.fingerprint_sha256} – bitte jetzt sichern.`;await loadAndroidSigning()}catch(e){$('androidSigningMessage').textContent=e.message;await loadAndroidSigning()}
}
async function exportAndroidSigning(){
  const password=$('androidSigningBackupPassword').value;
  if(password.length<12){$('androidSigningMessage').textContent='Für den Export ein Backup-Passwort mit mindestens 12 Zeichen eingeben.';return}
  $('androidSigningMessage').textContent='Verschlüsseltes Signing-Backup wird erstellt …';
  try{
    const r=await fetch('/api/android/signing/export',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password})});
    if(!r.ok){let d={};try{d=await r.json()}catch{}throw new Error(d.detail||r.statusText)}
    const blob=await r.blob();const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='firefox_android_signing_backup.zip';document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
    $('androidSigningMessage').textContent='Signing-Backup exportiert. Backup-Datei und Passwort getrennt sicher aufbewahren.';
  }catch(e){$('androidSigningMessage').textContent=e.message}
}
async function importAndroidSigning(){
  const file=$('androidSigningImportFile').files?.[0];const password=$('androidSigningBackupPassword').value;
  if(!file){$('androidSigningMessage').textContent='Bitte zuerst ein Signing-Backup auswählen.';return}
  if(password.length<12){$('androidSigningMessage').textContent='Das Backup-Passwort muss mindestens 12 Zeichen lang sein.';return}
  const replace=$('androidSigningReplace').checked;
  if(replace&&!confirm('Vorhandenen Release-Keystore wirklich ersetzen? Bereits installierte Release-APKs mit dem bisherigen Key können danach nicht mehr mit neuen Builds aktualisiert werden.'))return;
  const fd=new FormData();fd.append('backup',file);fd.append('password',password);fd.append('replace',replace?'true':'false');
  $('androidSigningMessage').textContent='Signing-Backup wird geprüft und importiert …';
  try{
    const r=await fetch('/api/android/signing/import',{method:'POST',body:fd});let d={};try{d=await r.json()}catch{}if(!r.ok)throw new Error(d.detail||r.statusText);
    $('androidSigningMessage').textContent=`Keystore importiert. SHA-256: ${d.fingerprint_sha256}`;$('androidSigningImportFile').value='';$('androidSigningReplace').checked=false;await loadAndroidSigning();
  }catch(e){$('androidSigningMessage').textContent=e.message}
}
async function createAndroidBuild(){
  const btn=$('androidBuildButton');btn.disabled=true;$('androidBuildMessage').textContent='Android-Build wird eingereiht …';
  const type=androidBuildType();
  try{
    const androidAbout=await saveAndroidAboutConfig();
    const d=await jsonFetch('/api/builds',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:$('androidVersionInput').value.trim(),platform:'android-arm64',android_build_type:type,android_about_config:androidAbout})});
    $('androidBuildMessage').textContent=`Firefox ${d.version} Android ARM64 (Fenix-${type==='release'?'Release':'Debug'}-APK) wurde eingereiht.`;
    await Promise.all([loadBuilds(),loadQueue(),loadAndroidWorker(),loadAndroidSigning()]);
  }catch(e){$('androidBuildMessage').textContent=e.message}
  finally{refreshAndroidBuildButton()}
}

async function loadWorker(){
  try{
    const d=await jsonFetch('/api/worker');workerData=d;
    const b=$('workerBadge');
    if(d.busy){b.textContent=d.can_accept_jobs?'Worker beschäftigt · Queue verfügbar':'Worker beschäftigt'}
    else{b.textContent=d.all_ok?'Worker bereit':d.online?'Worker prüfen':'Worker offline'}
    b.className='badge '+(d.can_accept_jobs?'badge-ok':d.online?'badge-warn':'badge-error');
    $('workerSummary').textContent=d.busy?`beschäftigt · Queue verfügbar · Worker ${d.worker_version||'?'}`:d.all_ok?`bereit · Worker ${d.worker_version}`:`Projekt ${d.project_version} · Worker ${d.worker_version||'?'}`;
    const dev=typeof d.dev_drive==='object'?`${d.dev_drive.letter||'D:'} · ${d.dev_drive.ready?'Dev Drive bereit':'nicht bereit'}${d.dev_drive.filesystem?' · '+d.dev_drive.filesystem:''}${d.dev_drive.size_gb?' · '+d.dev_drive.size_gb+' GB':''}${d.dev_drive.detail?' · '+d.dev_drive.detail:''}`:(d.dev_drive||'noch nicht gemeldet');
    const mem=d.pagefile||{};
    const memoryText=mem.ready?`✓ ${mem.detail||`Commit-Limit ${mem.commit_limit_gb} GB · frei ${mem.free_commit_gb} GB`}`:
      mem.pending_reboot?`⚠ ${mem.detail} · Windows-VM einmal neu starten`:
      mem.detail?`⚠ ${mem.detail}`:'noch nicht geprüft';
    const def=d.defender||{};
    const defText=def.ready?`✓ aktiv · ${(def.covered_paths||[]).join(' · ')||'Build-Pfade ausgenommen'}`:def.checked_at?`⚠ geprüft · ${(def.warnings||[]).join(' · ')||'nicht alle Ausnahmen bestätigt'}`:'noch nicht gemeldet';
    const vc=d.vcredist||{};
    const vcArch=x=>x?.installed?`${x.version||'installiert'}${x.version_ok===false?' · zu alt':''}`:'fehlt';
    const vcText=vc.ready?`✓ x86 ${vcArch(vc.x86)} · x64 ${vcArch(vc.x64)}`:vc.checked_at?`⚠ x86 ${vcArch(vc.x86)} · x64 ${vcArch(vc.x64)}${(vc.warnings||[]).length?' · '+(vc.warnings||[]).join(' · '):''}`:'noch nicht gemeldet';
    $('workerDetails').innerHTML=`<dt>Windows/SSH</dt><dd>${d.online?'✓ online':'offline'} · ${esc(d.host)}:${esc(d.port)}</dd><dt>Build-Worker</dt><dd>${d.busy?'⚙ beschäftigt · Queue verfügbar':d.all_ok?'✓ bereit':'⏳ nicht vollständig bereit'}</dd><dt>Worker-Version</dt><dd class="${d.version_match?'state-ok':'state-warn'}">${esc(d.worker_version||'nicht erkannt')}</dd><dt>Projektstand</dt><dd>${esc(d.project_version)}</dd><dt>Versionen gleich</dt><dd>${d.version_match?'✓ ja':'⚠ nein'}</dd><dt>MozillaBuild</dt><dd>${d.mozilla_build?'✓ installiert':'⏳ fehlt/nicht erkannt'}</dd><dt>Mozilla Build-State</dt><dd>${esc(d.mozbuild_state_path||'D:\\mozbuild-state')}</dd><dt>VC++ Runtime</dt><dd>${esc(vcText)}</dd><dt>RAM / Auslagerungsdatei</dt><dd>${esc(memoryText)}</dd><dt>Dev Drive</dt><dd>${esc(dev)}</dd><dt>Windows Defender</dt><dd>${esc(defText)}</dd><dt>Status</dt><dd>${esc(d.detail)}</dd>${d.auto_update?.running?'<dt>Worker-Autoupdate</dt><dd>⚙ aktuelle Worker-Dateien werden automatisch übertragen …</dd>':d.auto_update?.last_error&&!d.version_match?`<dt>Worker-Autoupdate</dt><dd>⚠ ${esc(d.auto_update.last_error)}</dd>`:''}`;
    applyAutoCards();refreshBuildButton()
  }catch(e){
    workerData={online:false,all_ok:false,version_match:false,can_accept_jobs:false};
    $('workerBadge').textContent='Worker-Prüfung fehlgeschlagen';$('workerBadge').className='badge badge-error';
    $('workerDetails').innerHTML=`<dt>Fehler</dt><dd>${esc(e.message)}</dd>`;applyAutoCards();refreshBuildButton()
  }
}
async function finalizeWorker(){const btn=$('finalizeWorkerButton');btn.disabled=true;$('workerMessage').textContent='Worker-Dateien werden repariert und Task wird neu registriert …';try{const d=await jsonFetch('/api/worker/finalize',{method:'POST'});$('workerMessage').textContent=`Übertragen: ${(d.pushed||[]).join(', ')||'keine Dateien'}`;await loadWorker()}catch(e){$('workerMessage').textContent=e.message}finally{btn.disabled=false}}
async function loadQueue(){try{const d=await jsonFetch('/api/queue');workerData=d.worker;$('queueSummary').textContent=`${d.queued} wartend · ${d.active} aktiv`;$('queueDetails').innerHTML=`<dt>Wartend</dt><dd>${d.queued}</dd><dt>Aktiv</dt><dd>${d.active}</dd><dt>Worker-Version = Projektstand</dt><dd>${d.worker.version_match?'✓ ja':'⚠ nein'}</dd><dt>Worker</dt><dd>${d.worker.busy&&d.worker.can_accept_jobs?'⚙ beschäftigt · Queue verfügbar':esc(d.worker.detail)}</dd>`;applyAutoCards();if(IS_ANDROID)refreshAndroidBuildButton();else refreshBuildButton()}catch(e){$('queueSummary').textContent='Fehler';$('queueDetails').innerHTML=`<dt>Fehler</dt><dd>${esc(e.message)}</dd>`}}
function statusClass(s){return ['FINISHED'].includes(s)?'state-ok':['FAILED'].includes(s)?'state-bad':['QUEUED','CLAIMED','BUILDING','PACKAGE'].includes(s)?'state-warn':''}
async function loadBuilds(){try{const rows=await jsonFetch('/api/builds');$('buildRows').innerHTML=rows.length?rows.map(r=>`<tr><td>${esc(r.version)}</td><td>${esc(r.platform)}<br><span class="muted small">${r.platform==='android-arm64'?(r.flavor==='release'?'Fenix-Release-APK · ARM64':'Fenix-Debug-APK · ARM64'):r.flavor==='legacy'?'Bisherig / Unofficial':r.flavor==='nightly'?'Nightly-Branding':'Release-Branding'}</span></td><td class="${statusClass(r.status)}">${esc(r.status)}</td><td>${esc(new Date(r.created_at).toLocaleString())}</td><td class="actions"><button class="secondary" onclick="showLog('${r.id}')">Log</button>${r.artifact_available?`<a class="button-link secondary" href="/api/builds/${r.id}/download">Build herunterladen</a>`:''}${r.status==='FINISHED'?`${r.platform==='windows-x64'?`<button class="secondary" onclick="editBuildPrefs('${r.id}')">Edit about:config</button><button class="secondary" onclick="editBuildBookmarks('${r.id}')">Edit Lesezeichen</button>`:''}<button class="secondary" onclick="deleteBuildLogs('${r.id}')" ${r.log_available||r.diagnostics_available?'':'disabled'}>Log löschen</button>`:''}${['FINISHED','FAILED'].includes(r.status)&&['windows-x64','android-arm64'].includes(r.platform)?`<button class="danger" onclick="cleanupWorkerData('${r.id}','${r.platform}')" ${r.workspace_cleanup?.status==='QUEUED'||r.workspace_cleanup?.status==='BUILDING'||r.workspace_cleanup?.status==='CLAIMED'?'disabled':''}>${r.workspace_cleanup?.status==='FINISHED'?'Workerdaten gelöscht':r.workspace_cleanup?.status==='FAILED'?'Workerdaten erneut löschen':'Workerdaten löschen'}</button>`:''}${r.status==='FAILED'||r.status==='QUEUED'?`<button class="secondary" ${requeuePending.has(r.id)?'disabled':''} onclick="requeue('${r.id}')">${requeuePending.has(r.id)?'wird eingereiht …':'Neu einreihen'}</button>`:''}${!['CLAIMED','BUILDING','PACKAGE'].includes(r.status)?`<button class="danger" onclick="deleteBuild('${r.id}')">Löschen</button>`:''}</td></tr>`).join(''):'<tr><td colspan="5">Noch keine Builds.</td></tr>'}catch(e){$('buildRows').innerHTML=`<tr><td colspan="5">${esc(e.message)}</td></tr>`}}
async function createBuild(){const btn=$('buildButton');btn.disabled=true;$('buildMessage').textContent='Build wird eingereiht …';try{const prefs=await window.aboutConfigPanel.flush();const bookmarks=await window.bookmarkPanel.flush();const d=await jsonFetch('/api/builds',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:$('versionInput').value.trim(),platform:'windows-x64',nightly:$('nightlyInput').checked,about_config:prefs.selected,about_config_full:prefs.full,bookmarks})});$('buildMessage').textContent=`Firefox ${d.version} (${d.flavor}) wurde eingereiht.`;await Promise.all([loadBuilds(),loadQueue(),loadWorker()])}catch(e){$('buildMessage').textContent=e.message}finally{refreshBuildButton()}}
async function showLog(id,persist=true){selectedBuildId=id;if(persist){setCardOpen('log',true);await persistCard('log',{open:true,mode:'manual'})}try{const d=await jsonFetch(`/api/builds/${id}/log?lines=160`);const age=Number.isFinite(d.log_age_seconds)?`letzte Aktivität vor ${formatAge(d.log_age_seconds)}`:'noch keine Log-Datei';$('logMeta').textContent=`${age} · ${formatBytes(d.size_bytes)}`;$('logBox').textContent=d.text;$('logDownload').hidden=!d.download_available;if(d.download_available){$('logDownload').href=`/api/builds/${id}/diagnostics/download`}}catch(e){$('logMeta').textContent='Log konnte nicht geladen werden';$('logBox').textContent=e.message}}
async function requeue(id){if(requeuePending.has(id))return;requeuePending.add(id);await loadBuilds();try{const d=await jsonFetch(`/api/builds/${id}/requeue`,{method:'POST'});if(d.already_queued)console.info(`Build ${id} ist bereits in ${d.location}`);await Promise.all([loadBuilds(),loadQueue()]);await showLog(id,false)}catch(e){alert(e.message)}finally{requeuePending.delete(id);await loadBuilds()}}
async function deleteBuildLogs(id){
  if(!confirm('Log und Diagnose-Snapshots dieses fertigen Builds wirklich löschen? Das Firefox-Paket und die Workerdaten bleiben erhalten.'))return;
  try{const d=await jsonFetch(`/api/builds/${id}/logs`,{method:'DELETE'});if(selectedBuildId===id)await showLog(id,false);await loadBuilds();alert(`Log/Diagnose gelöscht: ${d.deleted_files} Dateien, ${formatBytes(d.freed_bytes)} freigegeben.`)}catch(e){alert(e.message)}
}
async function cleanupWorkerData(id,platform){
  const android=platform==='android-arm64';
  const question=android
    ? 'WIRKLICH die versionsbezogenen Android-Workerdaten dieser Firefox-Version unter /work löschen? Quellbaum, ARM64-Objektverzeichnis, Source-Archiv, Mozconfig sowie Bootstrap-/Configure-Status werden entfernt. SDK/NDK, gemeinsamer Gradle-Cache und Release-Keystore bleiben erhalten. APK und Diagnose-Logs bleiben erhalten.'
    : 'WIRKLICH alle Workerdaten dieser Firefox-Version auf D: löschen? Quellbaum, alle zugehörigen obj-Verzeichnisse und Source-Archiv werden entfernt. Bei einem späteren Build muss alles neu aufgebaut werden. Downloads und Diagnose-Logs bleiben erhalten.';
  if(!confirm(question))return;
  try{await jsonFetch(`/api/builds/${id}/cleanup-workspace`,{method:'POST'});await loadBuilds();alert(`Bereinigung in ${android?'Android':'Windows'}-Worker-Warteschlange eingereiht; Status erscheint in der Versionsverwaltung.`)}catch(e){alert(e.message)}
}
async function deleteBuild(id){if(!confirm('Build wirklich löschen?'))return;try{await jsonFetch(`/api/builds/${id}`,{method:'DELETE'});if(selectedBuildId===id){selectedBuildId=null;$('logMeta').textContent='Kein Build ausgewählt';$('logBox').textContent='Noch kein Build ausgewählt.';$('logDownload').hidden=true}await Promise.all([loadBuilds(),loadQueue()])}catch(e){alert(e.message)}}
async function editBuildBookmarks(id){try{await window.bookmarkPanel.openBuild(id)}catch(e){alert(e.message)}}
window.editBuildBookmarks=editBuildBookmarks;
async function editBuildPrefs(id){try{await window.aboutConfigPanel.openBuild(id)}catch(e){alert(e.message)}}
window.editBuildPrefs=editBuildPrefs;window.showLog=showLog;window.requeue=requeue;window.deleteBuild=deleteBuild;window.deleteBuildLogs=deleteBuildLogs;window.cleanupWorkerData=cleanupWorkerData;
$('refreshLatest').addEventListener('click',()=>loadLatest(true,false));
$('refreshBuilds').addEventListener('click',()=>Promise.all([loadBuilds(),loadQueue()]));
if(IS_ANDROID){
  $('androidVersionInput').addEventListener('input',()=>{androidVersionValid=false;$('androidCardTitle').textContent='Android bauen';refreshAndroidBuildButton()});
  $('androidCheckVersion').addEventListener('click',checkAndroidVersion);
  $('androidBuildButton').addEventListener('click',createAndroidBuild);
  document.querySelectorAll('input[name="androidBuildType"]').forEach(el=>el.addEventListener('change',updateAndroidBuildTypeUI));
  $('androidSigningGenerate').addEventListener('click',generateAndroidSigning);
  $('androidSigningExport').addEventListener('click',exportAndroidSigning);
  $('androidSigningImport').addEventListener('click',importAndroidSigning);
  ['androidAccountServerEnabled','androidSyncServerEnabled'].forEach(id=>$(id).addEventListener('change',scheduleAndroidAboutConfigSave));
  ['androidAccountServerValue','androidSyncServerValue'].forEach(id=>$(id).addEventListener('input',scheduleAndroidAboutConfigSave));
}else{
  $('nightlyInput').addEventListener('change',()=>{$('brandingHint').textContent=$('nightlyInput').checked?'Nightly-Aussehen aus dem gewählten Release-Quellarchiv; kein echtes Mozilla-Nightly mit eigenem Entwicklungsstand. Eigener Update-Kanal default.':'Release-Branding aus Mozilla-Release-Quellcode; selbst kompiliert, kein offizielles Mozilla-Binary. Markenrechte bei Weitergabe beachten.'});
  $('checkVersion').addEventListener('click',checkVersion);
  $('buildButton').addEventListener('click',createBuild);
  $('finalizeWorkerButton').addEventListener('click',finalizeWorker);
  $('versionInput').addEventListener('input',()=>{versionValid=false;refreshBuildButton()});
}
bindCards();
if(IS_ANDROID)updateAndroidBuildTypeUI();
(async()=>{
  try{await loadUiConfig()}catch(e){console.warn('UI config',e)}
  if(IS_ANDROID){
    await Promise.all([loadAndroidWorker(),loadAndroidSigning(),loadAndroidAboutConfig(),loadQueue(),loadBuilds()]);
  }else{
    try{await window.aboutConfigPanel.init();await window.bookmarkPanel.init();refreshBuildButton()}catch(e){console.warn('about:config',e);$('aboutConfigStatus').textContent=e.message;$('bookmarksStatus').textContent=e.message}
    await Promise.all([loadWorker(),loadQueue(),loadBuilds()]);
  }
  await loadLatest(false,true);
  setInterval(()=>IS_ANDROID?Promise.all([loadAndroidWorker(),loadQueue(),loadBuilds()]):Promise.all([loadWorker(),loadQueue(),loadBuilds()]),5000);
  setInterval(()=>{if(selectedBuildId)showLog(selectedBuildId,false)},5000);
})();
