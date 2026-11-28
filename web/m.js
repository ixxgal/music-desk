/* 音乐下载台 · 手机版前端
   只做四件事：贴链接下载 / 看任务 / 听曲库 / 翻记录 */
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const el = {
  hdSub: $('#hdSub'),
  hdR: $('.hd-r'),
  btnSettings: $('#btnSettings'),
  btnRefresh: $('#btnRefresh'),
  q: $('#q'),
  btnSrc: $('#btnSrc'),
  srcNow: $('#srcNow'),
  btnGo: $('#btnGo'),
  btnPaste: $('#btnPaste'),
  hint: $('#hint'),
  results: $('#results'),
  tasks: $('#tasks'),
  taskCount: $('#taskCount'),
  taskDot: $('#taskDot'),
  library: $('#library'),
  libCount: $('#libCount'),
  libFilter: $('#libFilter'),
  history: $('#history'),
  histCount: $('#histCount'),
  btnHistClear: $('#btnHistClear'),
  player: $('#player'),
  plTitle: $('#plTitle'),
  plSub: $('#plSub'),
  audio: $('#audio'),
  video: $('#video'),
  btnStop: $('#btnStop'),
  tabbar: $('#tabbar'),
  toast: $('#toast'),
  sheet: $('#sheet'),
  sheetMask: $('#sheetMask'),
  sheetTitle: $('#sheetTitle'),
  sheetBody: $('#sheetBody'),
};

const SRC_ITEMS = [
  ['auto', '自动 · 全部源', '同时搜 B站 + 网易云，按相关度混排'],
  ['bilibili', 'B站', '原曲最全，Hi-Res 多'],
  ['netease', '网易云', 'VIP 歌也能下：会自动换源到 B 站'],
];

const SRC_NAME = Object.fromEntries(SRC_ITEMS.map(([v, n]) => [v, n]));

const state = {
  tab: 'dl',
  src: 'auto',
  jobs: [],
  files: [],
  hist: [],
  playing: '',
  playingName: '',
  mode: 'idle',   // idle | searching | results
  appVersion: '',
  downloadDir: '',
  latestVersion: '',
  hasUpdate: false,
  apkUrl: '',
  apkSize: 0,
};

/* ---------------- 基础工具 ---------------- */
async function api(path, opt = {}) {
  const res = await fetch(path, {
    headers: opt.body ? { 'Content-Type': 'application/json' } : undefined,
    ...opt,
  });
  const txt = await res.text();
  let data;
  try { data = JSON.parse(txt); } catch { data = { ok: false, error: txt.slice(0, 160) }; }
  if (!res.ok && data && !data.error) data.error = 'HTTP ' + res.status;
  return data;
}

let toastTimer = 0;
function toast(msg, kind = '') {
  el.toast.textContent = msg;
  el.toast.className = 'toast on' + (kind ? ' ' + kind : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.toast.className = 'toast' + (kind ? ' ' + kind : ''); }, 2600);
}

const esc = s => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

function fmtDur(sec) {
  const s = Math.round(Number(sec) || 0);
  if (!s) return '';
  const m = Math.floor(s / 60);
  const r = s % 60;
  return m + ':' + String(r).padStart(2, '0');
}

function human(n) {
  const b = Number(n) || 0;
  if (!b) return '';
  if (b < 1024) return b + ' B';
  if (b < 1024 * 1024) return (b / 1024).toFixed(0) + ' KB';
  if (b < 1024 * 1024 * 1024) return (b / 1048576).toFixed(1) + ' MB';
  return (b / 1073741824).toFixed(2) + ' GB';
}

function ago(ts) {
  const t = Number(ts) || 0;
  if (!t) return '';
  const d = Math.max(0, Date.now() / 1000 - t);
  if (d < 60) return '刚刚';
  if (d < 3600) return Math.floor(d / 60) + ' 分钟前';
  if (d < 86400) return Math.floor(d / 3600) + ' 小时前';
  if (d < 86400 * 30) return Math.floor(d / 86400) + ' 天前';
  const dt = new Date(t * 1000);
  return (dt.getMonth() + 1) + '月' + dt.getDate() + '日';
}

function thumbUrl(u) {
  if (!u) return '';
  return '/api/thumb?u=' + encodeURIComponent(u);
}

/* 从一大段分享文案里把链接抠出来 */
const URL_RE = /https?:\/\/[^\s"'<>|\\^`{}【】（）\[\]《》，。！？；：、]+/gi;
function extractUrls(text) {
  const hit = String(text || '').match(URL_RE) || [];
  const clean = hit.map(u => u.replace(/[.,;:!?)\]}'"'、。，！？；：）】》]+$/, ''));
  return Array.from(new Set(clean));
}

