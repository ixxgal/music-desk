/* 音乐工具台 · 前端 */
(() => {
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const el = {
  input: $('#input'),
  btnGo: $('#btnGo'),
  chips: $('#chips'),
  selSrc: $('#selSrc'),
  resultsPanel: $('#resultsPanel'),
  resultsTitle: $('#resultsTitle'),
  resultsCount: $('#resultsCount'),
  results: $('#results'),
  tasksPanel: $('#tasksPanel'),
  tasks: $('#tasks'),
  taskBadge: $('#taskBadge'),
  library: $('#library'),
  libBadge: $('#libBadge'),
  libSize: $('#libSize'),
  libSearch: $('#libSearch'),
  libSelAll: $('#libSelAll'),
  libSelCount: $('#libSelCount'),
  libBulk: $('#libBulk'),
  btnBulkDl: $('#btnBulkDl'),
  btnBulkDel: $('#btnBulkDel'),
  btnBulkClear: $('#btnBulkClear'),
  player: $('#player'),
  audio: $('#audio'),
  videoView: $('#videoView'),
  playerTitle: $('#playerTitle'),
  playerSub: $('#playerSub'),
  status: $('#status'),
  toast: $('#toast'),
  modal: $('#settingsModal'),
  dropZone: $('#dropZone'),
  chkAutohide: $('#chkAutohide'),
  btnTaskRefresh: $('#btnTaskRefresh'),
  btnLibRefresh: $('#btnLibRefresh'),
  history: $('#history'),
  historyBadge: $('#historyBadge'),
  historyHint: $('#historyHint'),
  btnHistRefresh: $('#btnHistRefresh'),
  btnHistClear: $('#btnHistClear'),

  /* 更新 */
  updateBar: $('#updateBar'),
  ubVer: $('#ubVer'),
  ubNotes: $('#ubNotes'),
  btnUpdateNow: $('#btnUpdateNow'),
  btnUpdateLater: $('#btnUpdateLater'),
  setUpdateSrc: $('#setUpdateSrc'),
  setVersion: $('#setVersion'),
  btnCheckUpdate: $('#btnCheckUpdate'),
  updState: $('#updState'),
};

const SRC_LABEL = {
  auto: '综合搜索',
  bilibili: 'B站搜索',
  netease: '网易云搜索',
  qq: 'QQ音乐搜索',
  kugou: '酷狗搜索',
  ximalaya: '喜马拉雅搜索',
  douyin: '抖音搜索',
  kuaishou: '快手搜索',
  web: '网络源搜索',
};

/* 版权平台：只能搜到精确元数据，下载时由后端自动换源 */
const LICENSE_SRC = { qq: 1, kugou: 1 };

const state = {
  src: 'auto',
  autohide: localStorage.getItem('md_autohide') === '1',
  history: [],
  jobs: [],
  files: [],
  jobNodes: new Map(),
  lastDone: -1,
  playing: null,
  playlist: [],
  playIndex: -1,
  searchSeq: 0,
  selected: new Set(),
};

/* 搜索源显示名：auto 时说明合并了哪些源 */
function srcLabel() {
  if (state.src === 'auto') return '综合搜索';
  return SRC_LABEL[state.src] || '搜索';
}

/* ------------------------------------------------ 基础工具 */
function toast(msg, kind = '') {
  el.toast.textContent = msg;
  el.toast.className = 'toast show ' + kind;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.toast.className = 'toast ' + kind; }, 3400);
}

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}`);
  return data;
}

function fmtDur(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  const m = Math.floor(sec / 60), s = sec % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

function esc(s) { return String(s == null ? '' : s); }

function autoGrow() {
  el.input.style.height = 'auto';
  el.input.style.height = Math.min(220, el.input.scrollHeight) + 'px';
}

/* ------------------------------------------------ 提交 */
/* ------------------------------------------------ 链接识别 */
// 从任意文本里抽 URL：兼容抖音/快手/B站的分享文案（链接后面常跟着中文和 emoji）
const URL_RE = /https?:\/\/[^\s"'<>|\\^`{}【】（）\[\]《》，。！？；：、]+/gi;
function extractUrls(text) {
  const found = text.match(URL_RE) || [];
  const cleaned = found.map(u => u.replace(/[.,;:!?)\]}>'"]+$/, ''));
  return [...new Set(cleaned)];
}

const PLAT_RULES = [
  [/music\.163\.com|163cn\.tv/i, '网易云'],
  [/bilibili\.com|b23\.tv/i, 'B站'],
  [/ximalaya\.com|xmcdn\.com/i, '喜马拉雅'],
  [/douyin\.com|iesdouyin\.com/i, '抖音'],
  [/kuaishou\.com|kwaicdn\.com|chenzhongtech\.com/i, '快手'],
  [/youtube\.com|youtu\.be/i, 'YouTube'],
  [/y\.qq\.com|qq\.com\/.*song/i, 'QQ音乐'],
  [/kugou\.com/i, '酷狗'],
  [/\.(mp3|m4a|flac|wav|aac|ogg)(\?|$)/i, '音频直链'],
  [/\.(mp4|m4v|mov|webm|mkv)(\?|$)/i, '视频直链'],
];
function detectPlatform(url) {
  for (const [re, name] of PLAT_RULES) if (re.test(url)) return name;
  return '未知平台';
}

