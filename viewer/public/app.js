import {selectRender} from './renders.mjs';
import {sourcePanel} from './sources.mjs';

const $ = id => document.getElementById(id);
const state = {project: null, video: null, projectId: null, render: null, assets: new Map()};
const node = (tag, className, text) => {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text != null) element.textContent = text;
  return element;
};
const show = (id, visible) => { $(id).hidden = !visible; };
const count = value => new Intl.NumberFormat().format(value);
const mediaUrl = sha => state.assets.get(sha)?.sourceUrl || `/api/projects/${encodeURIComponent(state.project.id)}/assets/${sha}`;
const date = value => value ? (value.length === 10 ? new Intl.DateTimeFormat(undefined, {dateStyle: 'medium', timeZone: 'UTC'}).format(new Date(value)) : new Date(value).toLocaleString()) : 'Date unrecorded';
function notice(message) { $('notice').textContent = message; show('notice', Boolean(message)); }
async function api(path, init) {
  const response = await fetch(path, {...init, cache: 'no-store'});
  if (response.status === 401) { show('login', true); show('workspace', false); show('project-label', false); show('logout', false); }
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'The request could not be completed');
  return data;
}
function uniqueFiles(files) {
  return [...new Map(files.map(file => [file.sha256 + ':' + file.path + ':' + file.role, file])).values()];
}
function renderFiles(video) {
  return [...new Map(video.files.filter(file => file.role === 'render' && state.assets.has(file.sha256)).map(file => [file.source_render_id || file.sha256, file])).values()];
}
function drawList() {
  const target = $('video-list'); target.replaceChildren();
  const query = $('search').value.trim().toLowerCase();
  for (const video of state.project.videos.filter(v => v.title.toLowerCase().includes(query))) {
    const button = node('button', 'video-item', video.title);
    button.type = 'button'; button.setAttribute('aria-pressed', String(video.id === state.video?.id));
    const renders = Math.max(renderFiles(video).length, state.project.source_catalog?.find(item => item.video_id === video.id)?.render_count || 0);
    const revisions = state.project.source_catalog?.find(item => item.video_id === video.id)?.revisions || 0;
    button.append(node('small', null, `${renders ? renders + ' render' + (renders === 1 ? '' : 's') : 'No render in R2'} · ${revisions ? revisions + ' source versions' : uniqueFiles(video.files).length + ' files'}`));
    button.addEventListener('click', () => chooseVideo(video)); target.append(button);
  }
  if (!target.children.length) target.append(node('p', 'subtle', 'No videos match this search.'));
}
function fileRow(file, image = false) {
  const asset = state.assets.get(file.sha256), row = node('li', 'file-row');
  const name = file.path || asset?.name || file.sha256;
  if (image && asset?.mime.startsWith('image/')) {
    const thumbnail = node('img'); thumbnail.src = mediaUrl(file.sha256); thumbnail.alt = ''; thumbnail.loading = 'lazy'; row.append(thumbnail);
  }
  row.append(node('span', 'file-name', name));
  if (!asset) { row.append(node('span', 'subtle', 'Unavailable in this snapshot')); return row; }
  row.append(node('span', 'file-size', asset.bytes < 1e6 ? `${(asset.bytes / 1000).toFixed(1)} KB` : `${(asset.bytes / 1e6).toFixed(1)} MB`));
  const download = node('a', null, 'Download'); download.href = mediaUrl(file.sha256) + '?download=1'; row.append(download);
  if (file.role === 'transcript' || /\.(md|txt|srt|vtt|js|json|py|tsx?|jsx|yml|yaml|svg|mjs)$/.test(name)) {
    const preview = node('button', null, 'Read'); preview.type = 'button';
    preview.addEventListener('click', async () => {
      const existing = row.querySelector('pre');
      if (existing) { existing.remove(); return; }
      preview.disabled = true;
      try {
        const response = await fetch(mediaUrl(file.sha256));
        if (!response.ok) throw new Error('Source file could not be loaded');
        row.append(node('pre', 'source-preview', await response.text()));
      } catch (error) { notice(error.message); }
      finally { preview.disabled = false; }
    }); row.append(preview);
  }
  return row;
}
function sourceGroups(video) {
  const groups = node('div', 'source-groups');
  for (const [role, label] of [['transcript', 'Transcripts & captions'], ['source', 'Editable sources & references'], ['audio', 'Audio'], ['raw', 'Images & raw inputs'], ['render', 'Render versions']]) {
    const files = uniqueFiles(video.files).filter(file => (file.role || 'source') === role);
    if (!files.length) continue;
    const details = node('details'), summary = node('summary', null, `${label} (${files.length})`);
    details.append(summary);
    details.addEventListener('toggle', () => {
      if (!details.open || details.querySelector('ul')) return;
      const list = node('ul', 'file-list'); files.forEach(file => list.append(fileRow(file))); details.append(list);
    }); groups.append(details);
  }
  return groups;
}
function updateLocation() {
  const query = new URLSearchParams({project: state.project.id, video: state.video.id});
  if (state.render) query.set('render', state.render);
  if (state.renderRecord) query.set('renderRecord', state.renderRecord);
  history.replaceState(null, '', '/?' + query);
}
async function chooseVideo(video) {
  state.video = video; state.render = null; state.renderRecord = null; drawList();
  const projectId = state.project.id;
  let sources = {revisions: [], renders: [], checks: []};
  try {
    let offset = 0, catalogRevision = null;
    do {
      const page = await api(`/api/sources/${encodeURIComponent(projectId)}/list?video=${encodeURIComponent(video.id)}&offset=${offset}`);
      if (catalogRevision !== null && page.catalog_revision !== catalogRevision) throw new Error('Source catalog changed while loading. Select the video again.');
      catalogRevision = page.catalog_revision;
      sources.revisions.push(...page.revisions); sources.renders.push(...page.renders); sources.checks.push(...(page.checks || [])); offset = page.next_offset;
      if (sources.revisions.length + sources.renders.length > 4000) throw new Error('Source history exceeds the browser limit; use the CLI.');
    } while (offset != null);
  } catch (error) { sources.error = error.message; }
  if (state.video.id !== video.id || state.project.id !== projectId) return;
  const newer = new Set(sources.renders.map(r => r.output.sha256));
  video = {...video, files: [...video.files.filter(f => f.role !== 'render' || !newer.has(f.sha256)), ...sources.renders.map(r => {
    state.assets.set(r.output.sha256, {...r.output, mime: 'video/mp4', name: r.output.path, sourceUrl: `/api/sources/${encodeURIComponent(projectId)}/render/${r.id}/files/0`});
    return {...r.output, source_render_id: r.id, source_revision_id: r.source_revision_id, association: r.association};
  })]};
  const sourceView = sourcePanel(projectId, video, sources, api, null);
  const target = $('detail'); target.replaceChildren();
  const heading = node('div', 'detail-heading'); heading.append(node('h2', null, video.title)); target.append(heading);
  const renders = renderFiles(video);
  if (renders.length) {
    const stage = node('div', 'player-stage'), player = node('video'); player.controls = true; player.preload = 'metadata'; player.playsInline = true;
    stage.append(player); target.append(stage);
    const select = node('select', 'render-select'); select.setAttribute('aria-label', 'Render version');
    renders.forEach((file, index) => {
      const option = node('option', null, `${index + 1} · ${file.path.split('/').slice(-2).join('/')} · ${file.sha256.slice(0, 8)}${file.reviewStatus ? ' · ' + file.reviewStatus : ''}`);
      option.value = file.source_render_id || file.sha256; select.append(option);
    });
    const preferred = renders.find(file => file.source_render_id === new URLSearchParams(location.search).get('renderRecord')) || selectRender(renders, new URLSearchParams(location.search).get('render'));
    state.render = preferred.sha256; state.renderRecord = preferred.source_render_id || null; select.value = preferred.source_render_id || state.render; player.src = mediaUrl(state.render); sourceView.selectRender(preferred);
    select.addEventListener('change', () => {
      const file = renders.find(file => (file.source_render_id || file.sha256) === select.value);
      state.render = file.sha256; state.renderRecord = file.source_render_id || null; sourceView.selectRender(file);
      player.pause(); player.src = mediaUrl(state.render); player.load(); updateLocation();
    });
    player.addEventListener('error', () => notice('This render could not be streamed. Refresh the project or download the file.'));
    target.append(select);
  } else target.append(node('div', 'empty', 'No render in R2. Sources are available below.'));
  const related = state.project.relationships.filter(r => (r.from_key === video.id || r.to_key === video.id) && r.relationship !== 'published_as');
  const relatedVideos = new Set(related.map(r => r.from_key === video.id ? r.to_key : r.from_key));
  for (const family of related.filter(r => r.relationship === 'has_variant')) {
    state.project.relationships.filter(r => r.relationship === 'has_variant' && r.from_key === family.from_key).forEach(r => relatedVideos.add(r.to_key));
  }
  const links = node('div', 'related');
  for (const id of relatedVideos) {
    const other = state.project.videos.find(v => v.id === id);
    if (!other || other.id === video.id) continue;
    const button = node('button', null, other.title); button.type = 'button'; button.addEventListener('click', () => chooseVideo(other)); links.append(button);
  }
  if (links.children.length) target.append(node('h3', null, 'Related editions'), links);
  const posts = allPosts().filter(p => p.videoId === video.id);
  if (posts.length) { target.append(node('h3', null, 'Posting history')); posts.forEach(post => target.append(postRow(post))); }
  if (video.metadata?.transcript) {
    const transcript = node('details');
    transcript.append(node('summary', null, 'Transcript'), node('pre', 'source-preview', video.metadata.transcript));
    target.append(transcript);
  }
  if (sources.error) target.append(node('p', 'notice', 'Source view pending: ' + sources.error));
  target.append(sourceView.element);
  const historical = node('details'); historical.append(node('summary', null, 'Historical file library · version membership unverified'), sourceGroups(video)); target.append(historical);
  updateLocation();
}
function allPosts() {
  const posts = new Map(state.project.posts.map(post => [post.id, {...post}]));
  for (const observation of [...(state.project.observations || [])].sort((a, b) => Date.parse(a.observedAt) - Date.parse(b.observedAt))) {
    const existing = posts.get(observation.postId) || {id: observation.postId, observations: []};
    posts.set(observation.postId, {...existing, ...observation, observations: [...existing.observations, observation]});
  }
  return [...posts.values()];
}
function postRow(post) {
  const article = node('article', 'post'), top = node('div', 'post-top');
  top.append(node('h3', null, post.title || post.platform || post.id));
  if (/^https:\/\//.test(post.url || '')) { const link = node('a', null, 'View post ↗'); link.href = post.url; link.target = '_blank'; link.rel = 'noopener noreferrer'; top.append(link); }
  article.append(top, node('div', 'status', `${post.platform || ''} · ${post.status || 'unknown'} · ${date(post.postedAt || post.postedDate || post.scheduledAt)}`));
  const metrics = node('div', 'metrics');
  for (const key of ['views', 'likes', 'comments']) {
    let value = post[key];
    const recorded = post.metrics?.filter(m => m.metric === key).at(-1);
    if (value == null && recorded) value = recorded.value_number;
    const group = node('span'); group.append(node('strong', null, value == null ? '—' : count(value)), node('span', null, key)); metrics.append(group);
  }
  article.append(metrics);
  const details = node('details'); details.append(node('summary', null, 'Saved observations'), node('pre', null, JSON.stringify({observations: post.observations, metrics: post.metrics}, null, 2))); article.append(details);
  return article;
}
function drawRaw() {
  const target = $('raw-files'); target.replaceChildren(); target.className = 'asset-grid';
  const query = $('raw-search').value.trim().toLowerCase();
  const files = uniqueFiles(state.project.raw || []);
  files.filter(f => f.path.toLowerCase().includes(query)).forEach(file => target.append(fileRow(file, true)));
  if (!target.children.length) target.append(node('p', 'subtle', files.length ? 'No matching files.' : 'Add reusable inputs to the production workspace’s raw directory, then import them into Outpost.'));
}
async function loadProject(id) {
  state.projectId = id; notice('Loading project…');
  const result = await api('/api/projects/' + encodeURIComponent(id));
  if (state.projectId !== id) return;
  state.project = result; state.assets = new Map(result.assets.map(a => [a.sha256, a]));
  const sourceCount = (result.source_catalog || []).reduce((total, video) => total + video.revisions, 0);
  $('project-title').textContent = result.name; $('counts').textContent = `${count(result.videos.length)} video${result.videos.length === 1 ? '' : 's'} · ${sourceCount ? count(sourceCount) + ' source versions' : count(result.assets.length) + ' preserved files'}`;
  $('archive-info').textContent = `Archive snapshot ${result.snapshot_id}`;
  $('search').value = ''; drawList(); drawRaw();
  $('posts').replaceChildren(...allPosts().map(postRow));
  if (!$('posts').children.length) $('posts').append(node('p', 'subtle', 'No posting observations recorded.'));
  $('ads').replaceChildren(...result.ads.map(ad => { const item = node('article', 'ad'); item.append(node('h3', null, ad.ad_name || ad.campaign_name || ad.ad_id || 'Ad usage'), node('pre', null, JSON.stringify(ad, null, 2))); return item; }));
  if (!$('ads').children.length) $('ads').append(node('p', 'subtle', 'No ad usage recorded.'));
  const desired = new URLSearchParams(location.search).get('video');
  const selected = result.videos.find(v => v.id === desired) || result.videos.find(v => renderFiles(v).length) || result.videos[0];
  if (selected) await chooseVideo(selected); else $('detail').replaceChildren(node('p', 'empty', 'No videos recorded.'));
  show('login', false); show('workspace', true); show('project-label', true); show('logout', true); notice('');
}
async function start() {
  const fragment = new URLSearchParams(location.hash.slice(1)), launch = fragment.get('ticket');
  if (launch) { history.replaceState(null, '', location.pathname + location.search); await api('/api/session', {method: 'POST', body: JSON.stringify({ticket: launch})}); }
  const index = await api('/api/projects');
  $('project').replaceChildren(...index.projects.map(project => { const option = node('option', null, project.name); option.value = project.id; return option; }));
  const desired = new URLSearchParams(location.search).get('project');
  const project = index.projects.find(p => p.id === desired) || index.projects[0];
  if (project) { $('project').value = project.id; await loadProject(project.id); }
}
$('login-form').addEventListener('submit', async event => {
  event.preventDefault();
  try { await api('/api/session', {method: 'POST', body: JSON.stringify({key: $('key').value})}); $('key').value = ''; await start(); }
  catch (error) { notice(error.message); }
});
$('project').addEventListener('change', () => loadProject($('project').value).catch(error => notice(error.message)));
$('search').addEventListener('input', drawList); $('raw-search').addEventListener('input', drawRaw);
for (const button of document.querySelectorAll('nav button')) button.addEventListener('click', () => {
  for (const other of document.querySelectorAll('nav button')) other.removeAttribute('aria-current');
  button.setAttribute('aria-current', 'page');
  for (const name of ['videos', 'raw', 'posts', 'ads']) show(name === 'videos' ? 'video-view' : name + '-view', name === button.dataset.view);
  document.querySelector('video')?.pause();
});
$('logout').addEventListener('click', async () => {
  try { await api('/api/logout', {method: 'POST'}); document.querySelector('video')?.pause(); state.project = null; $('detail').replaceChildren(); show('workspace', false); show('project-label', false); show('logout', false); show('login', true); }
  catch (error) { notice(error.message); }
});
start().catch(error => notice(error.message));