const PLAT_NAME = {
  bilibili: 'B站', netease: '网易云', qq: 'QQ音乐', kugou: '酷狗',
  ximalaya: '喜马拉雅', douyin: '抖音', kuaishou: '快手',
  youtube: 'YouTube', direct: '直链', web: '网络',
};

/* ---------------- Tab ---------------- */
function switchTab(name) {
  state.tab = name;
  $$('.panel').forEach(p => p.classList.toggle('hidden', p.dataset.panel !== name));
  $$('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  window.scrollTo({ top: 0, behavior: 'smooth' });
  if (name === 'task') { el.taskDot.classList.add('hidden'); loadJobs(); }
  if (name === 'lib') loadFiles();
  if (name === 'hist') loadHist();
}

el.tabbar.addEventListener('click', e => {
  const btn = e.target.closest('.tab');
  if (btn) switchTab(btn.dataset.tab);
});

/* ---------------- 提交 ---------------- */
async function submit() {
  const raw = el.q.value.trim();
  if (!raw) { toast('先贴个链接或写个歌名', 'bad'); el.q.focus(); return; }

  const urls = extractUrls(raw);
  if (urls.length) {
    el.q.value = '';
    toast('已加入队列，开始下载');
    await createJobs(urls.map(u => ({ source: u })));
    switchTab('task');
    return;
  }
  await search(raw);
}

async function createJobs(items) {
  const r = await api('/api/jobs', { method: 'POST', body: JSON.stringify({ items }) });
  if (!r.ok) { toast(r.error || '提交失败', 'bad'); return; }
  await loadJobs();
}

async function search(q) {
  el.btnGo.disabled = true;
  el.btnGo.textContent = '搜索中';
  el.results.innerHTML = '<div class="empty">正在搜…</div>';
  el.hint.className = 'hint';
  try {
    const r = await api('/api/search?q=' + encodeURIComponent(q) + '&src=' + encodeURIComponent(state.src) + '&limit=24');
    if (!r.ok) { el.results.innerHTML = '<div class="empty">' + esc(r.error || '搜索失败') + '</div>'; return; }
    if (r.notice) {
      el.results.innerHTML = '<div class="empty">' + esc(r.notice) + '</div>';
      return;
    }
    const items = r.items || [];
    if (!items.length) {
      el.results.innerHTML = '<div class="empty">没搜到，换个关键词或换个源试试</div>';
      return;
    }
    state.mode = 'results';
    renderResults(items, r.src || state.src);
  } catch (e) {
    el.results.innerHTML = '<div class="empty">' + esc(e.message || '网络出错') + '</div>';
  } finally {
    el.btnGo.disabled = false;
    el.btnGo.textContent = '开始';
  }
}

function renderResults(items, src) {
  const frag = document.createDocumentFragment();
  items.forEach((it, i) => {
    const row = document.createElement('div');
    row.className = 'res';
    const t = it.title || '(无标题)';
    const sub = [];
    if (it.artist) sub.push('<span>' + esc(it.artist) + '</span>');
    if (it.duration) sub.push('<span>' + fmtDur(it.duration) + '</span>');
    const plat = it.platform || (src === 'auto' ? '' : src);
    if (plat && PLAT_NAME[plat]) sub.push('<span class="plat">' + esc(PLAT_NAME[plat]) + '</span>');
    if (it.vip) sub.push('<span class="vip">VIP</span>');

    row.innerHTML =
      '<img class="thumb" loading="lazy" src="' + esc(thumbUrl(it.thumb) || '') + '" alt="">' +
      '<div class="res-main">' +
        '<div class="res-title">' + esc(t) + '</div>' +
        '<div class="res-sub">' + sub.join('') + '</div>' +
      '</div>' +
      '<button class="res-go" aria-label="下载">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round"><path d="M12 4v11M7.5 10.5L12 15l4.5-4.5"/><path d="M5 19h14"/></svg>' +
      '</button>';

    row.querySelector('.thumb').addEventListener('error', e => { e.target.style.visibility = 'hidden'; });

    row.querySelector('.res-go').addEventListener('click', async e => {
      const btn = e.currentTarget;
      if (btn.classList.contains('done')) return;
      btn.disabled = true;
      const payload = { source: it.source || it.url, title: it.title || '', artist: it.artist || '', platform: plat || '', engine: it.engine || '' };
      if (it.duration) payload.duration = it.duration;
      if (it.thumb) payload.thumb = it.thumb;
      const r = await api('/api/jobs', { method: 'POST', body: JSON.stringify({ items: [payload] }) });
      if (r.ok) {
        btn.classList.add('done');
        btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M5 12.5l4.5 4.5L19 6.5"/></svg>';
        toast('已加入下载队列');
        loadJobs();
      } else {
        btn.disabled = false;
        toast(r.error || '加入失败', 'bad');
      }
    });
    frag.append(row);
  });
  el.results.innerHTML = '';
  el.results.append(frag);
}

/* ---------------- 任务 ---------------- */
const jobNodes = new Map();

function jobName(j) {
  if (j.status === 'done' && j.filename) return j.filename.replace(/\.[^.]+$/, '');
  return j.title || j.filename || j.id;
}

function renderJobs() {
  const list = state.jobs || [];
  const live = list.filter(j => j.status === 'running' || j.status === 'queued');
  const done = list.filter(j => j.status !== 'running' && j.status !== 'queued').slice(0, 24);
  const show = live.concat(done);

  el.taskCount.textContent = String(list.length);

  if (!show.length) {
    el.tasks.innerHTML = '<div class="empty">还没有任务<br>贴个链接或搜首歌试试</div>';
    jobNodes.clear();
    return;
  }

  if (el.tasks.querySelector('.empty')) el.tasks.innerHTML = '';

  show.forEach(j => {
    let node = jobNodes.get(j.id);
    if (!node) {
      node = document.createElement('div');
      node.className = 'task';
      node.dataset.id = j.id;
      node.innerHTML =
        '<div class="task-top">' +
          '<div class="task-title"></div>' +
          '<span class="task-pct"></span>' +
        '</div>' +
        '<div class="bar"><i></i></div>' +
        '<div class="task-sub"></div>' +
        '<div class="task-acts"></div>';
      jobNodes.set(j.id, node);
      el.tasks.append(node);
    }
    node.dataset.st = j.status === 'done' ? 'done' : (j.status === 'error' ? 'error' : 'run');
    const pct = Math.max(0, Math.min(100, Math.round((j.progress || 0) * 100)));
    node.querySelector('.task-title').textContent = jobName(j);
    node.querySelector('.task-title').title = j.title || '';
    node.querySelector('.task-pct').textContent =
      j.status === 'done' ? '完成' : (j.status === 'error' ? '失败' : (j.status === 'queued' ? '排队' : pct + '%'));
    node.querySelector('.bar i').style.width = (j.status === 'done' ? 100 : pct) + '%';

    const sub = [];
    const plat = PLAT_NAME[j.platform];
    if (plat) sub.push('<span class="plat">' + esc(plat) + '</span>');
    if (j.status === 'error' && j.error) {
      sub.push('<span class="err">' + esc(String(j.error).slice(0, 90)) + '</span>');
    } else {
      if (j.stage) sub.push('<span>' + esc(j.stage) + '</span>');
      if (j.status === 'running' && j.speed) sub.push('<span>' + esc(j.speed) + '</span>');
      if (j.status === 'running' && j.eta) sub.push('<span>剩 ' + esc(String(j.eta)) + '</span>');
      if (j.status === 'done') {
        if (j.duration) sub.push('<span>' + fmtDur(j.duration) + '</span>');
        if (j.out_size) sub.push('<span>' + human(j.out_size) + '</span>');
      }
    }
    node.querySelector('.task-sub').innerHTML = sub.join('');

    const acts = node.querySelector('.task-acts');
    acts.innerHTML = '';
    if (j.status === 'done' && j.filename) {
      const b = document.createElement('button');
      b.className = 'btn tiny ghost';
      b.textContent = '播放';
      b.onclick = () => playFile(j.filename, jobName(j), j.artist || '');
      acts.append(b);
    }
    if (j.status === 'error' || j.status === 'done') {
      const b = document.createElement('button');
      b.className = 'btn tiny ghost';
      b.textContent = '移除';
      b.onclick = async () => {
        await api('/api/jobs/' + encodeURIComponent(j.id), { method: 'DELETE' });
        jobNodes.delete(j.id);
        node.remove();
        loadJobs();
      };
      acts.append(b);
    }
    if (j.status === 'running' || j.status === 'queued') {
      const b = document.createElement('button');
      b.className = 'btn tiny ghost';
      b.textContent = '取消';
      b.onclick = async () => {
        await api('/api/jobs/' + encodeURIComponent(j.id) + '/cancel', { method: 'POST' });
        loadJobs();
      };
      acts.append(b);
    }
  });

  // 清掉已不在列表里的节点
  const alive = new Set(show.map(j => j.id));
  jobNodes.forEach((n, id) => {
    if (!alive.has(id)) { n.remove(); jobNodes.delete(id); }
  });

  // 有在跑的任务，tab 上点个小灯
  if (live.length && state.tab !== 'task') el.taskDot.classList.remove('hidden');
}

async function loadJobs() {
  const r = await api('/api/jobs');
  if (!r.ok) return;
  state.jobs = (r.jobs || r.items || []).slice();
  renderJobs();
}

/* ---------------- 曲库 ---------------- */
function isVideo(f) {
  return f.kind === 'video' || /\.(mp4|m4v|mov|webm|mkv)$/i.test(f.name || '');
}

/* ---------------- 设置面板 ---------------- */
const REPO = 'ixxgal/music-desk';
const RELEASE_PAGE = 'https://github.com/' + REPO + '/releases/latest';
const DL_PAGE = 'https://github.com/' + REPO + '/releases';

function native() {
  return (window.MDNative && window.MDNative.getMode) ? window.MDNative : null;
}

function modeText() {
  const n = native();
  if (!n) return { name: '浏览器模式', sub: '文件存在电脑上，页面只是遥控器' };
  const mode = n.getMode() || '';
  const srv = (n.getServer && n.getServer()) || '';
  if (mode === 'local') return { name: '本机引擎', sub: '不依赖电脑，歌直接下到这台手机' };
  if (mode === 'remote') return { name: '连接电脑', sub: srv || '（未填地址）' };
  return { name: '未选择', sub: '点下面其中一个开始' };
}

function cmpVer(a, b) {
  const pa = String(a || '0').replace(/^v/, '').split('.').map(n => parseInt(n, 10) || 0);
  const pb = String(b || '0').replace(/^v/, '').split('.').map(n => parseInt(n, 10) || 0);
  for (let i = 0; i < 3; i++) {
    if ((pa[i] || 0) !== (pb[i] || 0)) return (pa[i] || 0) - (pb[i] || 0);
  }
  return 0;
}

async function checkAppUpdate(manual) {
  const cur = state.appVersion || '0';
  const line = document.getElementById('setUpdLine');
  const setLine = (txt, cls) => {
    if (!line) return;
    line.textContent = txt;
    line.className = 'set-sub' + (cls ? ' ' + cls : '');
  };
  if (manual) setLine('正在查…');
  try {
    const r = await fetch('/api/app/update', { cache: 'no-store' });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || '查询失败');
    const latest = String(j.latest || '').replace(/^v/, '');
    state.latestVersion = latest;
    state.apkUrl = j.apk_url || '';
    state.apkSize = j.apk_size || 0;
    if (j.has_update) {
      const mb = state.apkSize ? '（' + (state.apkSize / 1048576).toFixed(1) + ' MB）' : '';
      setLine('有新版本 v' + latest + mb + '，直接点「下载并安装」', 'ok');
      state.hasUpdate = true;
      el.hdSub.textContent = 'v' + cur + ' · 有新版本 v' + latest;
    } else {
      setLine('已是最新（v' + cur + '）', '');
      state.hasUpdate = false;
    }
  } catch (e) {
    setLine(manual ? ('查不到：' + (e.message || e)) : '当前 v' + cur, manual ? 'bad' : '');
  }
}