async function submit() {
  const raw = el.input.value.trim();
  if (!raw) { el.input.focus(); return; }

  // 先抽链接：粘贴分享文案（"7.68 复制打开抖音… https://v.douyin.com/xxx/"）也能用
  const urls = extractUrls(raw);

  el.btnGo.disabled = true;
  el.btnGo.querySelector('span').textContent = '处理中';

  try {
    if (urls.length) {
      const plats = urls.map(detectPlatform);
      await createJobs(urls.map(u => ({ source: u })));
      const uniq = [...new Set(plats)];
      if (urls.length === 1) {
        toast(`识别到 ${plats[0]}，已开始下载`, 'ok');
      } else {
        toast(`识别到 ${urls.length} 个链接（${uniq.join(' / ')}），已全部加入队列`, 'ok');
      }
      el.input.value = ''; autoGrow();
      return;
    }
    // 没有链接 → 当搜索词
    await search(raw);
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    el.btnGo.disabled = false;
    el.btnGo.querySelector('span').textContent = '解析';
  }
}

async function probeAndShow(url) {
  el.resultsPanel.classList.remove('hidden');
  el.resultsTitle.textContent = '解析结果';
  el.resultsCount.textContent = '';
  el.results.innerHTML = '<div class="empty">正在探测...</div>';
  let p;
  try {
    p = (await api('/api/probe?url=' + encodeURIComponent(url))).probe;
  } catch (e) {
    el.results.innerHTML = `<div class="empty">探测失败：${esc(e.message)}</div>`;
    return;
  }
  const item = {
    engine: p.platform === 'netease' ? 'netease' : (p.platform === 'direct' ? 'direct' : 'ytdlp'),
    id: p.song_id || '',
    title: p.title || url,
    artist: p.artist || '',
    album: '',
    thumb: p.thumb || '',
    duration: p.duration || 0,
    source: url,
    note: p.ok ? (p.message || '可下载') : (p.message || '可能不可用'),
    bad: !p.ok,
  };
  renderResults([item], 'probe');
  el.resultsPanel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function search(q) {
  el.resultsPanel.classList.remove('hidden');
  el.resultsTitle.textContent = srcLabel();
  el.results.innerHTML = '<div class="empty">搜索中...</div>';
  el.resultsCount.textContent = '';
  const seq = ++state.searchSeq;
  try {
    const data = await api(`/api/search?q=${encodeURIComponent(q)}&src=${state.src}&limit=20`);
    if (seq !== state.searchSeq) return;
    const items = data.items || [];
    if (!items.length) {
      el.results.innerHTML = data.notice
          ? `<div class="empty notice">${esc(data.notice)}</div>`
          : '<div class="empty">没搜到，换个关键词或切换源试试</div>';
      el.resultsCount.textContent = '0';
      return;
    }
    renderResults(items, 'search');
  } catch (e) {
    if (seq !== state.searchSeq) return;
    el.results.innerHTML = `<div class="empty">搜索失败：${esc(e.message)}</div>`;
  }
}

function renderResults(items, mode) {
  el.resultsTitle.textContent = mode === 'probe' ? '解析结果' : srcLabel();
  el.resultsCount.textContent = `${items.length} 条`;
  el.results.innerHTML = '';
  const frag = document.createDocumentFragment();
  const RESULT_TIPS = {
    auto: '同时搜了 B站 / 网易云 / 喜马拉雅，按相关度混排。只想搜一个平台，用上方的下拉切换。',
    netease: '网易云对版权曲目只返回翻唱版本，找原唱请切到「B站」源。',
    qq: 'QQ音乐只提供精确的歌曲信息（歌名 / 歌手 / 专辑 / 时长）。点下载会自动去 B站 匹配同一首歌的高音质版本。',
    kugou: '酷狗同样只提供歌曲信息（含 320k / 无损体积）。点下载会自动换源到 B站 / 网易云 取同曲。',
    ximalaya: '喜马拉雅是有声书 / 播客 / 电台源：免费音频可直接下载，VIP 付费内容取不到音频。',
  };
  if (mode === 'search' && RESULT_TIPS[state.src]) {
    const tip = document.createElement('div');
    tip.className = 'res-tip';
    tip.textContent = RESULT_TIPS[state.src];
    frag.appendChild(tip);
  }
  items.forEach((it) => {
    const row = document.createElement('div');
    row.className = 'result';

    const thumb = document.createElement('div');
    thumb.className = 'res-thumb';
    if (it.thumb) {
      const img = document.createElement('img');
      img.loading = 'lazy';
      img.src = thumbUrl(it.thumb);
      img.onerror = () => { img.remove(); };
      thumb.appendChild(img);
    } else {
      thumb.textContent = '♪';
    }

    const main = document.createElement('div');
    main.className = 'res-main';
    const t = document.createElement('div');
    t.className = 'res-title';
    t.textContent = it.title || '未命名';
    const sub = document.createElement('div');
    sub.className = 'res-sub';
    const bits = [it.artist, it.note].filter(Boolean);
    sub.textContent = bits.join(' · ');
    if (it.bad) sub.style.color = '#f8a4a4';
    else if (it.vip) sub.style.color = '#fbbf24';
    main.append(t, sub);

    const dur = document.createElement('div');
    dur.className = 'res-dur';
    dur.textContent = it.duration ? fmtDur(it.duration) : '';

    const act = document.createElement('button');
    act.className = 'res-act';
    act.textContent = mode === 'probe' ? '下载 MP3' : '下载';
    act.onclick = async () => {
      act.disabled = true;
      act.textContent = '已加入';
      try {
        await createJobs([{
          source: it.source,
          engine: it.engine,
          platform: it.platform || (state.src === 'auto' ? '' : state.src),
          platform_id: String(it.id || ''),
          song_id: String(it.id || ''),
          title: it.title,
          artist: it.artist,
          album: it.album,
          thumb: it.thumb,
          duration: it.duration,
        }]);
        toast('已加入下载队列', 'ok');
        el.tasksPanel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      } catch (e) {
        act.disabled = false;
        act.textContent = '下载';
        toast(e.message, 'err');
      }
    };

    row.append(thumb, main, dur, act);
    frag.appendChild(row);
  });
  el.results.appendChild(frag);
  el.resultsPanel.classList.remove('hidden');
}

async function createJobs(items) {
  const data = await api('/api/jobs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ items }),
  });
  await tick();
  return data;
}

