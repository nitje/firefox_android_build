/* Firefox editable Bookmarks policy editor. No secure-origin crypto dependency. */
(() => {
  const $ = id => document.getElementById(id);
  const clone = x => JSON.parse(JSON.stringify(x));
  const make = (tag, className='', text) => {
    const e=document.createElement(tag);e.className=className;
    if(text!==undefined)e.textContent=text;
    return e;
  };
  const button = (text,fn,cls='secondary') => {
    const b=make('button',cls,text);b.type='button';b.addEventListener('click',fn);return b;
  };
  function newId(){
    const bytes=new Uint8Array(16);
    if(globalThis.crypto?.getRandomValues)crypto.getRandomValues(bytes);
    else for(let i=0;i<bytes.length;i++)bytes[i]=Math.floor(Math.random()*256);
    return 'b_'+[...bytes].map(n=>n.toString(16).padStart(2,'0')).join('');
  }
  function urlOk(v){return /^(https?:\/\/[^\s]+|ftp:\/\/[^\s]+|about:[^\s]+|file:\/\/[^\s]+|mailto:[^\s]+)$/i.test(v);}
  async function jsonRequest(url,options={}){
    const r=await fetch(url,options),data=await r.json().catch(()=>({}));
    if(!r.ok)throw new Error(data.detail||`HTTP ${r.status}`);
    return data;
  }
  function jsonOptions(doc){return {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(doc)};}
  class BookmarksEditor {
    constructor(box,count,status,addDirect,addFolder,config,{autosave=false,onChange=()=>{}}={}){
      this.box=box;this.count=count;this.status=status;this.addDirect=addDirect;this.addFolder=addFolder;
      this.config=clone(config);this.autosave=autosave;this.onChange=onChange;this.invalid=false;
      this.changed=0;this.saved=0;this.saving=Promise.resolve();this.timer=null;
      this.onDirect=()=>this.add('', 'toolbar');
      this.onFolder=()=>this.folderForm();
      this.addDirect.addEventListener('click',this.onDirect);
      this.addFolder.addEventListener('click',this.onFolder);
      this.render();
    }
    detach(){clearTimeout(this.timer);this.addDirect.removeEventListener('click',this.onDirect);this.addFolder.removeEventListener('click',this.onFolder);}
    notify(msg,bad=false){this.status.textContent=msg;this.status.className='small '+(bad?'pref-error-text':'muted');}
    validate(){
      const ids=new Set();let error='';
      if(this.config.entries.length>1000)error='Maximal 1000 Lesezeichen.';
      for(const r of this.config.entries){
        if(!r.enabled)continue;
        if(!r.title.trim()||!urlOk(r.url.trim())){error='Aktive Lesezeichen brauchen einen Titel und eine gültige URL (https:, about:, …).';break;}
        if(ids.has(r.id)){error='Doppelte Lesezeichen-ID.';break;}ids.add(r.id);
      }
      this.invalid=!!error;
      this.count.textContent=`${this.config.entries.filter(r=>r.enabled).length} aktiv · ${this.config.entries.length} Einträge`;
      if(error)this.notify(error,true);
      else if(this.autosave&&this.changed===this.saved)this.notify('Lesezeichen serverseitig gespeichert');
      else if(!this.autosave)this.notify('Änderungen mit „Speichern & ZIP aktualisieren“ übernehmen');
      this.onChange(!this.invalid);
      return !this.invalid;
    }
    mark(){this.changed++;this.validate();if(!this.autosave||this.invalid)return;
      this.notify('Lesezeichen werden gespeichert …');clearTimeout(this.timer);
      this.timer=setTimeout(()=>this.flush().catch(e=>this.notify(e.message,true)),600);
    }
    async flush(){clearTimeout(this.timer);if(!this.validate())throw new Error('Ungültige Lesezeichen.');
      if(this.autosave&&this.changed!==this.saved){
        const rev=this.changed,doc=clone(this.config);
        this.saving=this.saving.catch(()=>{}).then(async()=>{
          const result=await jsonRequest('/api/bookmarks',jsonOptions(doc));
          this.saved=Math.max(this.saved,rev);
          if(this.saved===this.changed)this.notify('Lesezeichen serverseitig gespeichert');
          return result;
        });await this.saving;
      }else await this.saving;
      return clone(this.config);
    }
    add(folder='',placement='toolbar'){
      const row={id:newId(),enabled:true,title:'',url:'',placement,folder};
      this.config.entries.push(row);this.render();this.mark();
      this.box.querySelector(`[data-bookmark-id="${row.id}"] .bookmark-title`)?.focus();
    }
    folderForm(){if(this.box.querySelector('.bookmarks-folder-form'))return;
      const wrap=make('div','row bookmarks-folder-form');
      const input=make('input');input.type='text';input.maxLength=120;input.placeholder='Ordnername';input.setAttribute('aria-label','Neuer Lesezeichenordner');
      const save=()=>{const name=input.value.trim();if(!name){input.focus();this.notify('Ordnername fehlt.',true);return;}
        if(this.config.folders.includes(name)){input.focus();this.notify('Ordner existiert bereits.',true);return;}
        this.config.folders.push(name);wrap.remove();this.add(name);};
      wrap.append(input,button('Ordner anlegen',save),button('Abbrechen',()=>wrap.remove()));
      input.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();save();}else if(e.key==='Escape')wrap.remove();});
      this.box.prepend(wrap);input.focus();
    }
    drawRow(r){
      const row=make('div','bookmark-row'+(r.enabled?'':' is-disabled'));row.dataset.bookmarkId=r.id;
      const active=make('input');active.type='checkbox';active.checked=r.enabled;active.setAttribute('aria-label','Lesezeichen aktivieren');
      active.addEventListener('change',()=>{r.enabled=active.checked;row.classList.toggle('is-disabled',!r.enabled);this.mark();});
      const title=make('input','bookmark-title');title.type='text';title.maxLength=250;title.placeholder='Titel';title.value=r.title;
      title.addEventListener('input',()=>{r.title=title.value;this.mark();});
      const url=make('input','bookmark-url');url.type='text';url.maxLength=4096;url.placeholder='about:config oder https://…';url.value=r.url;
      url.addEventListener('input',()=>{r.url=url.value;this.mark();});
      const placement=make('select');
      for(const [v,label] of [['toolbar','Lesezeichenleiste'],['menu','Lesezeichenmenü']]){
        const o=make('option','',label);o.value=v;placement.append(o);
      }placement.value=r.placement;
      placement.addEventListener('change',()=>{r.placement=placement.value;this.mark();});
      const folder=make('select');
      const updateFolders=()=>{
        folder.replaceChildren();const opt=make('option','','Direkt / kein Ordner');opt.value='';folder.append(opt);
        for(const f of this.config.folders){const o=make('option','',f);o.value=f;folder.append(o);}
        folder.value=r.folder;
      };updateFolders();folder.addEventListener('change',()=>{r.folder=folder.value;this.render();this.mark();});
      const del=button('×',()=>{this.config.entries=this.config.entries.filter(e=>e.id!==r.id);this.render();this.mark();},'danger');del.title='Lesezeichen entfernen';
      row.append(active,title,url,placement,folder,del);return row;
    }
    render(){this.box.replaceChildren();
      const direct=this.config.entries.filter(r=>!r.folder);
      const directSection=make('section','bookmark-group');directSection.append(make('h4','','Direkt auf der Lesezeichenleiste / im Menü'));
      for(const r of direct)directSection.append(this.drawRow(r));
      this.box.append(directSection);
      for(const name of this.config.folders){
        const group=make('details','bookmark-group');group.open=true;
        const summary=make('summary','',`📁 ${name}`);group.append(summary);
        for(const r of this.config.entries.filter(r=>r.folder===name))group.append(this.drawRow(r));
        const actions=make('div','row bookmark-group-actions');
        actions.append(button('＋ Lesezeichen',()=>this.add(name)));
        actions.append(button('Ordner entfernen',()=>{
          if(this.config.entries.some(r=>r.folder===name)&&!confirm(`Ordner „${name}“ mit allen Einträgen entfernen?`))return;
          this.config.folders=this.config.folders.filter(f=>f!==name);
          this.config.entries=this.config.entries.filter(r=>r.folder!==name);
          this.render();this.mark();
        },'danger'));
        group.append(actions);this.box.append(group);
      }
      this.validate();
    }
  }
  let globalEditor=null,modalEditor=null,modalId=null;
  async function init(){const config=await jsonRequest('/api/bookmarks');
    globalEditor=new BookmarksEditor($('bookmarksEditor'),$('bookmarksCount'),$('bookmarksStatus'),
      $('bookmarksAddDirect'),$('bookmarksAddFolder'),config,{autosave:true});
    window.bookmarkPanel.ready=true;if(typeof refreshBuildButton==='function')refreshBuildButton();
  }
  async function openBuild(id){const d=await jsonRequest(`/api/builds/${id}/bookmarks`);
    modalId=id;
    $('editBookmarksTitle').textContent=`Lesezeichen · Firefox ${d.version} (${d.flavor})`;
    $('editBookmarksSnapshot').textContent=d.complete_snapshot?'Lesezeichen-Auswahl dieses Builds geladen.':
      'Älteres Paket ohne Lesezeichen-Snapshot: fünf Standardlinks werden als bearbeitbarer Vorschlag angezeigt. Erst Speichern ändert das ZIP.';
    modalEditor?.detach();modalEditor=new BookmarksEditor($('editBookmarksEditor'),$('editBookmarksCount'),$('editBookmarksStatus'),
      $('editBookmarksAddDirect'),$('editBookmarksAddFolder'),d.config,{onChange:v=>$('editBookmarksSave').disabled=!v||!d.editable});
    $('editBookmarksSave').disabled=!d.editable;
    $('editBookmarksDialog').showModal();
  }
  async function saveBuild(){if(!modalEditor||!modalId)return;
    $('editBookmarksSave').disabled=true;
    try{const config=await modalEditor.flush();modalEditor.notify('ZIP wird ohne Firefox-Kompilierung aktualisiert …');
      const d=await jsonRequest(`/api/builds/${modalId}/bookmarks`,jsonOptions(config));
      $('editBookmarksDialog').close();modalEditor.detach();modalEditor=null;modalId=null;
      if(typeof loadBuilds==='function')await loadBuilds();alert(`Lesezeichen gespeichert. Neues ZIP: ${d.artifact_name}`);
    }catch(e){modalEditor.notify(e.message,true);$('editBookmarksSave').disabled=modalEditor.invalid;}
  }
  function closeModal(){if(modalEditor?.changed&&!confirm('Nicht gespeicherte Lesezeichenänderungen verwerfen?'))return;
    modalEditor?.detach();modalEditor=null;modalId=null;$('editBookmarksDialog').close();}
  async function doImport(){const file=$('bookmarksImportFile').files[0];if(!file){globalEditor.notify('Bitte zuerst eine Datei auswählen.',true);return;}
    if(file.size>2_000_000){globalEditor.notify('Datei zu groß (maximal 2 MB).',true);return;}
    const mode=$('bookmarksImportMode').value;
    if(mode==='replace'&&!confirm('Vorhandene Lesezeicheneinträge ersetzen? Ordner bleiben erhalten.'))return;
    const buttonEl=$('bookmarksImport');buttonEl.disabled=true;
    try{await globalEditor.flush();
      const d=await jsonRequest('/api/bookmarks/import',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({filename:file.name,content:await file.text(),mode})});
      globalEditor.detach();globalEditor=new BookmarksEditor($('bookmarksEditor'),$('bookmarksCount'),$('bookmarksStatus'),
        $('bookmarksAddDirect'),$('bookmarksAddFolder'),d.config,{autosave:true});
      globalEditor.notify(`${d.added} Lesezeichen importiert · ${d.skipped} übersprungen · ${d.flattened} aus Unterordnern zusammengefasst.`);
      $('bookmarksImportFile').value='';
    }catch(e){globalEditor.notify('Import fehlgeschlagen: '+e.message,true);}finally{buttonEl.disabled=false;}
  }
  async function doExport(){try{const data=await globalEditor.flush();
    const blob=new Blob([JSON.stringify(data,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob);
    const a=make('a');a.href=url;a.download='firefox_webbrowser_build_lesezeichen.json';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }catch(e){globalEditor.notify('Export fehlgeschlagen: '+e.message,true);}}
  document.addEventListener('DOMContentLoaded',()=>{
    $('bookmarksImport').addEventListener('click',doImport);
    $('bookmarksExport').addEventListener('click',doExport);
    $('editBookmarksSave').addEventListener('click',saveBuild);
    $('editBookmarksCancel').addEventListener('click',closeModal);
    $('editBookmarksDialog').addEventListener('cancel',e=>{e.preventDefault();closeModal();});
  });
  window.bookmarkPanel={init,flush:async()=>{
    if(!globalEditor)throw new Error('Lesezeichen nicht geladen');return globalEditor.flush();
  },openBuild,ready:false};
})();