function showSettingsSheet() {
  const m = modeText();
  const n = native();
  // 每次打开都实时问一次：用户可能刚从系统设置授权回来
  let dir = state.downloadDir || '（未取到）';
  let storageOk = true;
  let pubDir = '';
  if (n && n.downloadDir) {
    try { dir = n.downloadDir() || dir; } catch { /* 忽略 */ }
    try { storageOk = n.storageOk ? !!n.storageOk() : true; } catch { storageOk = true; }
    try { pubDir = n.publicDir ? n.publicDir() : ''; } catch { pubDir = ''; }
  }
  state.downloadDir = dir;
  const cur = state.appVersion || '?';

  const html =
    '<div class="set-group">' +
      '<div class="set-label">运行方式</div>' +
      '<div class="set-now"><b>' + esc(m.name) + '</b><span>' + esc(m.sub) + '</span></div>' +
      (n
        ? '<div class="set-btns">' +
            '<button class="btn ghost" id="setLocal">用这台手机运行</button>' +
            '<button class="btn ghost" id="setRemote">连接电脑</button>' +
          '</div>'
        : '<div class="set-sub">在浏览器里打开时只能连电脑；装成 App 后可以选本机引擎。</div>') +
    '</div>' +

    '<div class="set-group">' +
      '<div class="set-label">下载位置</div>' +
      '<div class="set-now"><b>' + esc(pubDir || '音乐/音乐下载台') + '</b>' +
        '<span>' + (storageOk
          ? '下好的歌会自动存一份到这里，手机自带的音乐 App、文件管理器的「音频」里都能看到'
          : '暂时只能存在应用私有目录') + '</span></div>' +
      (storageOk ? '' :
        '<div class="set-sub bad">安卓 9 及以下要一个存储权限才能写进公共目录。' +
          '点下面的按钮，系统会直接弹授权框。</div>' +
        '<button class="btn primary wide" id="setGrant">允许写入存储</button>') +
      '<div class="set-label" style="margin-top:12px">实际文件目录</div>' +
      '<div class="set-path" id="setPath">' + esc(dir) + '</div>' +
      '<div class="set-sub">系统音乐库里那份是复制出来的，删这里的不会影响那边。</div>' +
      '<div class="set-btns">' +
        '<button class="btn ghost" id="setCopyPath">复制路径</button>' +
        '<button class="btn ghost" id="setRepublish">重新存一遍</button>' +
      '</div>' +
    '</div>' +

    '<div class="set-group">' +
      '<div class="set-label">版本</div>' +
      '<div class="set-row"><span>当前</span><code>v' + esc(cur) + '</code></div>' +
      '<div class="set-sub" id="setUpdLine">' + (state.hasUpdate ? '有新版本 v' + esc(state.latestVersion || '') : '') + '</div>' +
      '<div class="set-btns">' +
        '<button class="btn ghost" id="setCheck">检查更新</button>' +
        (state.hasUpdate
          ? (n
            ? '<button class="btn primary" id="setGoDl">下载并安装</button>'
            : '<button class="btn primary" id="setGoDl">去下载</button>')
          : '<button class="btn ghost" id="setGoDl">下载页</button>') +
      '</div>' +
    '</div>' +

    (n ? '<div class="set-group"><button class="btn ghost wide" id="setReset">重新选一次连接方式</button></div>' : '');

  openSheet('设置', html, () => {
    const click = (id, fn) => { const b = document.getElementById(id); if (b) b.onclick = fn; };

    click('setLocal', () => {
      if (!window.MDNative.useLocal) return;
      closeSheet();
      toast('正在启动本机引擎…');
      setTimeout(() => {
        const url = window.MDNative.useLocal();
        if (url && url.indexOf('http') === 0) location.href = url;
        else toast('引擎启动失败：' + (url || '未知错误'), 'bad');
      }, 80);
    });

    click('setRemote', () => {
      closeSheet();
      setTimeout(() => {
        const cur2 = (window.MDNative.getServer && window.MDNative.getServer()) || '';
        if (cur2 && window.MDNative.useRemote) {
          window.MDNative.useRemote(cur2);
          location.href = cur2.replace(/\/+$/, '') + '/m';
        } else if (window.MDNative.reset) {
          window.MDNative.reset();
        }
      }, 80);
    });

    click('setCopyPath', async () => {
      try {
        await navigator.clipboard.writeText(dir);
        toast('路径已复制');
      } catch {
        toast('复制不了，长按上面的路径手选');
      }
    });

    click('setGrant', () => {
      closeSheet();
      toast('系统会弹一个存储权限，点「允许」');
      setTimeout(() => { if (n.requestStorage) n.requestStorage(); }, 320);
    });

    click('setRepublish', () => {
      closeSheet();
      if (!n || !n.publishAll) { toast('只有装成 App 才有这个功能'); return; }
      toast('正在往系统「音乐」里存…');
      setTimeout(() => n.publishAll(), 200);
    });

    click('setCheck', () => checkAppUpdate(true));

    click('setGoDl', () => {
      // 应用内更新：有新版且有直链，就让系统下载器拉下来直接装
      if (state.hasUpdate && state.apkUrl && n && n.downloadAndInstall) {
        const name = 'MusicDesk-' + (state.latestVersion || 'new') + '.apk';
        closeSheet();
        toast('开始下载 v' + (state.latestVersion || '') + '…');
        setTimeout(() => n.downloadAndInstall(state.apkUrl, name), 200);
        return;
      }
      if (n && n.openUrl) n.openUrl(DL_PAGE);
      else window.open(DL_PAGE, '_blank');
    });

    click('setReset', () => {
      if (window.MDNative.reset) window.MDNative.reset();
    });
  });
}

