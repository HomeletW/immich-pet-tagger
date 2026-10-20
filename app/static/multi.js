'use strict';
const $ = id => document.getElementById(id);
let snapshot = null;
let pending = false;
let displayedPet = null;
function message(text, error=false) {
  $('message').textContent = text;
  $('message').className = error ? 'error' : '';
}
async function api(path, method='GET', body) {
  const response = await fetch('/api/multi' + path, {
    method, headers: {'Content-Type':'application/json'},
    ...(body === undefined ? {} : {body:JSON.stringify(body)})
  });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail));
  return data;
}
async function action(work) {
  if (pending) return;
  pending = true;
  try { await work(); message('Saved.'); await refresh(); }
  catch (e) { message(e.message, true); }
  finally { pending = false; }
}
function options(id, entries, first) {
  const node = $(id), selected = node.value;
  node.replaceChildren();
  if (first) node.add(new Option(first, ''));
  entries.forEach(([value,label]) => node.add(new Option(label,value)));
  if ([...node.options].some(o => o.value === selected)) node.value = selected;
}
function petFields(force=false) {
  const id = $('pet').value, pet = snapshot.pets[id];
  $('pet-controls').hidden = !pet;
  if (pet && (force || displayedPet !== id)) {
    $('edit-name').value = pet.name;
    $('edit-description').value = pet.description || '';
    $('pet-since').value = pet.since || '';
    $('pet-until').value = pet.until || '';
  }
  if (pet) $('person-mappings').textContent = 'Linked accounts: ' + (Object.keys(pet.persons).join(', ') || 'Created on the first scan');
  displayedPet = id;
}
function card(ref) {
  const el = document.createElement('article'); el.className = 'card';
  const img = document.createElement('img'); img.loading = 'lazy';
  img.src = '/api/multi/thumb/' + encodeURIComponent(ref.account) + '/' + encodeURIComponent(ref.asset_id);
  img.alt = 'Reference from ' + ref.account;
  el.append(img);
  for (const text of [ref.account, ref.asset_id]) {const p = document.createElement('p'); p.textContent = text; el.append(p);}
  return el;
}
function button(el, label, fn) {
  const b = document.createElement('button'); b.textContent = label;
  b.onclick = () => action(fn); el.append(b);
}
function renderRefs() {
  const petId = $('pet').value;
  const refs = petId ? snapshot.pets[petId].refs : snapshot.negatives;
  $('references').replaceChildren();
  for (const ref of refs) {
    const el = card(ref);
    button(el, 'Remove reference', () => api('/references','DELETE',{...ref,pet_id:petId || null}));
    $('references').append(el);
  }
  $('review').replaceChildren();
  for (const ref of snapshot.review || []) {
    const el = card(ref);
    const p = document.createElement('p'); p.textContent = ref.taken.slice(0,10) + ' · ' + ref.matches.map(m => (snapshot.pets[m.pet]?.name || 'Unknown pet') + ' ' + Math.round(m.probability*100) + '%').join(', '); el.append(p);
    button(el, 'Use as selected pet reference', () => {
      if (!$('pet').value) throw new Error('Choose a pet first.');
      return api('/references','POST',{account:ref.account,asset_id:ref.asset_id,pet_id:$('pet').value});
    });
    button(el, 'Not my pets', () => api('/references','POST',{account:ref.account,asset_id:ref.asset_id}));
    $('review').append(el);
  }
}
function renderProgress() {
  const scan = snapshot.scan;
  $('scan-state').textContent = scan.running ? 'Scanning ' + (scan.current_account || 'references') + '…' : scan.error ? 'Scan failed: ' + scan.error : scan.cancelled ? 'Stopped. Pending work is saved.' : 'Idle. ' + (scan.finished_at ? 'Last finished: ' + new Date(scan.finished_at).toLocaleString() : '');
  $('progress').replaceChildren();
  const table = document.createElement('table');
  const heading = document.createElement('tr');
  ['Account','Pending','Retry','Done','Error'].forEach(text => {const th=document.createElement('th');th.textContent=text;heading.append(th);}); table.append(heading);
  for (const id of snapshot.accounts) {
    const state = snapshot.progress[id] || {}, jobs = state.jobs || {}, row=document.createElement('tr');
    [id,jobs.pending||0,jobs.retry||0,jobs.done||0,scan.accounts[id] || state.error || ''].forEach(text=>{const td=document.createElement('td');td.textContent=text;row.append(td);});table.append(row);
  }
  $('progress').append(table);
}
let refsSignature = '';
async function refresh() {
  snapshot = await api('/state');
  options('source-account',snapshot.accounts.map(x=>[x,x]));
  options('scan-account',snapshot.accounts.map(x=>[x,x]),'All accounts');
  options('pet',Object.entries(snapshot.pets).map(([id,p])=>[id,p.name]),'Not my pets / negatives');
  petFields(); renderProgress();
  const signature = JSON.stringify([snapshot.pets,snapshot.negatives,snapshot.review,$('pet').value]);
  if (signature !== refsSignature) {refsSignature=signature;renderRefs();}
}
$('pet').onchange = () => {petFields(true);renderRefs();};
$('create-form').onsubmit = event => {event.preventDefault();action(async()=>{
  const result=await api('/pets','POST',{name:$('new-name').value,description:$('new-description').value});
  await refresh();$('pet').value=result.id;petFields(true);renderRefs();$('create-form').reset();
});};
$('edit-form').onsubmit = event => {event.preventDefault();action(()=>api('/pets/'+$('pet').value,'PUT',{name:$('edit-name').value,description:$('edit-description').value,since:$('pet-since').value||null,until:$('pet-until').value||null}));};
$('reference-form').onsubmit = event => {event.preventDefault();action(async()=>{
  const match=$('asset').value.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i);
  if(!match) throw new Error('Enter a valid Immich asset ID or photo URL.');
  const bbox=$('bbox').value.trim() ? $('bbox').value.split(',').map(Number) : null;
  await api('/references','POST',{account:$('source-account').value,asset_id:match[0],pet_id:$('pet').value||null,bbox});
  $('asset').value='';$('bbox').value='';
});};
$('scan-form').onsubmit = event => {event.preventDefault();action(()=>api('/scan','POST',{account:$('scan-account').value||null,since:$('since').value||null,until:$('until').value||null}));};
$('stop').onclick = () => action(()=>api('/scan/stop','POST'));
$('remove-pet').onclick = () => {if(confirm('Remove this shared enrollment? Existing Immich tags will remain.')) action(()=>api('/pets/'+$('pet').value+'?local_only=true','DELETE'));};
$('delete-pet').onclick = () => {if(confirm('Delete this pet and its person records from EVERY linked Immich account?')) action(()=>api('/pets/'+$('pet').value,'DELETE'));};
$('reset-pet').onclick = () => {if(confirm('Remove this pet’s person record and tags in '+$('source-account').value+'? Shared references stay. Run a dated rescan to retag.')) action(()=>api('/pets/'+$('pet').value+'/reset/'+encodeURIComponent($('source-account').value),'POST'));};
refresh().catch(e=>message(e.message,true));
setInterval(()=>refresh().catch(e=>message(e.message,true)),5000);
