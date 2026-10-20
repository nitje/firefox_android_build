/* Two independent about:config editors: global build defaults and per-build modal.
   IDs work on HTTP LAN origins (crypto.randomUUID requires a secure context). */
(() => {
  const $ = id => document.getElementById(id);
  const clone = x => JSON.parse(JSON.stringify(x));
  const make = (tag, cls = '', label) => {
    const e = document.createElement(tag); e.className = cls;
    if (label !== undefined) e.textContent = label;
    return e;
  };
  function newId() {
    if (globalThis.crypto?.randomUUID) return crypto.randomUUID();
    const bytes = new Uint8Array(16);
    if (globalThis.crypto?.getRandomValues) crypto.getRandomValues(bytes);
    else for (let i=0;i<16;i++) bytes[i]=Math.floor(Math.random()*256);
    return 'u_' + [...bytes].map(x=>x.toString(16).padStart(2,'0')).join('');
  }
  function btn(label, cls, fn) {
    const b=make('button',cls,label);b.type='button';b.addEventListener('click',fn);return b;
  }
  function errorFromResponse(response, data) {
    return typeof data?.detail==='string'?data.detail:`HTTP ${response.status}`;
  }
  // Links explain host-Windows codecs; they are NOT included in the Firefox ZIP.
  const mediaCodecLinks = [
    ['AV1-Videoerweiterung', 'https://apps.microsoft.com/detail/9MVZQVXJBQ9V'],
    ['HEVC/H.265-Videoerweiterung', 'https://apps.microsoft.com/detail/9NMZLZ57R3T7'],
    ['VP9-Videoerweiterung', 'https://apps.microsoft.com/detail/9N4D0MSMP0PT'],
    ['AVC/H.264-Videoerweiterung', 'https://apps.microsoft.com/detail/9PB0TRCNRHFX'],
    ['MPEG-2-Videoerweiterung', 'https://apps.microsoft.com/detail/9N95Q1ZZPMH4'],
  ];
  function mediaCompatibilityNotice(){
    const notice=make('div','pref-media-notice pref-warning');
    notice.setAttribute('role','note');
    notice.append(make('strong','','⚠ Windows-Medienerweiterungen (AV1, HEVC, VP9, AVC, MPEG-2) – bei fehlenden Codecs auf dem Ziel-PC installieren. '));
    notice.append(make('span','', 'Die Erweiterungen werden nicht in das Firefox-Paket eingebaut. Nach der Installation Firefox neu starten. '));
    const wrap=make('div','pref-codec-links');
    for(const [name,url] of mediaCodecLinks){
      const a=make('a','pref-codec-link',name+' ↗');
      a.href=url;a.target='_blank';a.rel='noopener noreferrer';
      wrap.append(a);
    }
    notice.append(wrap);
    return notice;
  }
  class Editor {
    constructor({box, count, status, addCategory, config, autosave=false, changed}) {
      this.box=box;this.count=count;this.statusBox=status;this.addCategory=addCategory;
      this.cfg=clone(config);this.autosave=autosave;this.changed=changed||(()=>{});
      this.ready=true;this.valid=true;this.dirty=false;
      this.generation=0;this.savedGeneration=0;this.timer=null;
      this.saving=Promise.resolve();this.invalidNumbers=new Set();
      // v0.0.14 always created an empty custom category: do not show it unless
      // it has user content, which must never be discarded.
      this.cfg.categories=this.cfg.categories.filter(c=>c.id!=='custom'||c.rows.length);
      this.addCategory.addEventListener('click',()=>this.categoryInput());
      this.render();
    }
    status(message, bad=false) {
      this.statusBox.textContent=message;
      this.statusBox.className='small '+(bad?'pref-error-text':'muted');
    }
    selected(){return this.cfg.categories.flatMap(c=>c.rows.filter(r=>r.enabled).map(r=>({enabled:true,name:r.name.trim(),type:r.type,value:r.value})));}
    validate() {
      const names=new Set();let error='';let total=0;
      for(const c of this.cfg.categories) for(const r of c.rows) {
        total++;
        if(!r.enabled) continue;
        const n=r.name.trim();
        if(!/^[A-Za-z0-9_.-]{1,200}$/.test(n)){error='Aktivierter Pref benötigt einen gültigen Feldnamen (A–Z, 0–9, . _ -).';break;}
        if(names.has(n)){error=`Doppelt aktivierter Pref: ${n}`;break;}
        names.add(n);
        if(r.type==='Number'&&(!Number.isInteger(r.value)||r.value<-(2**31)||r.value>2**31-1)){error=`Number muss eine 32-bit-Ganzzahl sein: ${n}`;break;}
        if(r.type==='Boolean'&&typeof r.value!=='boolean'){error=`Boolean ungültig: ${n}`;break;}
        if(r.type==='String'&&(typeof r.value!=='string'||r.value.length>8192)){error=`String ungültig (max. 8192 Zeichen): ${n}`;break;}
      }
      this.valid=!error&&!this.invalidNumbers.size;
      this.count.textContent=`${names.size} aktiv · ${total} Einträge`;
      if(error||this.invalidNumbers.size) this.status(error||'Bitte eine gültige 32-bit-Ganzzahl eingeben.',true);
      else if(this.autosave&&this.generation===this.savedGeneration)this.status(`${names.size} aktive Einstellungen · serverseitig gespeichert`);
      else if(!this.autosave)this.status(`${names.size} aktive Einstellungen · Änderungen erst mit „Speichern & ZIP aktualisieren“ übernehmen`);
      this.changed(this.valid);
      return this.valid;
    }
    markChanged(){
      this.generation++;this.dirty=true;this.validate();
      if(!this.autosave||!this.valid)return;
      this.status('Einstellungen werden gespeichert …');
      clearTimeout(this.timer);
      this.timer=setTimeout(()=>{this.flush().catch(e=>this.status(`Speichern fehlgeschlagen: ${e.message}`,true));},550);
    }
    async flush(){
      clearTimeout(this.timer);
      if(!this.validate())throw new Error('Ungültige about:config-Werte. Bitte korrigieren.');
      if(!this.autosave)return {selected:this.selected(),full:clone(this.cfg)};
      if(this.generation!==this.savedGeneration){
        const rev=this.generation, snapshot=JSON.stringify(this.cfg);
        this.saving=this.saving.catch(()=>{}).then(async()=>{
          const r=await fetch('/api/about-config',{method:'PUT',headers:{'Content-Type':'application/json'},body:snapshot});
          const d=await r.json().catch(()=>({}));
          if(!r.ok)throw new Error(errorFromResponse(r,d));
          this.savedGeneration=Math.max(this.savedGeneration,rev);
          if(rev===this.generation){this.dirty=false;this.status(`${this.selected().length} aktive Einstellungen · serverseitig gespeichert`);}
        });
        await this.saving;
      }
      await this.saving;
      return {selected:this.selected(),full:clone(this.cfg)};
    }
    addRow(c){
      const id=newId();
      c.rows.push({id,enabled:false,name:'',type:'Boolean',value:false,note:''});
      c.open=true;this.render();this.markChanged();
      this.box.querySelector(`[data-pref-id="${id}"] .pref-name`)?.focus();
    }
    categoryInput(){
      if(this.box.querySelector('.pref-new-category'))return;
      const wrap=make('div','pref-new-category row');
      const input=make('input');input.type='text';input.maxLength=100;input.placeholder='Name der neuen Kategorie';input.setAttribute('aria-label','Kategoriename');
      const cancel=btn('Abbrechen','secondary',()=>wrap.remove());
      const save=btn('Kategorie anlegen','secondary',()=>{
        const title=input.value.trim();
        if(!title){input.focus();this.status('Bitte zuerst einen Kategorienamen eingeben.',true);return;}
        const c={id:'custom_'+newId(),title,rows:[],open:true};
        this.cfg.categories.push(c);wrap.remove();this.render();this.markChanged();this.addRow(c);
      });
      input.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();save.click();}if(e.key==='Escape')wrap.remove();});
      wrap.append(input,save,cancel);this.box.append(wrap);input.focus();
    }
    rowElement(row,cat){
      const line=make('div','pref-row'+(row.enabled?'':' is-disabled'));line.dataset.prefId=row.id;
      const check=make('input','pref-check');check.type='checkbox';check.checked=row.enabled;
      check.setAttribute('aria-label','Pref aktivieren '+(row.name||'eigener Eintrag'));
      check.addEventListener('change',()=>{row.enabled=check.checked;line.classList.toggle('is-disabled',!row.enabled);this.markChanged();});
      const name=make('input','pref-name pref-field-name');name.type='text';name.maxLength=200;name.value=row.name;
      name.placeholder='z. B. browser.startup.page';name.title='Feldname (bearbeitbar)';
      name.addEventListener('input',()=>{row.name=name.value;this.markChanged();});
      const type=make('select','pref-type');
      for(const t of ['Boolean','Number','String']){const o=make('option','',t);o.value=t;type.append(o);}
      type.value=row.type;
      const value=make('div','pref-value');
      const drawValue=()=>{
        value.replaceChildren();let control;
        if(row.type==='Boolean'){
          control=make('select','pref-value-input');
          for(const v of ['true','false']){const o=make('option','',v);o.value=v;control.append(o);}
          control.value=row.value?'true':'false';
          control.addEventListener('change',()=>{row.value=control.value==='true';this.markChanged();});
        }else{
          control=make('input','pref-value-input');control.type=row.type==='Number'?'number':'text';
          if(row.type==='Number'){control.step='1';control.min='-2147483648';control.max='2147483647';}
          control.value=String(row.value??'');
          if(row.type==='String')control.placeholder='String (darf leer sein)';
          control.addEventListener('input',()=>{
            if(row.type==='String'){row.value=control.value;this.markChanged();return;}
            const v=Number(control.value);
            const good=/^-?\d+$/.test(control.value)&&Number.isInteger(v)&&v>=-(2**31)&&v<=2**31-1;
            control.classList.toggle('pref-invalid',!good);
            if(good){this.invalidNumbers.delete(row.id);row.value=v;}
            else this.invalidNumbers.add(row.id);
            this.markChanged();
          });
        }
        control.setAttribute('aria-label','Wert für '+row.name);value.append(control);
      };
      type.addEventListener('change',()=>{row.type=type.value;row.value=row.type==='Boolean'?false:row.type==='Number'?0:'';this.invalidNumbers.delete(row.id);drawValue();this.markChanged();});
      drawValue();
      const del=btn('×','danger pref-delete',()=>{cat.rows=cat.rows.filter(x=>x.id!==row.id);this.invalidNumbers.delete(row.id);this.render();this.markChanged();});
      del.title='Eintrag entfernen';
      line.append(check,name,type,value,del);
      if(row.note){const note=make('span','pref-warning','⚠ '+row.note);note.setAttribute('role','note');line.append(note);}
      return line;
    }
    render(){
      this.box.replaceChildren();
      for(const cat of this.cfg.categories){
        const details=make('details','pref-category');details.open=!!cat.open;
        const summary=make('summary');summary.append(make('strong','',cat.title));
        summary.append(make('span','summary-state',`${cat.rows.filter(r=>r.enabled).length}/${cat.rows.length} aktiv`));
        details.append(summary);
        details.addEventListener('toggle',()=>{if(details.open===!!cat.open)return;cat.open=details.open;this.markChanged();});
        const body=make('div','pref-category-body');
        if(cat.id==='media_compat')body.append(mediaCompatibilityNotice());
        for(const row of cat.rows)body.append(this.rowElement(row,cat));
        body.append(btn('＋ Eintrag hinzufügen','secondary pref-add-row',()=>this.addRow(cat)));
        if(cat.id.startsWith('custom_')||cat.id==='custom')body.append(btn('Kategorie entfernen','danger pref-remove-category',()=>{
          if(cat.rows.length&&!confirm('Diese Kategorie mit allen Einträgen entfernen?'))return;
          this.cfg.categories=this.cfg.categories.filter(x=>x.id!==cat.id);this.render();this.markChanged();
        }));
        details.append(body);this.box.append(details);
      }
      this.validate();
    }
  }
  let globalEditor=null,modalEditor=null,modalId=null;
  function updateBuild(valid){window.aboutConfigPanel.ready=!!globalEditor;window.aboutConfigPanel.valid=valid;
    if(typeof refreshBuildButton==='function')refreshBuildButton();}
  async function init(){
    const r=await fetch('/api/about-config');const d=await r.json().catch(()=>({}));
    if(!r.ok)throw new Error(errorFromResponse(r,d));
    globalEditor=new Editor({box:$('aboutConfigCategories'),count:$('aboutConfigCount'),
      status:$('aboutConfigStatus'),addCategory:$('aboutConfigAddCategory'),config:d,autosave:true,changed:updateBuild});
    updateBuild(globalEditor.valid);
  }
  async function openBuild(id){
    const r=await fetch(`/api/builds/${id}/about-config`);const d=await r.json().catch(()=>({}));
    if(!r.ok)throw new Error(errorFromResponse(r,d));
    modalId=id;
    $('editPrefsTitle').textContent=`about:config · Firefox ${d.version} (${d.flavor})`;
    $('editPrefsWarning').textContent=d.prefs_delivery==='embedded'?
      'Älteres Paket: Einige Prefs wurden bereits in omni.ja eingebaut. Neue/aktivierte Werte können ohne Neukompilierung übersteuert werden. Eine hier deaktivierte, früher fest eingebaute Pref lässt sich dagegen nicht zuverlässig auf den ursprünglichen Mozilla-Standard zurücksetzen. Dafür ist einmal ein sauberer Neu-Build nötig.':
      'Nur das portable Firefox-ZIP wird mit neuen Standard-Prefs versehen. Kein Compilerlauf. Ein bereits installiertes Firefox und persönliche Profilwerte ändern sich dadurch nicht.';
    $('editPrefsSnapshot').textContent=d.complete_snapshot?'Originalauswahl dieses Builds geladen.':
      'Älterer Build: aktive Prefs sind aus dem Build-Snapshot wiederhergestellt; damals deaktivierte und individuell umbenannte Felder waren nicht vollständig gespeichert.';
    $('editPrefsSave').disabled=!d.editable;
    modalEditor=new Editor({box:$('editPrefsCategories'),count:$('editPrefsCount'),
      status:$('editPrefsStatus'),addCategory:$('editPrefsAddCategory'),config:d.config,changed:(valid)=>{
        $('editPrefsSave').disabled=!valid||!d.editable;
      }});
    $('editPrefsDialog').showModal();
    $('editPrefsScroll').scrollTop=0;
  }
  async function saveBuild(){
    if(!modalEditor||!modalId)return;
    const button=$('editPrefsSave');button.disabled=true;
    try{
      const {full}=await modalEditor.flush();
      modalEditor.status('Firefox-ZIP wird ohne Neukompilierung neu gepackt …');
      const r=await fetch(`/api/builds/${modalId}/about-config`,{
        method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(full)
      });
      const d=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(errorFromResponse(r,d));
      modalEditor.dirty=false;
      $('editPrefsDialog').close();
      if(typeof loadBuilds==='function')await loadBuilds();
      alert(`about:config übernommen: ${d.selected_count} aktive Prefs. Neues ZIP: ${d.artifact_name}`);
    }catch(e){modalEditor.status(`Nachbearbeitung fehlgeschlagen: ${e.message}`,true);button.disabled=!modalEditor.valid;}
  }
  function closeModal(){if(modalEditor?.dirty&&!confirm('Nicht gespeicherte Änderungen an diesem Build verwerfen?'))return;modalEditor=null;modalId=null;$('editPrefsDialog').close();}
  document.addEventListener('DOMContentLoaded',()=>{
    $('editPrefsSave').addEventListener('click',saveBuild);
    $('editPrefsCancel').addEventListener('click',closeModal);
    $('editPrefsDialog').addEventListener('cancel',e=>{e.preventDefault();closeModal();});
  });
  window.aboutConfigPanel={init,openBuild,flush:async()=>{
    if(!globalEditor)throw new Error('about:config nicht geladen');
    return await globalEditor.flush();
  },ready:false,valid:false};
})();