/* ---------------- 底部弹层（代替原生 select / alert） ---------------- */
let sheetOnClose = null;

function openSheet(title, html, onMount) {
  el.sheetTitle.textContent = title;
  el.sheetBody.innerHTML = html;
  el.sheet.classList.remove('hidden');
  // 让动画生效
  requestAnimationFrame(() => el.sheet.classList.add('on'));
  if (onMount) onMount();
}

function closeSheet() {
  el.sheet.classList.remove('on');
  setTimeout(() => {
    el.sheet.classList.add('hidden');
    el.sheetBody.innerHTML = '';
    if (sheetOnClose) { const f = sheetOnClose; sheetOnClose = null; f(); }
  }, 190);
}

function bindSheetOnce() {
  el.sheetMask.addEventListener('click', closeSheet);
}

function showSrcSheet() {
  const rows = SRC_ITEMS.map(([v, name, desc]) =>
    '<button class="sheet-row' + (state.src === v ? ' on' : '') + '" data-src="' + v + '">' +
      '<span class="sr-main"><span class="sr-name">' + esc(name) + '</span>' +
      '<span class="sr-desc">' + esc(desc) + '</span></span>' +
      (state.src === v
        ? '<svg class="sr-tick" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l5 5L19 7"/></svg>'
        : '') +
    '</button>').join('');
  openSheet('搜索源', rows, () => {
    $$('.sheet-row[data-src]', el.sheetBody).forEach(b => {
      b.onclick = () => {
        state.src = b.dataset.src;
        el.srcNow.textContent = SRC_NAME[state.src] || '自动 · 全部源';
        closeSheet();
      };
    });
  });
}

