const node=(tag,text,className)=>{const el=document.createElement(tag);if(text!=null)el.textContent=text;if(className)el.className=className;return el;};
const link=(text,href)=>{const el=node('a',text);el.href=href;return el;};

export function exactSource(records,render) {
  return records.revisions.find(source=>source.id===render?.source_revision_id)?.id || null;
}

export function sourcePanel(projectId,video,records,api,initialRender) {
  const root=node('section',null,'exact-sources'),base='/api/sources/'+encodeURIComponent(projectId);
  root.append(node('h3','Source versions'));
  if(!records.revisions.length){root.append(node('p','Exact source versions have not been cataloged for this video. Historical files remain below.','subtle'));return {element:root,selectRender(){}};}
  const select=node('select',null,'render-select');select.setAttribute('aria-label','Source revision');
  for(const revision of records.revisions){const option=node('option',(revision.label||'Source version')+' · '+revision.id.slice(0,12));option.value=revision.id;select.append(option);}
  const association=node('p',null,'subtle'),detail=node('div');let selectedRender=initialRender,requestNumber=0;
  root.append(select,association,detail);
  async function draw(){
    const serial=++requestNumber,id=select.value;detail.replaceChildren(node('p','Loading this source version…','subtle'));
    const sourceId=exactSource(records,selectedRender);
    association.textContent=sourceId===id ? (selectedRender.association?.status==='frozen_inputs'?'Selected render is linked to these frozen inputs.':'Selected render references this source version; the historical match is unverified.') : 'This source version is being viewed independently. No source-to-render match is asserted.';
    try{
      const revision=await api(base+'/revision/'+id);if(serial!==requestNumber)return;
      const m=revision.manifest,total=m.files.reduce((n,f)=>n+f.bytes,0);
      const checks=(records.checks||[]).filter(check=>check.source_revision_id===id);
      const evidence=kind=>{
        const check=checks.filter(check=>check.kind===kind&&(kind!=='render'||check.render_id===selectedRender?.source_render_id)).sort((a,b)=>b.observed_at.localeCompare(a.observed_at))[0];
        return check?`${check.status} · ${check.environment.os||'environment unrecorded'} · ${check.observed_at} · ${check.evidence}`:'Not recorded';
      };
      const facts=node('dl',null,'source-facts');
      for(const [label,value] of [['Files',`${m.files.length} · ${(total/1048576).toFixed(1)} MiB · archived with checksums`],['Dependencies',m.dependencies.length?m.dependencies.map(d=>`${d.name}: ${d.status}`).join(' · '):'Not declared; completeness unknown'],['Restore test',evidence('restore')],['Render test',evidence('render')],['Creative approval',evidence('creative_approval')],['Working tree',m.git.working_tree],['Git commit',m.git.commit||'Unrecorded']])facts.append(node('dt',label),node('dd',value));
      const actions=node('div',null,'source-actions');
      actions.append(link('Download manifest',base+'/revision/'+id+'?download=1'));
      if(m.files.length<=32&&total+JSON.stringify(revision).length<=16*1024*1024)actions.append(link('Download project ZIP',base+'/revision/'+id+'/package'));
      const command=node('pre',`outpost source restore ${projectId} ${id} --out ./restored-project`,'source-preview');
      const help=node('p','Restore to a fresh folder, or add --missing-only to fill missing files in a Git checkout. Changed files are never overwritten. Project commands and dependency installs are always manual.','subtle');
      const recipe=node('details');recipe.append(node('summary','Recipe, dependencies and provenance'),node('pre',JSON.stringify({recipe:m.recipe,dependencies:m.dependencies,git:m.git,lineage:m.lineage},null,2),'source-preview'));
      const files=node('ul',null,'file-list');
      m.files.forEach((file,index)=>{const row=node('li',null,'file-row');row.append(node('span',file.path,'file-name'),node('span',file.role+(file.language?' · '+file.language:''),'file-size'),link('Download',base+'/revision/'+id+'/files/'+index+'?download=1'));files.append(row);});
      detail.replaceChildren(facts,actions,command,help,recipe,files);
    }catch(error){if(serial===requestNumber)detail.replaceChildren(node('p','Source view pending: '+error.message,'notice'));}
  }
  select.addEventListener('change',draw);
  function selectRender(render){selectedRender=render;const source=exactSource(records,render);if(source)select.value=source;draw();}
  selectRender(initialRender);
  return {element:root,selectRender};
}