/* ------------------------------------------------ 任务渲染 */
function thumbUrl(u) {
  if (!u) return '';
  return '/api/thumb?u=' + encodeURIComponent(u);
}

function jobTitle(j) {
  if (j.status === 'done' && j.filename) return j.filename.replace(/\.mp3$/i, '');
  if (j.title) return j.title;
  if (j.source) {
    try { return decodeURIComponent(new URL(j.source).pathname.split('/').pop()) || j.source; }
    catch (_) { return j.source; }
  }
  return j.id;
}

function renderJobs() {
  const list = state.jobs;
  el.taskBadge.textContent = list.filter(j => j.status === 'running' || j.status === 'queued').length;
  const active = list.filter(j => j.status === 'running' || j.status === 'queued');
  const recent = list.filter(j => j.status !== 'running' && j.status !== 'queued'
    && !(state.autohide && j.status === 'done')).slice(0, 30);
  const shown = [...active, ...recent];

  if (!shown.length) {
    el.tasks.innerHTML = '<div class="empty">还没有任务，上面粘个链接试试</div>';
    state.jobNodes.clear();
    return;
  }
  if (el.tasks.querySelector('.empty')) el.tasks.innerHTML = '';

  const seen = new Set();
  shown.forEach((j, idx) => {
    seen.add(j.id);
    let node = state.jobNodes.get(j.id);
    if (!node) {
      node = buildTaskNode(j);
      state.jobNodes.set(j.id, node);
    }
    updateTaskNode(node, j);
    if (el.tasks.children[idx] !== node) {
      el.tasks.insertBefore(node, el.tasks.children[idx] || null);
    }
  });
  [...state.jobNodes.entries()].forEach(([id, node]) => {
    if (!seen.has(id)) { node.remove(); state.jobNodes.delete(id); }
  });
}

const STATUS_TEXT = { queued: '排队中', running: '进行中', done: '已完成', error: '失败' };

const PLAT_NAME = {
  bilibili: 'B站', netease: '网易云', qq: 'QQ音乐', kugou: '酷狗',
  ximalaya: '喜马拉雅', douyin: '抖音', kuaishou: '快手',
  youtube: 'YouTube', direct: '直链', generic: '链接', kuwo: '酷我',
};

function buildTaskNode(j) {
  const node = document.createElement('div');
  node.className = 'task';
  node.innerHTML = `
    <div class="task-left">
      <div class="task-title"></div>
      <div class="task-sub">
        <span class="plat"></span>
        <span class="tag"></span>
        <span class="stage"></span>
        <span class="dot"></span>
        <span class="speed"></span>
      </div>
      <div class="progress"><i style="width:0%"></i></div>
    </div>
    <div class="task-right">
      <button class="icon-btn act-play" title="播放" hidden>
        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
      </button>
      <button class="icon-btn act-remove" title="移除记录">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 6l12 12M18 6L6 18"/></svg>
      </button>
    </div>`;
  node.querySelector('.act-remove').onclick = async () => {
    try {
      await api('/api/jobs/' + j.id, { method: 'DELETE' });
      state.jobNodes.get(j.id)?.remove();
      state.jobNodes.delete(j.id);
      await tick();
    } catch (e) { toast(e.message, 'err'); }
  };
  node._playBtn = node.querySelector('.act-play');
  return node;
}

function updateTaskNode(node, j) {
  node.className = 'task ' + j.status;
  const titleEl = node.querySelector('.task-title');
  const shownTitle = jobTitle(j);
  titleEl.textContent = shownTitle;
  titleEl.title = (j.status === 'done' && j.filename && j.title && j.title !== shownTitle) ? j.title : '';
  const plat = node.querySelector('.plat');
  const platKey = (j.platform || '').toLowerCase();
  const platName = PLAT_NAME[platKey] || '';
  plat.textContent = platName;
  plat.className = 'plat' + (platName ? '' : ' hidden');
  const tag = node.querySelector('.tag');
  tag.className = 'tag ' + j.status;
  tag.textContent = STATUS_TEXT[j.status] || j.status;
  node.querySelector('.stage').textContent = j.stage || '';
  const speed = node.querySelector('.speed');
  speed.textContent = j.speed ? String(j.speed) : (j.status === 'done' && j.out_size ? human(j.out_size) : '');
  const bar = node.querySelector('.progress > i');
  bar.style.width = (j.status === 'done' ? 100 : (j.progress || 0)) + '%';

  let errBox = node.querySelector('.task-err');
  if (j.status === 'error' && j.error) {
    if (!errBox) {
      errBox = document.createElement('div');
      errBox.className = 'task-err';
      node.querySelector('.task-left').appendChild(errBox);
    }
    errBox.textContent = j.error;
  } else if (errBox) {
    errBox.remove();
  }

  const pb = node._playBtn;
  if (j.status === 'done' && j.filename) {
    pb.hidden = false;
    pb.onclick = () => playFile(j.filename, shownTitle, j.artist || '');
  } else {
    pb.hidden = true;
  }
}