/** 文件信息：完整路径 + 用系统播放器打开 */
function showFileSheet(f) {
  const name = f.name || '';
  const isApp = window.MDNative && window.MDNative.filePath;
  const path = isApp ? window.MDNative.filePath(name) : '（浏览器模式，文件在电脑上）';
  const meta = [
    isVideo(f) ? '视频' : '音频',
    f.duration ? fmtDur(f.duration) : '',
    human(f.size),
  ].filter(Boolean).join(' · ');

  const html =
    '<div class="sf-name">' + esc(name.replace(/\.[^.]+$/, '')) + '</div>' +
    '<div class="sf-meta">' + esc(meta) + '</div>' +
    '<div class="sf-path-label">文件位置</div>' +
    '<div class="sf-path" id="sfPath">' + esc(path) + '</div>' +
    '<div class="sf-acts">' +
      (isApp ? '<button class="btn primary wide" id="sfOpen">用其他应用打开</button>' : '') +
      '<button class="btn ghost wide" id="sfCopy">复制路径</button>' +
    '</div>';

  openSheet('文件', html, () => {
    const open = $('#sfOpen');
    if (open) open.onclick = () => { closeSheet(); window.MDNative.openFile(name); };
    $('#sfCopy').onclick = async () => {
      try {
        await navigator.clipboard.writeText(path);
        toast('路径已复制');
      } catch {
        toast('复制不了，长按上面的路径手选');
      }
    };
  });
}

