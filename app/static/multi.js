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
async function action(work, success='Saved.') {
  if (pending) return;
  pending = true;
  try { await work(); await refresh(); message(success); }
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
  if (pet) $('pet-album').textContent = pet.deleting ? 'Deletion pending; the next sync will retry cleanup.' : pet.album_error ? 'Album sync needs attention: ' + pet.album_error : pet.album ? 'Shared album: ' + pet.name + ' · Last synced: ' + (pet.album_synced_at ? new Date(pet.album_synced_at).toLocaleString() : 'pending') : snapshot.album_sharing?.enabled ? 'Shared album creation pending.' : 'Shared albums are disabled.';
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
function button(el, label, fn, success='Saved.') {
  const b = document.createElement('button'); b.textContent = label;
  b.dataset.enrollment = 'true';
  b.onclick = () => action(fn, success); el.append(b);
}
function renderRefs() {
  const petId = $('pet').value;
  const refs = petId ? snapshot.pets[petId].refs : snapshot.negatives;
  $('reference-heading').textContent = petId ? 'References for ' + snapshot.pets[petId].name + ' (' + refs.length + ')' : 'Not my pets — negative references (' + refs.length + ')';
  $('references').replaceChildren();
  if (!refs.length) $('references').textContent = petId ? 'No reference photos yet.' : 'No negative references yet. Mark an uncertain match “Not my pets” to add one here.';
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
    }, 'Saved as a pet reference. Rescan its date range to apply this feedback.');
    button(el, 'Not my pets', () => api('/references','POST',{account:ref.account,asset_id:ref.asset_id}), 'Saved to “Not my pets / negatives”. Rescan its date range to apply this feedback.');
    $('review').append(el);
  }
  if (!(snapshot.review || []).length) $('review').textContent = 'No uncertain matches awaiting review.';
  syncControls();
}
function syncControls() {
  const running = snapshot.scan.running;
  $('start').disabled = running;
  $('sync-albums').disabled = running || !snapshot.album_sharing?.enabled;
  $('album-sharing').textContent = snapshot.album_sharing?.enabled ? 'Shared albums are owned by ' + snapshot.album_sharing.owner + '. Matching photos are shared with all configured accounts. Album sync uses existing pet assignments; no GPU scan is needed.' : 'Shared albums are disabled. Configure IMMICH_ALBUM_OWNER_ACCOUNT to enable them.';
  $('stop').disabled = !running;
  $('review-status').textContent = running ? 'A scan is running. Stop it and wait for “Stopped” before editing references or reviewing matches. Pending work is saved.' : 'Ready to review. Negative references are available under Enrollment → Not my pets / negatives.';
  const albumErrors = Object.entries(snapshot.album_sharing?.errors || {});
  if (albumErrors.length) $('album-sharing').textContent += ' Needs attention: ' + albumErrors.map(([name,error]) => name + ': ' + error).join('; ');
  document.querySelectorAll('[data-enrollment], #create-form button, #edit-form button, #reference-form button, #remove-pet, #delete-pet, #reset-pet').forEach(b => {
    b.disabled = running;
    b.title = running ? 'Stop the current scan before editing enrollment.' : '';
  });
}
function renderProgress() {
  const scan = snapshot.scan;
  const phaseNames = {syncing_albums:'Syncing shared pet albums',discovering:'Discovering assets',loading_models:'Loading GPU models',training:'Training from shared references',processing:'Tagging assets',unloading_models:'Releasing GPU models'};
  $('scan-state').textContent = scan.running ? (phaseNames[scan.phase] || 'Scanning') + (scan.current_account ? ' · ' + scan.current_account : '') : scan.error ? 'Scan failed: ' + scan.error : scan.cancelled ? 'Stopped. Pending work is saved.' : scan.finished_at ? 'Scan finished.' : 'Idle — no scan has run since startup.';
  const total = scan.total || 0, completed = scan.completed || 0, retry = scan.retry || 0, attempted = completed + retry;
  const percent = total ? Math.min(100, Math.floor(attempted / total * 100)) : 0;
  const bar = $('scan-progress');
  if (scan.running && ['discovering','syncing_albums'].includes(scan.phase)) bar.removeAttribute('value');
  else bar.value = percent;
  $('scan-detail').textContent = scan.running && ['discovering','syncing_albums'].includes(scan.phase) ? (scan.phase === 'syncing_albums' ? 'Reconciling each pet’s album and account contributions.' : 'Building this run’s queue; total becomes available after discovery.') : total ? percent + '% attempted · ' + completed.toLocaleString() + ' completed · ' + retry.toLocaleString() + ' queued for retry · ' + Math.max(0,total-attempted).toLocaleString() + ' remaining · ' + total.toLocaleString() + ' assets in this run' : 'No queued work for this run.';
  const elapsed = scan.started_at ? Math.max(0, Math.floor(((scan.running ? Date.now() : Date.parse(scan.finished_at)) - Date.parse(scan.started_at)) / 1000)) : 0;
  $('scan-timing').textContent = scan.started_at ? 'Started ' + new Date(scan.started_at).toLocaleString() + ' · Elapsed ' + Math.floor(elapsed/60) + 'm ' + elapsed%60 + 's · Updates every 5 seconds' : 'Updates every 5 seconds';
  $('progress').replaceChildren();
  const table = document.createElement('table');
  const heading = document.createElement('tr');
  ['Account','Pending','Retry','Done','Error'].forEach(text => {const th=document.createElement('th');th.textContent=text;heading.append(th);}); table.append(heading);
  for (const id of snapshot.accounts) {
    const state = snapshot.progress[id] || {}, jobs = state.jobs || {}, row=document.createElement('tr');
    [id,jobs.pending||0,jobs.retry||0,jobs.done||0,scan.accounts[id] || state.error || ''].forEach(text=>{const td=document.createElement('td');td.textContent=text;row.append(td);});table.append(row);
  }
  $('progress').append(table);
  syncControls();
}
let refsSignature = '';
let refreshSequence = 0;
async function refresh() {
  const sequence = ++refreshSequence;
  const next = await api('/state');
  if (sequence !== refreshSequence) return;
  snapshot = next;
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
$('sync-albums').onclick = () => action(()=>api('/albums/sync','POST'), 'Shared album synchronization started.');
$('stop').onclick = () => action(()=>api('/scan/stop','POST'), 'Stop requested. Waiting for active work to finish; pending work is saved.');
$('remove-pet').onclick = () => {if(confirm('Remove this enrollment and delete its managed shared album? Original photos and existing Immich face assignments will remain.')) action(()=>api('/pets/'+$('pet').value+'?local_only=true','DELETE'));};
$('delete-pet').onclick = () => {if(confirm('Delete this pet, its managed shared album, and its person records from EVERY linked account? Original photos will remain.')) action(()=>api('/pets/'+$('pet').value,'DELETE'));};
$('reset-pet').onclick = () => {if(confirm('Remove this pet’s person record and tags in '+$('source-account').value+'? Shared references stay. Run a dated rescan to retag.')) action(()=>api('/pets/'+$('pet').value+'/reset/'+encodeURIComponent($('source-account').value),'POST'));};
refresh().catch(e=>message(e.message,true));
setInterval(()=>refresh().catch(e=>message(e.message,true)),5000);