function human(n) {
  n = Number(n) || 0;
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + u[i];
}

/* ------------------------------------------------ 音乐库 */
async function loadFiles() {
  try {
    const data = await api('/api/files');
    state.files = data.files || [];
    renderLibrary();
  } catch (_) {}
}

function renderLibrary() {
  const kw = (el.libSearch.value || '').trim().toLowerCase();
  const files = state.files.filter(f =>
    !kw || (f.name + ' ' + (f.title || '') + ' ' + (f.artist || '')).toLowerCase().includes(kw));
  el.libBadge.textContent = state.files.length;
  const total = state.files.reduce((a, f) => a + (f.size || 0), 0);
  el.libSize.textContent = state.files.length ? human(total) : '';

  if (!files.length) {
    el.library.innerHTML = state.files.length
      ? '<div class="empty">没有匹配的文件</div>'
      : '<div class="empty">下载完成的文件会出现在这里</div>';
    syncSelBar([]);
    return;
  }
  el.library.innerHTML = '';
  const frag = document.createDocumentFragment();
  files.forEach((f, i) => {
    const checked = state.selected.has(f.name);
    const row = document.createElement('div');
    row.className = 'track' + (state.playing === f.name ? ' playing' : '') + (checked ? ' selected' : '');
    row.dataset.name = f.name;

    const box = document.createElement('input');
    box.type = 'checkbox';
    box.className = 'track-check';
    box.checked = checked;
    box.title = '选中这一项';
    box.addEventListener('click', (ev) => ev.stopPropagation());
    box.addEventListener('change', () => toggleSel(f.name, box.checked));

    const play = document.createElement('button');
    play.className = 'play-btn';
    play.innerHTML = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>';
    play.onclick = () => playFile(f.name, f.title || f.name, f.artist || '');

    const main = document.createElement('div');
    main.className = 'track-main';
    const tt = document.createElement('div');
    tt.className = 'track-title';
    tt.textContent = f.title || f.name;
    const sub = document.createElement('div');
    sub.className = 'track-sub';
    sub.textContent = [f.artist, f.name].filter(Boolean).join(' · ');
    main.append(tt, sub);

    const meta = document.createElement('div');
    meta.className = 'track-meta';
    const isVid = f.kind === 'video' || /\.(mp4|m4v|mov|webm|mkv)$/i.test(f.name);
    meta.textContent = [
      isVid ? '视频' : '',
      f.duration ? fmtDur(f.duration) : '',
      isVid && f.width ? f.width + '×' + f.height : '',
      !isVid && f.bitrate ? f.bitrate + 'k' : '',
      f.size_h,
    ].filter(Boolean).join(' · ');

    const acts = document.createElement('div');
    acts.className = 'track-actions';

    const dl = document.createElement('a');
    dl.className = 'icon-btn';
    dl.href = '/media/' + encodeURIComponent(f.name) + '?dl=1';
    dl.title = '下载到本机';
    dl.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 3v12M7 11l5 5 5-5M5 21h14"/></svg>';
    dl.setAttribute('download', f.name);

    const rn = document.createElement('button');
    rn.className = 'icon-btn';
    rn.title = '重命名';
    rn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 20h9M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z"/></svg>';
    rn.onclick = async () => {
      const name = prompt('新的文件名（不含扩展名）', f.name.replace(/\.[a-z0-9]+$/i, ''));
      if (!name) return;
      try {
        await api('/api/files/rename', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: f.name, new: name }),
        });
        toast('已重命名', 'ok');
        loadFiles();
      } catch (e) { toast(e.message, 'err'); }
    };

    const del = document.createElement('button');
    del.className = 'icon-btn';
    del.title = '删除';
    del.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/></svg>';
    del.onclick = async () => {
      if (!confirm(`删除「${f.name}」？`)) return;
      try {
        await api('/api/files?name=' + encodeURIComponent(f.name), { method: 'DELETE' });
        if (state.playing === f.name) stopPlay();
        toast('已删除');
        loadFiles();
      } catch (e) { toast(e.message, 'err'); }
    };

    acts.append(dl, rn, del);
    row.append(box, play, main, meta, acts);
    frag.appendChild(row);
  });
  el.library.appendChild(frag);
  syncSelBar(files);
}

/* ------------------------------------------------ 曲库多选 */
function visibleFiles() {
  const kw = (el.libSearch.value || '').trim().toLowerCase();
  return state.files.filter(f =>
    !kw || (f.name + ' ' + (f.title || '') + ' ' + (f.artist || '')).toLowerCase().includes(kw));
}

function syncSelBar(files) {
  const names = (files || visibleFiles()).map(f => f.name);
  const sel = names.filter(n => state.selected.has(n)).length;
  el.libSelAll.checked = names.length > 0 && sel === names.length;
  el.libSelAll.indeterminate = sel > 0 && sel < names.length;
  el.libSelCount.textContent = sel ? `已选 ${sel} 项` : '未选择';
  el.libBulk.classList.toggle('hidden', sel === 0);
}