function renderFiles() {
  const kw = el.libFilter.value.trim().toLowerCase();
  const files = (state.files || []).filter(f => !kw || (f.name || '').toLowerCase().includes(kw));
  el.libCount.textContent = String((state.files || []).length);

  if (!files.length) {
    el.library.innerHTML = '<div class="empty">' + ((state.files || []).length ? '没有匹配的文件' : '曲库还是空的<br>下载完成的文件会出现在这里') + '</div>';
    return;
  }

  const frag = document.createDocumentFragment();
  files.forEach(f => {
    const vid = isVideo(f);
    const meta = [
      vid ? '视频' : '音频',
      f.duration ? fmtDur(f.duration) : '',
      vid && f.width ? f.width + '×' + f.height : '',
      f.bitrate ? f.bitrate + 'kbps' : '',
      human(f.size),
    ].filter(Boolean).join(' · ');

    const row = document.createElement('div');
    row.className = 'track' + (state.playingName === f.name ? ' playing' : '');
    row.innerHTML =
      '<button class="tk-play" aria-label="播放">' +
        (state.playingName === f.name
          ? '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>'
          : '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5l11 7-11 7z"/></svg>') +
      '</button>' +
      '<div class="tk-main">' +
        '<div class="tk-name">' + esc((f.name || '').replace(/\.[^.]+$/, '')) + '</div>' +
        '<div class="tk-meta">' + esc(meta) + '</div>' +
      '</div>' +
      '<button class="tk-loc" aria-label="文件位置">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7.5A1.5 1.5 0 014.5 6h4l2 2.5h7A1.5 1.5 0 0119 10v7.5a1.5 1.5 0 01-1.5 1.5h-13A1.5 1.5 0 013 17.5z"/></svg>' +
      '</button>' +
      '<button class="tk-del" aria-label="删除">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><path d="M4 7h16M9 7V4.8h6V7M6.5 7l.9 13.2h9.2L17.5 7"/></svg>' +
      '</button>';

    row.querySelector('.tk-play').onclick = () => {
      if (state.playingName === f.name) stopPlay();
      else playFile(f.name, (f.name || '').replace(/\.[^.]+$/, ''), '');
    };

    row.querySelector('.tk-loc').onclick = () => showFileSheet(f);

    row.querySelector('.tk-del').onclick = async () => {
      if (!confirm('删除「' + (f.name || '') + '」？')) return;
      const r = await api('/api/files?name=' + encodeURIComponent(f.name), { method: 'DELETE' });
      if (r.ok) {
        if (state.playingName === f.name) stopPlay();
        toast('已删除');
        loadFiles();
      } else {
        toast(r.error || '删除失败', 'bad');
      }
    };

    frag.append(row);
  });

  el.library.innerHTML = '';
  el.library.append(frag);
}

