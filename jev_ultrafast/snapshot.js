(() => {
  if (!document.body) return null;
  const cache = window.__jevFast ||= {ids:new WeakMap(), nodes:new Map(), next:1};
  const identity = e => {
    if (!cache.ids.has(e)) cache.ids.set(e,cache.next++);
    const id=cache.ids.get(e); cache.nodes.set(id,e); return id;
  };
  cache.facts ||= new Map();
  for (const [id,e] of cache.nodes) if (!e.isConnected) { cache.nodes.delete(id); cache.facts.delete(id); }
  const safe = e => !['password','file','hidden'].includes(e.type);
  const visible = e => !e.closest('[aria-hidden="true"],[inert]') &&
    e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  const name = (e,seen=new Set()) => {
    if (!e || seen.has(e)) return '';
    seen.add(e);
    const referenced=(e.getAttribute('aria-labelledby')||'').split(/\s+/)
      .map(id=>name(document.getElementById(id),seen)).filter(Boolean).join(' ');
    return referenced || e.getAttribute('aria-label') ||
      [...(e.labels||[])].map(l=>name(l,seen)).filter(Boolean).join(' ') ||
      (['button','submit','reset'].includes(e.type) ? e.value : '') || e.getAttribute('alt') ||
      (e.tagName==='INPUT' ? '' : [...e.childNodes].map(n=>n.nodeType===3 ? n.textContent :
        n.nodeType===1 && n.getAttribute('aria-hidden')!=='true' ? name(n,seen) : '').join(' ').trim()) ||
      e.getAttribute('title') || e.getAttribute('placeholder') || '';
  };
  const roles=['button','link','checkbox','radio','switch','tab','menuitem','menuitemradio',
    'option','gridcell','combobox','textbox','searchbox','spinbutton'];
  const selector='a[href],button,input,textarea,select,summary,[contenteditable="true"],'+
    roles.map(role=>'[role="'+role+'"]').join(',');
  const role = e => {
    const explicit=e.getAttribute('role');
    if (roles.includes(explicit)) return explicit;
    if (e.tagName==='BUTTON' || e.tagName==='SUMMARY') return 'button';
    if (e.tagName==='A') return 'link';
    if (e.tagName==='SELECT') return 'combobox';
    if (e.tagName==='TEXTAREA' || e.isContentEditable) return 'textbox';
    if (e.tagName==='INPUT') {
      if (['checkbox','radio'].includes(e.type)) return e.type;
      if (['button','submit','reset','image'].includes(e.type)) return 'button';
      if (e.type==='search') return 'searchbox';
      if (e.type==='number') return 'spinbutton';
      if (['text','email','url','tel'].includes(e.type)) return 'textbox';
    }
    return null;
  };
  cache.pageKey=()=>[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    [...document.querySelectorAll('input,textarea,select')].filter(safe)
      .map(e=>[identity(e),e.value,e.checked,e.selectedIndex,e.disabled,e.readOnly])];
  cache.guard=e=>{
    if (!e?.isConnected || !visible(e)) return null;
    const scope=e.closest('form,dialog,[role="dialog"],article,li,tr,[role="row"]') || e.parentElement;
    return [identity(e),role(e),name(e),e.value??null,e.checked??null,e.selectedIndex??null,
      e.readOnly??null,e.matches(':disabled'),e.getAttribute('aria-disabled'),
      e.getAttribute('aria-expanded'),e.getAttribute('aria-checked'),e.getAttribute('aria-selected'),
      e.getAttribute('href'),scope?.innerText?.slice(0,6000)||''];
  };
  const group=e=>{
    const scope=e.closest('fieldset,form,nav,section,article,li,tr,[role="group"],[role="dialog"]');
    if (!scope) return '';
    const label=scope.getAttribute('aria-label') || scope.querySelector('legend,h1,h2,h3')?.textContent ||
      (['LI','TR'].includes(scope.tagName) ? scope.innerText : '') || scope.tagName.toLowerCase();
    return label.trim().replace(/\s+/g,' ').slice(0,80);
  };
  const hittable=(e,r)=>{
    const hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
    return hit===e || e.contains(hit);
  };
  const modal=[...document.querySelectorAll('dialog[open],[role="dialog"]')].reverse().find(e=>{
    const r=e.getBoundingClientRect();
    return visible(e) && e.getAttribute('aria-modal')!=='false' && r.width>0 && r.height>0 && hittable(e,r);
  });
  const actions=[], offscreen=[], controls=[];
  let omittedOffscreen=0, omittedControls=0;
  const fact=c=>{ if (controls.length<250) controls.push(c); else omittedControls++; };
  for (const e of document.querySelectorAll(selector)) {
    if (!safe(e)) continue;
    const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2, rname=role(e);
    if (!rname) continue;
    if (!visible(e) || r.width<=0 || r.height<=0) {
      const prior=cache.facts.get(cache.ids.get(e));
      if (prior) fact({...prior,observable:false});
      continue;
    }
    const inViewport=x>=0 && y>=0 && x<innerWidth && y<innerHeight;
    if (rname==='gridcell' && e.querySelector('button,[role="button"]')) continue;
    const base={node:identity(e),role:rname,label:name(e)||rname,
      rect:{x:r.x,y:r.y,w:r.width,h:r.height}};
    if (e.tagName==='SELECT') base.control_value=String(e.value);
    const section=group(e); if (section) base.group=section;
    for (const key of ['checked','selected','expanded']) {
      const value=e.getAttribute('aria-'+key);
      if (value!==null) base[key]=value;
    }
    if (['checkbox','radio'].includes(e.type)) base.checked=String(e.checked);
    const value='value' in e ? String(e.value) :
      e.isContentEditable || rname==='combobox' ? e.innerText.trim() : '';
    const binding={node:base.node,role:rname,label:base.label,kind:'control',...(section?{group:section}:{})};
    cache.facts.set(base.node,binding);
    const observable=(!modal || modal.contains(e)) && (!inViewport || hittable(e,r));
    const disabled=e.matches(':disabled') || !!e.closest('[aria-disabled="true"]');
    fact(observable ? {...binding,observable:true,value,disabled,readonly:!!e.readOnly,
      ...(base.control_value!==undefined?{control_value:base.control_value}:{}),
      ...(base.checked!==undefined?{checked:base.checked}:{})} : {...binding,observable:false});
    if (!observable || disabled) continue;
    if (!inViewport && offscreen.length>=48) { omittedOffscreen++; continue; }
    if (!inViewport) {
      const value=e.tagName==='SELECT' ? [...e.selectedOptions].map(o=>o.label).join(', ') :
        'value' in e ? String(e.value) : '';
      offscreen.push({...base,kind:'scroll_to',position:'offscreen',value,label:'Reveal '+base.label});
    } else if (e.tagName==='SELECT') {
      for (const o of e.options) if (!o.selected && !o.disabled && !o.closest('optgroup[disabled]'))
        actions.push({...base,kind:'select',value:o.value,
          current_value:[...e.selectedOptions].map(o=>o.label).join(', '),label:base.label+' → '+o.label});
    } else {
      const editable=!e.readOnly && e.getAttribute('aria-readonly')!=='true' &&
        (['textbox','searchbox','spinbutton'].includes(rname) ||
          (rname==='combobox' && ['INPUT','TEXTAREA'].includes(e.tagName)));
      actions.push({...base,kind:editable?'fill':'click',value});
      if (editable) actions.push({...base,kind:'click',value,label:'Open '+base.label});
    }
  }
  const omittedActions=Math.max(0,actions.length-250);
  actions.splice(250);
  const available=Math.min(offscreen.length,250-actions.length);
  actions.push(...offscreen.slice(0,available));
  const words=[], walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
  const range=document.createRange(); let node,length=0;
  while ((node=walker.nextNode()) && length<6000) {
    const value=node.textContent.trim(), parent=node.parentElement;
    if (!value || !parent || parent.closest('script,style,noscript,template') || !visible(parent) ||
        (modal && !modal.contains(parent))) continue;
    range.selectNodeContents(node); const r=range.getBoundingClientRect();
    if (r.width>0 && r.height>0 && r.bottom>0 && r.top<innerHeight && r.right>0 && r.left<innerWidth) {
      words.push(value); length+=value.length;
    }
  }
  const text=words.join('\n').slice(0,6000), height=document.documentElement.scrollHeight;
  const page_key=cache.pageKey(), guards={};
  for (const a of actions) if (!(a.node in guards)) guards[a.node]=cache.guard(cache.nodes.get(a.node));
  // Compare meaning and identity. Geometry is always resolved and hit-tested just before input.
  const semantics=actions.map(({rect,...action})=>action);
  const marker=[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    document.title,text,semantics,page_key[6],controls];
  // Terminal decisions do not address nodes. Compare all meanings and values, not replaceable DOM identity.
  const terminal_marker=[...marker.slice(0,8),semantics.map(({node,...a})=>a),page_key[6].map(a=>a.slice(1)),
    actions.map(a=>guards[a.node]?.slice(1,-1)||null),controls.map(({node,...c})=>c)];
  const omitted_actions=omittedActions+omittedOffscreen+offscreen.length-available;
  actions.forEach((a,i)=>a.id='e'+(i+1));
  if (scrollY+innerHeight<height-2) actions.push({id:'scroll_down',kind:'scroll',label:'Scroll down',delta:560});
  if (scrollY>0) actions.push({id:'scroll_up',kind:'scroll',label:'Scroll up',delta:-560});
  actions.push({id:'wait',kind:'wait',label:'Wait for the page to update'});
  return {url:location.href,title:document.title,document_id:performance.timeOrigin,modal_open:!!modal,
    ready_state:document.readyState,w:innerWidth,h:innerHeight,text,
    scroll:{y:scrollY,height},actions,controls,marker,terminal_marker,page_key,guards,omitted_actions,
    omitted_controls:omittedControls};
})()