function toggleSel(name, on) {
  if (on) state.selected.add(name);
  else state.selected.delete(name);
  const row = el.library.querySelector(`.track[data-name="${CSS.escape(name)}"]`);
  if (row) row.classList.toggle('selected', on);
  syncSelBar();
}

function clearSel() {
  state.selected.clear();
  renderLibrary();
}

function selectAllVisible() {
  const vis = visibleFiles().map(f => f.name);
  if (el.libSelAll.checked) vis.forEach(n => state.selected.add(n));
  else vis.forEach(n => state.selected.delete(n));
  renderLibrary();
}

async function bulkDelete() {
  const names = [...state.selected].filter(n => state.files.some(f => f.name === n));
  if (!names.length) return;
  if (!confirm(`确定删除选中的 ${names.length} 个文件？删掉就找不回来了。`)) return;
  let ok = 0, fail = 0;
  for (const name of names) {
    el.libSelCount.textContent = `删除中 ${ok + fail + 1}/${names.length}`;
    try {
      await api('/api/files?name=' + encodeURIComponent(name), { method: 'DELETE' });
      ok++;
      if (state.playing === name) stopPlay();
      state.selected.delete(name);
    } catch (e) {
      fail++;
    }
  }
  toast(fail ? `已删除 ${ok} 个，${fail} 个失败` : `已删除 ${ok} 个`, fail ? 'err' : 'ok');
  loadFiles();
}

function bulkDownload() {
  const names = [...state.selected].filter(n => state.files.some(f => f.name === n));
  if (!names.length) return;
  if (names.length > 5 && !confirm(`要逐个下载 ${names.length} 个文件，浏览器会问「是否允许下载多个文件」，选允许即可。继续？`)) return;
  names.forEach((n, i) => {
    setTimeout(() => {
      const a = document.createElement('a');
      a.href = '/media/' + encodeURIComponent(n) + '?dl=1';
      a.download = n;
      document.body.appendChild(a);
      a.click();
      a.remove();
    }, i * 700);
  });
  toast(`开始下载 ${names.length} 个文件`);
}

function bindLibrarySelection() {
  el.libSelAll.addEventListener('change', selectAllVisible);
  el.btnBulkDel.addEventListener('click', bulkDelete);
  el.btnBulkDl.addEventListener('click', bulkDownload);
  el.btnBulkClear.addEventListener('click', clearSel);
}

/* ------------------------------------------------ 播放 */
function playFile(name, title, artist) {
  state.playing = name;
  state.playlist = state.files.map(f => f.name);
  state.playIndex = state.playlist.indexOf(name);
  const item = state.files.find(f => f.name === name);
  const isVideo = /\.(mp4|m4v|mov|webm|mkv)$/i.test(name) || (item && item.kind === 'video');
  const src = '/media/' + encodeURIComponent(name);
  if (isVideo) {
    el.audio.pause();
    el.audio.removeAttribute('src');
    el.audio.classList.add('hidden');
    el.videoView.classList.remove('hidden');
    el.videoView.src = src;
    el.videoView.play().catch(() => {});
  } else {
    el.videoView.pause();
    el.videoView.removeAttribute('src');
    el.videoView.classList.add('hidden');
    el.audio.classList.remove('hidden');
    el.audio.src = src;
    el.audio.play().catch(() => {});
  }
  el.playerTitle.textContent = title;
  el.playerSub.textContent = artist || name;
  el.player.classList.remove('hidden');
  renderLibrary();
}

function stopPlay() {
  el.audio.pause();
  el.audio.removeAttribute('src');
  el.audio.load();
  el.videoView.pause();
  el.videoView.removeAttribute('src');
  el.videoView.load();
  el.videoView.classList.add('hidden');
  el.audio.classList.remove('hidden');
  el.player.classList.add('hidden');
  state.playing = null;
  renderLibrary();
}

function onPlayEnded() {
  if (state.playIndex >= 0 && state.playIndex < state.playlist.length - 1) {
    const next = state.playlist[state.playIndex + 1];
    const f = state.files.find(x => x.name === next);
    playFile(next, f ? (f.title || f.name) : next, f ? f.artist : '');
  } else {
    stopPlay();
  }
}

el.audio.addEventListener('ended', onPlayEnded);
el.videoView.addEventListener('ended', onPlayEnded);

/* ------------------------------------------------ 轮询 */
async function tick() {
  try {
    const data = await api('/api/jobs');
    state.jobs = data.jobs || [];
    el.status.className = 'status ok';
    el.status.querySelector('span').textContent = '已连接';
  } catch (e) {
    el.status.className = 'status bad';
    el.status.querySelector('span').textContent = '离线';
    return;
  }
  renderJobs();
  const doneCount = state.jobs.filter(j => j.status === 'done').length;
  if (doneCount !== state.lastDone) {
    state.lastDone = doneCount;
    loadFiles();
  }
}

/* ------------------------------------------------ 事件绑定 */
el.btnGo.onclick = submit;

el.input.addEventListener('input', autoGrow);
el.input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); submit(); }
});

el.chips.addEventListener('click', (e) => {
  const b = e.target.closest('.chip');
  if (!b) return;
  el.input.value = b.dataset.q || '';
  autoGrow();
  submit();
});