async function loadFiles() {
  // 悄悄把新下的歌补送到系统「音乐」（MediaStore，安卓 10+ 不需要权限）
  try {
    if (window.MDNative && window.MDNative.publishQuiet) window.MDNative.publishQuiet();
    else if (window.MDNative && window.MDNative.scanDownloads) window.MDNative.scanDownloads();
  } catch { /* 忽略 */ }
  const r = await api('/api/files');
  if (!r.ok) return;
  state.files = r.files || [];
  renderFiles();
}

/* ---------------- 记录 ---------------- */
function renderHist() {
  const list = state.hist || [];
  el.histCount.textContent = String(list.length);
  if (!list.length) {
    el.history.innerHTML = '<div class="empty">下载成功或失败的任务会记在这里</div>';
    return;
  }

  const frag = document.createDocumentFragment();
  list.forEach(h => {
    const ok = h.status === 'done';
    const row = document.createElement('div');
    row.className = 'hist';
    const sub = [];
    if (h.platform && PLAT_NAME[h.platform]) sub.push(PLAT_NAME[h.platform]);
    if (ok) {
      if (h.duration) sub.push(fmtDur(h.duration));
      if (h.size) sub.push(human(h.size));
    } else if (h.error) {
      sub.push(String(h.error).slice(0, 60));
    }
    row.innerHTML =
      '<div class="hist-ico' + (ok ? '' : ' bad') + '">' +
        (ok
          ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><path d="M5 12.5l4.5 4.5L19 6.5"/></svg>'
          : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>') +
      '</div>' +
      '<div class="hist-main">' +
        '<div class="hist-title">' + esc(h.title || h.filename || h.id) + '</div>' +
        '<div class="hist-sub">' + esc(sub.join(' · ')) + '</div>' +
      '</div>' +
      '<div class="hist-time">' + esc(ago(h.finished)) + '</div>';
    frag.append(row);
  });

  el.history.innerHTML = '';
  el.history.append(frag);
}

async function loadHist() {
  const r = await api('/api/history?limit=120');
  if (!r.ok) return;
  state.hist = r.items || [];
  renderHist();
}

/* ---------------- 播放 ---------------- */
function playFile(name, title, artist) {
  const vid = /\.(mp4|m4v|mov|webm|mkv)$/i.test(name || '');
  state.playing = name;
  state.playingName = name;

  el.plTitle.textContent = title || name;
  el.plSub.textContent = artist || (vid ? '视频' : '音频');
  el.player.classList.remove('hidden');

  const url = '/media/' + encodeURIComponent(name);
  if (vid) {
    el.audio.pause();
    el.audio.classList.add('hidden');
    el.video.classList.remove('hidden');
    el.video.src = url;
    el.video.play().catch(() => {});
  } else {
    el.video.pause();
    el.video.removeAttribute('src');
    el.video.classList.add('hidden');
    el.audio.classList.remove('hidden');
    el.audio.src = url;
    el.audio.play().catch(() => {});
  }
  renderFiles();
}

function stopPlay() {
  try { el.audio.pause(); } catch {}
  try { el.video.pause(); } catch {}
  el.audio.removeAttribute('src');
  el.video.removeAttribute('src');
  el.audio.classList.add('hidden');
  el.video.classList.add('hidden');
  state.playing = '';
  state.playingName = '';
  el.player.classList.add('hidden');
  renderFiles();
}

el.btnStop.onclick = stopPlay;

[el.audio, el.video].forEach(node => {
  node.addEventListener('ended', () => {
    // 播完自动下一首
    const files = (state.files || []).filter(f => !isVideo(f));
    const i = files.findIndex(f => f.name === state.playingName);
    if (i >= 0 && i + 1 < files.length) {
      const nx = files[i + 1];
      playFile(nx.name, (nx.name || '').replace(/\.[^.]+$/, ''), '');
    } else {
      stopPlay();
    }
  });
});

/* ---------------- 轮询 ---------------- */
let pollTimer = 0;
function schedulePoll() {
  clearTimeout(pollTimer);
  const gap = state.tab === 'task' ? 1200 : 3000;
  pollTimer = setTimeout(async () => {
    try {
      if (state.tab === 'task') await loadJobs();
      else if (state.tab === 'lib') await loadFiles();
      else {
        // 后台也刷新任务，用来点亮小灯
        const r = await api('/api/jobs');
        if (r.ok) { state.jobs = (r.jobs || r.items || []); renderJobs(); }
      }
    } catch {}
    schedulePoll();
  }, gap);
}

/* ---------------- 绑定 ---------------- */
el.btnGo.onclick = submit;

el.q.addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); submit(); }
});

el.btnPaste.onclick = async () => {
  try {
    const txt = await navigator.clipboard.readText();
    if (!txt) { toast('剪贴板是空的', 'bad'); return; }
    el.q.value = txt.trim();
    el.q.focus();
    toast('已粘贴');
    if (extractUrls(txt).length) submit();
  } catch {
    el.q.focus();
    toast('这个环境读不了剪贴板，长按输入框粘贴就行');
  }
};

el.btnRefresh.onclick = async () => {
  el.btnRefresh.classList.add('spin');
  try {
    await ping();
    if (state.tab === 'task') await loadJobs();
    if (state.tab === 'lib') await loadFiles();
    if (state.tab === 'hist') await loadHist();
    toast('已刷新');
  } finally {
    el.btnRefresh.classList.remove('spin');
  }
};

el.libFilter.addEventListener('input', renderFiles);

document.getElementById('chips').addEventListener('click', e => {
  const c = e.target.closest('.chip');
  if (!c) return;
  el.q.value = c.dataset.q || c.textContent;
  submit();
});

el.btnHistClear.onclick = async () => {
  if (!confirm('清空全部下载记录？（不影响磁盘上的文件）')) return;
  await api('/api/history', { method: 'DELETE' });
  await loadHist();
  toast('记录已清空');
};

/* 粘贴整段分享文案到输入框，自动识别链接就开始下 */
el.q.addEventListener('paste', e => {
  const txt = (e.clipboardData || window.clipboardData)?.getData('text') || '';
  if (extractUrls(txt).length) {
    setTimeout(() => {
      el.q.value = txt.trim();
      submit();
    }, 60);
  }
});

/* ---------------- 启动 ---------------- */
async function ping() {
  try {
    const r = await api('/api/ping');
    if (r.ok) {
      state.appVersion = r.version || '?';
      el.hdSub.textContent = '已连接 · v' + state.appVersion;
      return true;
    }
    el.hdSub.textContent = '服务异常';
  } catch {
    el.hdSub.textContent = '连接不上服务';
  }
  return false;
}

async function loadSettings() {
  // App 里能直接拿到真实目录（应用私有目录），比后端返回的更直观
  const n = native();
  if (n && n.downloadDir) {
    try { state.downloadDir = n.downloadDir(); } catch { /* 忽略 */ }
  }
  try {
    const r = await api('/api/settings');
    if (r.ok && r.settings && !state.downloadDir) {
      state.downloadDir = r.settings.download_dir || '';
    }
  } catch { /* 忽略，设置面板里会显示未取到 */ }
}

(async function boot() {
  bindSheetOnce();
  el.btnSrc.addEventListener('click', showSrcSheet);
  if (el.btnSettings) el.btnSettings.addEventListener('click', showSettingsSheet);

  await ping();
  await loadSettings();
  await loadJobs();
  await loadHist();
  await loadFiles();
  const qs = new URLSearchParams(location.search);
  if (qs.get('q')) {
    el.q.value = qs.get('q');
    switchTab('dl');
    submit();
  } else if (qs.get('tab')) {
    switchTab(qs.get('tab'));
  }
  schedulePoll();

  // 静默查一次新版：查不到就算了，不打扰
  setTimeout(() => checkAppUpdate(false), 2500);

  // 从系统设置授权回来：如果下载目录变了，重启本机引擎让新目录生效
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible') return;
    const n = native();
    if (!n || !n.downloadDir) return;
    let d = '';
    try { d = n.downloadDir() || ''; } catch { return; }
    if (!d) return;
    if (state.downloadDir && d !== state.downloadDir) {
      state.downloadDir = d;
      toast('下载位置已更新，正在重启引擎…');
      setTimeout(() => { if (n.restartLocal) n.restartLocal(); else location.reload(); }, 700);
    } else {
      state.downloadDir = d;
    }
  });

  // PWA：注册 service worker，让「添加到主屏幕」后能全屏打开、断网也认得壳
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  }
})();