el.selSrc.addEventListener('change', () => {
  state.src = el.selSrc.value || 'auto';
  const q = el.input.value.trim();
  if (q && !/^https?:\/\//i.test(q)) search(q);
});

$('#btnCloseResults').onclick = () => el.resultsPanel.classList.add('hidden');
$('#btnClosePlayer').onclick = stopPlay;
$('#btnFolder').onclick = async () => {
  try { await api('/api/open-folder', { method: 'POST' }); }
  catch (e) { toast(e.message, 'err'); }
};
el.libSearch.addEventListener('input', renderLibrary);
bindLibrarySelection();

$('#btnClearDone').onclick = async () => {
  const done = state.jobs.filter(j => j.status === 'done' || j.status === 'error');
  for (const j of done) {
    try { await api('/api/jobs/' + j.id, { method: 'DELETE' }); } catch (_) {}
  }
  await tick();
  toast('已清除 ' + done.length + ' 条记录');
};

/* 设置 */
$('#btnSettings').onclick = async () => {
  try {
    const { settings, ffmpeg } = await api('/api/settings');
    $('#setQuality').value = settings.quality || '320';
    $('#setConcurrency').value = settings.concurrency || 3;
    $('#setTags').checked = !!settings.write_tags;
    $('#setCover').checked = !!settings.embed_cover;
    $('#setProxy').value = settings.proxy || '';
    $('#setCookies').value = settings.browser_cookies || '';
    $('#setDir').value = settings.download_dir || '';
    $('#setMoveOld').checked = !!settings.move_on_dir_change;
    $('#setPurge').value = (settings.purge_days ?? 3);
    $('#setUpdateSrc').value = settings.update_source || '';
    $('#setAllowLan').checked = !!settings.allow_lan;
    refreshLanBox();
    $('#setFfmpeg').textContent = ffmpeg || '未找到';
    $('#updState').textContent = '';
    el.modal.classList.remove('hidden');
    checkUpdate(false);
  } catch (e) { toast(e.message, 'err'); }
};
/* 手机访问：把局域网地址和二维码填进去 */
async function refreshLanBox() {
  const box = document.getElementById('lanBox');
  const img = document.getElementById('lanQr');
  const urlEl = document.getElementById('lanUrl');
  const tipEl = document.getElementById('lanTip');
  if (!box) return;
  const want = document.getElementById('setAllowLan').checked;
  try {
    const net = await api('/api/net');
    if (!net.ok) { tipEl.textContent = '读不到网络信息'; return; }
    if (!net.lan_enabled) {
      urlEl.textContent = '未开启';
      img.removeAttribute('src');
      img.style.visibility = 'hidden';
      tipEl.textContent = want
        ? '保存后重启软件生效（关掉再打开「音乐下载台」）。'
        : '勾选后保存，重启软件就能用手机连。';
      return;
    }
    if (!net.ip) {
      urlEl.textContent = '没找到局域网地址';
      img.removeAttribute('src');
      img.style.visibility = 'hidden';
      tipEl.textContent = '这台电脑可能没连 Wi-Fi 或网线。';
      return;
    }
    const url = net.mobile_url || ('http://' + net.ip + ':' + net.port + '/m');
    urlEl.textContent = url;
    img.src = '/api/qr?t=' + encodeURIComponent(url);
    img.style.visibility = 'visible';
    tipEl.textContent = '手机和电脑连同一个 Wi-Fi，扫码直接打开。';
  } catch (e) {
    tipEl.textContent = '读不到网络信息：' + e.message;
  }
}
document.getElementById('setAllowLan').addEventListener('change', refreshLanBox);

$('#btnCloseSettings').onclick = () => el.modal.classList.add('hidden');
$('#btnCancelSettings').onclick = () => el.modal.classList.add('hidden');
el.modal.addEventListener('click', (e) => { if (e.target === el.modal) el.modal.classList.add('hidden'); });

$('#btnSaveSettings').onclick = async () => {
  const newDir = $('#setDir').value.trim();
  const moveOld = $('#setMoveOld').checked;
  const payload = {
    download_dir: newDir,
    quality: $('#setQuality').value,
    concurrency: Number($('#setConcurrency').value) || 3,
    write_tags: $('#setTags').checked,
    embed_cover: $('#setCover').checked,
    proxy: $('#setProxy').value.trim(),
    browser_cookies: $('#setCookies').value,
    purge_days: Math.max(0, Math.min(365, Number($('#setPurge').value) || 0)),
    update_source: $('#setUpdateSrc').value.trim(),
    allow_lan: $('#setAllowLan').checked,
    move_on_dir_change: $('#setMoveOld').checked,
  };
  try {
    // 先搬文件、再改目录：搬移读的是「当前」下载目录，顺序反了就搬不到东西
    if (moveOld && newDir) {   // moveOld 来自「换目录时搬旧文件」这个持久开关
      const cur = await api('/api/settings');
      if ((cur.settings.download_dir || '') !== newDir) {
        const mv = await api('/api/files/move', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ target: newDir }),
        });
        if (mv.ok) {
          toast(mv.moved ? `已搬移 ${mv.moved} 个文件` : '没有需要搬移的文件', 'ok');
          if (mv.failed && mv.failed.length) toast(`${mv.failed.length} 个文件搬移失败`, 'err');
        } else if (mv.error) {
          toast('搬移失败：' + mv.error, 'err');
        }
      }
    }
    await api('/api/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    el.modal.classList.add('hidden');
    toast('设置已保存' + (newDir ? `，下载到 ${newDir}` : ''), 'ok');
    await loadFiles();
  } catch (e) { toast(e.message, 'err'); }
};

/* 选择目录 */
$('#btnPickDir').onclick = async () => {
  const btn = $('#btnPickDir');
  const old = btn.textContent;
  btn.textContent = '…';
  btn.disabled = true;
  try {
    const r = await api('/api/pick-folder', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ initial: $('#setDir').value.trim() }),
    });
    if (r.ok && r.path) $('#setDir').value = r.path;
  } catch (e) { toast('打开目录选择器失败：' + e.message, 'err'); }
  finally { btn.textContent = old; btn.disabled = false; }
};

/* 完成后自动隐藏 */
el.chkAutohide.checked = state.autohide;
el.chkAutohide.onchange = () => {
  state.autohide = el.chkAutohide.checked;
  localStorage.setItem('md_autohide', state.autohide ? '1' : '0');
  state.jobNodes.forEach((node, id) => {
    const j = state.jobs.find(x => x.id === id);
    if (state.autohide && j && j.status === 'done') node.remove();
  });
  if (state.autohide) state.jobNodes.clear();
  renderJobs();
  toast(state.autohide ? '完成的任务会自动隐藏' : '已完成的任务会保留在列表里', 'ok');
};

/* 下载记录 */
async function loadHistory() {
  try {
    const data = await api('/api/history?limit=200');
    state.history = data.items || [];
    renderHistory();
  } catch (_) {}
}

function fmtTs(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const p = (x) => String(x).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function renderHistory() {
  const list = state.history || [];
  el.historyBadge.textContent = list.length;
  el.historyHint.textContent = list.length ? `最近 ${list.length} 条` : '';
  if (!list.length) {
    el.history.innerHTML = '<div class="empty">下载完成或失败的任务会记在这里</div>';
    return;
  }
  const frag = document.createDocumentFragment();
  list.forEach(h => {
    try {
    const ok = h.status === 'done';
    const row = document.createElement('div');
    row.className = 'hist-row';
    const ico = document.createElement('div');
    ico.className = 'hist-ico' + (ok ? '' : ' bad');
    ico.innerHTML = ok
      ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" width="13" height="13"><path d="M4 12l5 5L20 6"/></svg>'
      : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" width="13" height="13"><path d="M6 6l12 12M18 6L6 18"/></svg>';
    const main = document.createElement('div');
    main.className = 'hist-main';
    const t1 = document.createElement('div');
    t1.className = 'hist-title';
    t1.textContent = h.filename || h.title || '(未知)';
    t1.title = h.title || '';
    const t2 = document.createElement('div');
    t2.className = 'hist-sub';
    t2.textContent = [h.platform || '', h.artist || ''].filter(Boolean).join(' · ') || '—';
    main.append(t1, t2);
    const meta = document.createElement('div');
    meta.className = 'hist-meta';
    meta.textContent = [
      h.duration ? fmtDur(h.duration) : '',
      h.size ? human(h.size) : '',
    ].filter(Boolean).join(' · ') || (ok ? '' : '失败');
    const time = document.createElement('div');
    time.className = 'hist-time';
    time.textContent = fmtTs(h.finished);
    row.append(ico, main, meta, time);
    frag.append(row);
    } catch (err) { console.warn('history row render failed', h, err); }
  });
  el.history.innerHTML = '';
  el.history.append(frag);
}

el.btnHistRefresh.onclick = async () => { await loadHistory(); toast('记录已刷新', 'ok'); };
el.btnHistClear.onclick = async () => {
  if (!confirm('清空全部下载记录？文件不会被删除。')) return;
  try {
    await api('/api/history', { method: 'DELETE' });
    state.history = [];
    renderHistory();
    toast('记录已清空', 'ok');
  } catch (e) { toast(e.message, 'err'); }
};
el.btnLibRefresh.onclick = async () => { await loadFiles(); toast('曲库已刷新', 'ok'); };
el.btnTaskRefresh.onclick = async () => { await tick(); toast('任务已刷新', 'ok'); };

/* 拖拽上传 */
let dragDepth = 0;
['dragenter', 'dragover'].forEach(ev => {
  document.addEventListener(ev, (e) => {
    if (!e.dataTransfer || ![...e.dataTransfer.types].includes('Files')) return;
    e.preventDefault();
    if (ev === 'dragenter') dragDepth++;
    el.dropZone.classList.add('dragging');
  });
});
['dragleave', 'drop'].forEach(ev => {
  document.addEventListener(ev, (e) => {
    if (!e.dataTransfer || ![...e.dataTransfer.types].includes('Files')) return;
    e.preventDefault();
    if (ev === 'dragleave') dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0 || ev === 'drop') {
      el.dropZone.classList.remove('dragging');
      dragDepth = 0;
    }
  });
});
document.addEventListener('drop', async (e) => {
  if (!e.dataTransfer || !e.dataTransfer.files || !e.dataTransfer.files.length) return;
  e.preventDefault();
  el.dropZone.classList.remove('dragging');
  const files = [...e.dataTransfer.files];
  let ok = 0;
  for (const f of files) {
    try {
      const res = await fetch('/api/upload?name=' + encodeURIComponent(f.name), {
        method: 'POST', body: f,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || '上传失败');
      ok++;
    } catch (err) {
      toast(`${f.name}: ${err.message}`, 'err');
    }
  }
  if (ok) {
    toast(`已提交 ${ok} 个文件转 mp3`, 'ok');
    await tick();
  }
});

/* 粘贴文字直接触发搜索（多行链接不触发） */
el.input.addEventListener('paste', () => {
  setTimeout(async () => {
    autoGrow();
    const v = el.input.value.trim();
    // 粘贴内容里只要含链接就自动开跑（分享文案也算）
    if (v && !/[\r\n]/.test(v) && /https?:\/\//i.test(v)) {
      submit();
    }
  }, 60);
});

/* ------------------------------------------------ 版本更新 */
let updInfo = null;
let updStaged = false;

function hideUpdateBar() { el.updateBar.classList.add('hidden'); }

function showUpdateBar(info) {
  el.ubVer.textContent = 'v' + info.latest;
  const notes = String(info.notes || '').split('\n').map(s => s.trim()).filter(Boolean).slice(0, 3).join(' · ');
  el.ubNotes.textContent = notes || `当前 v${info.current} → v${info.latest}`;
  el.btnUpdateNow.textContent = '立即更新';
  el.btnUpdateNow.disabled = false;
  el.updateBar.classList.remove('hidden');
  updStaged = false;
}

async function checkUpdate(manual) {
  try {
    const r = await api('/api/update');
    updInfo = r;
    if (el.setVersion) el.setVersion.textContent = 'v' + (r.version || r.current || '—');
    if (el.setUpdateSrc && document.activeElement !== el.setUpdateSrc) {
      // 只在用户没在编辑时回填，避免把正在输入的内容冲掉
      if (!el.setUpdateSrc.value) el.setUpdateSrc.placeholder = r.source ? '自动：' + r.source : '留空 = 自动找 OneDrive 里的 release 目录';
    }
    if (r.has_update) {
      showUpdateBar(r);
      if (manual && el.updState) el.updState.textContent = `发现 v${r.latest}`;
    } else {
      hideUpdateBar();
      if (manual && el.updState) {
        el.updState.textContent = r.error ? ('检查失败：' + r.error) : `已是最新版 v${r.current}`;
      }
    }
    return r;
  } catch (e) {
    if (manual && el.updState) el.updState.textContent = '检查失败：' + e.message;
    return null;
  }
}

async function stageUpdate() {
  el.btnUpdateNow.disabled = true;
  el.btnUpdateNow.textContent = '下载中…';
  const sz = updInfo && updInfo.size ? human(updInfo.size) : '';
  el.ubNotes.textContent = '正在下载更新包' + (sz ? `（${sz}）` : '') + '，请稍等…';
  try {
    const r = await api('/api/update/apply', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
    if (!r.ok) throw new Error(r.error || '下载失败');
    updStaged = true;
    el.btnUpdateNow.disabled = false;
    el.btnUpdateNow.textContent = '重启并安装';
    el.ubNotes.textContent = `更新包已就绪（v${r.version || ''}），点右边按钮会关闭软件、自动替换文件后重新打开`;
    toast('更新包已下载完成', 'ok');
  } catch (e) {
    el.btnUpdateNow.disabled = false;
    el.btnUpdateNow.textContent = '立即更新';
    el.ubNotes.textContent = '更新失败：' + e.message;
    toast('更新失败：' + e.message, 'err');
  }
}

async function installUpdate() {
  el.btnUpdateNow.disabled = true;
  el.btnUpdateNow.textContent = '正在重启…';
  try {
    const r = await api('/api/update/install', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
    if (!r.ok) throw new Error(r.error || '安装失败');
    el.ubNotes.textContent = '软件即将关闭并更新，大约 10 秒后会自动重新打开…';
    toast('正在更新，稍后自动重启', 'ok');
  } catch (e) {
    el.btnUpdateNow.disabled = false;
    el.btnUpdateNow.textContent = '重启并安装';
    toast('安装失败：' + e.message, 'err');
  }
}

function initUpdate() {
  if (el.btnUpdateNow) {
    el.btnUpdateNow.onclick = () => (updStaged ? installUpdate() : stageUpdate());
  }
  if (el.btnUpdateLater) {
    el.btnUpdateLater.onclick = () => { hideUpdateBar(); };
  }
  if (el.btnCheckUpdate) {
    el.btnCheckUpdate.onclick = async () => {
      el.btnCheckUpdate.disabled = true;
      const old = el.btnCheckUpdate.textContent;
      el.btnCheckUpdate.textContent = '…';
      if (el.updState) el.updState.textContent = '检查中…';
      await checkUpdate(true);
      el.btnCheckUpdate.disabled = false;
      el.btnCheckUpdate.textContent = old;
    };
  }
  setTimeout(() => checkUpdate(false), 1500);
  setInterval(() => checkUpdate(false), 6 * 3600 * 1000);
}

/* 启动 */
autoGrow();
tick();
loadFiles();
loadHistory();
setInterval(tick, 900);
initUpdate();

/* 支持 ?q=xxx&src=bilibili 直接搜索，方便分享与调试 */
(() => {
  const qs = new URLSearchParams(location.search);
  const q = (qs.get('q') || '').trim();
  const src = qs.get('src');
  if (src && SRC_LABEL[src]) {
    state.src = src;
    el.selSrc.value = src;
  }
  if (q) {
    el.input.value = q;
    autoGrow();
    submit();
  }
  // ?settings=1 直接弹设置面板，方便截图 / 快速改配置
  if (qs.get('settings') === '1') setTimeout(() => $('#btnSettings').click(), 700);
})();
})();