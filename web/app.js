/* 音频转 MP3 · Web 版 —— 前端逻辑
 *
 * 设计要点：
 *  - 零外部依赖（不使用任何 CDN），离线可用。
 *  - 逐个文件上传到 POST /api/convert，天然获得「逐文件进度」。
 *  - 需要 ZIP 时，把已转换成功的 MP3 再按自定义帧格式 POST 到 /api/zip，
 *    由服务器在内存里完成打包（复用 Python zipfile）；服务端全程不落盘。
 *  - 若服务端启用了访问令牌，页面通过 Cookie 自动携带，前端无需处理 token。
 *
 * 自定义帧格式（与后端 web_server.py 约定一致，大端序）：
 *   重复若干块：[u32 名称长度][名称 UTF-8][u32 数据长度][数据]
 */
'use strict';

const $ = (id) => document.getElementById(id);

const state = {
  files: [],                       // { file, status, outName, url, blob, err }
  running: false,
  limits: { maxFileMb: 0 },        // 0 表示未知
};

/* ------------------------------------------------------------------ 工具 */
function fmtSize(n) {
  const u = ['B', 'KB', 'MB', 'GB'];
  let v = n, i = 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (i === 0 ? v : v.toFixed(1)) + ' ' + u[i];
}

function extOf(name) {
  const i = name.lastIndexOf('.');
  return i >= 0 ? name.slice(i + 1).toUpperCase() : '未知';
}

function decodeHeaderName(v) {
  if (!v) return '';
  try { return decodeURIComponent(v); } catch (e) { return v; }
}

let toastTimer = null;
function toast(message, kind) {
  const el = $('toast');
  el.textContent = message;
  el.className = 'toast show' + (kind === 'warn' ? ' warn' : '');
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 6000);
}

function clearToast() {
  clearTimeout(toastTimer);
  $('toast').hidden = true;
}

/* ------------------------------------------------------------------ 列表 */
function badgeClass(s) {
  return { wait: 'wait', running: 'run', ok: 'ok', fail: 'fail' }[s] || 'wait';
}

function badgeText(item) {
  switch (item.status) {
    case 'running': return '转换中…';
    case 'ok': return '成功';
    case 'fail': return '失败';
    default: return '等待';
  }
}

function renderList() {
  const ul = $('list');
  ul.innerHTML = '';

  state.files.forEach((item, idx) => {
    const li = document.createElement('li');

    const name = document.createElement('span');
    name.className = 'fname';
    name.textContent = item.file.name;
    name.title = item.file.name;

    const meta = document.createElement('span');
    meta.className = 'fmeta';
    meta.textContent = extOf(item.file.name) + ' · ' + fmtSize(item.file.size);

    const badge = document.createElement('span');
    badge.className = 'badge ' + badgeClass(item.status);
    badge.textContent = badgeText(item);
    if (item.err) badge.title = item.err;

    li.append(name, meta, badge);

    if (item.url) {
      const a = document.createElement('a');
      a.className = 'dl';
      a.href = item.url;
      a.download = item.outName || (item.file.name.replace(/\.[^.]+$/, '') + '.mp3');
      a.textContent = '下载';
      li.append(a);
    }

    if (!state.running) {
      const rm = document.createElement('button');
      rm.className = 'rm';
      rm.type = 'button';
      rm.textContent = '×';
      rm.title = '移除';
      rm.setAttribute('aria-label', '移除 ' + item.file.name);
      rm.onclick = () => { revoke(item); state.files.splice(idx, 1); renderList(); };
      li.append(rm);
    }

    ul.append(li);
  });

  $('count').textContent = `共 ${state.files.length} 个文件`;
  $('empty').hidden = state.files.length > 0;
  $('convertBtn').disabled = state.running || state.files.length === 0;
  updateLimitHint();
}

function updateLimitHint() {
  const max = state.limits.maxFileMb;
  const el = $('limitHint');
  if (!max) { el.textContent = ''; return; }
  const over = state.files.filter((it) => it.file.size > max * 1024 * 1024).length;
  el.textContent = over > 0 ? `${over} 个文件超过单文件上限 ${max} MB` : '';
}

function oversized(item) {
  const max = state.limits.maxFileMb;
  return max > 0 && item.file.size > max * 1024 * 1024;
}

function revoke(item) {
  if (item.url) { URL.revokeObjectURL(item.url); item.url = null; }
}

/* --------------------------------------------------------------- 添加文件 */
function addFiles(fileList) {
  let added = 0;
  for (const f of fileList) {
    if (state.files.some((it) => it.file.name === f.name && it.file.size === f.size)) continue;
    state.files.push({ file: f, status: 'wait', outName: null, url: null, blob: null, err: null });
    added++;
  }
  renderList();
  return added;
}

/* ------------------------------------------------------------------ 转换 */
async function convert(targets) {
  if (state.running || targets.length === 0) return;

  state.running = true;
  clearToast();
  const bitrate = $('bitrate').value;
  const wantZip = $('zipCheck').checked;
  const bar = $('bar');
  const btn = $('convertBtn');

  btn.classList.add('loading');
  $('convertLabel').textContent = '转换中…';
  $('retryBtn').hidden = true;

  // 重置目标项状态
  $('resultsPanel').hidden = true;
  $('zipRow').hidden = true;
  $('zipRow').innerHTML = '';
  $('results').innerHTML = '';
  bar.classList.remove('done');
  bar.style.width = '0%';

  targets.forEach((it) => { revoke(it); it.blob = null; it.status = 'wait'; it.err = null; it.outName = null; });
  renderList();

  const total = targets.length;
  let done = 0;
  let authFailed = false;

  for (const item of targets) {
    item.status = 'running';
    renderList();
    $('status').textContent = `正在处理 ${done + 1}/${total}：${item.file.name}`;

    if (oversized(item)) {
      item.status = 'fail';
      item.err = `文件超过服务器单文件上限 ${state.limits.maxFileMb} MB`;
      done++;
      bar.style.width = Math.round((done / total) * 100) + '%';
      renderList();
      continue;
    }

    try {
      const url = `/api/convert?bitrate=${encodeURIComponent(bitrate)}`
                + `&name=${encodeURIComponent(item.file.name)}`;
      const resp = await fetch(url, { method: 'POST', body: item.file });

      if (!resp.ok) {
        const msg = await readError(resp);
        if (resp.status === 401) { authFailed = true; }
        throw new Error(msg);
      }

      const blob = await resp.blob();
      item.status = 'ok';
      item.outName = decodeHeaderName(resp.headers.get('X-Out-Name'))
                   || item.file.name.replace(/\.[^.]+$/, '') + '.mp3';
      item.blob = blob;
      item.url = URL.createObjectURL(blob);
    } catch (e) {
      item.status = 'fail';
      item.err = e.message || String(e);
    }

    done++;
    bar.style.width = Math.round((done / total) * 100) + '%';
    renderList();

    if (authFailed) {
      // location.host 已包含 IPv6 的方括号，直接复用即可给出可点击形式
      toast(`访问被拒绝（401）：请在地址栏用 ?token=你的令牌 重新打开，`
          + `例如 http://${location.host}/?token=你的令牌`);
      break;
    }
  }

  const oks = state.files.filter((it) => it.status === 'ok');
  const fails = state.files.filter((it) => it.status === 'fail');

  if (wantZip && oks.length > 0) {
    $('status').textContent = '正在打包 ZIP……';
    try {
      const zipBlob = await postZip(oks);
      const zipUrl = URL.createObjectURL(zipBlob);
      const a = document.createElement('a');
      a.href = zipUrl;
      a.download = window.__zipName || 'mp3_output.zip';
      a.textContent = `下载 ZIP 压缩包（${oks.length} 个文件）`;
      const row = $('zipRow');
      row.innerHTML = '';
      row.appendChild(a);
      row.hidden = false;
      a.click();                     // 自动触发下载
    } catch (e) {
      fails.push({ file: { name: 'ZIP 打包' }, err: e.message || String(e), status: 'fail' });
    }
  }

  bar.style.width = '100%';
  if (fails.length === 0 && oks.length > 0) bar.classList.add('done');
  $('status').textContent = `完成：成功 ${oks.length} 个，失败 ${fails.length} 个`;
  renderResults(oks, fails);

  state.running = false;
  btn.classList.remove('loading');
  $('convertLabel').textContent = '转换为 MP3';
  $('retryBtn').hidden = fails.length === 0;
  renderList();

  if (fails.length > 0) {
    const firstErr = fails.find((f) => f.err);
    toast(`有 ${fails.length} 个文件失败` + (firstErr ? `：${firstErr.err}` : ''),
          'warn');
  }
}

async function readError(resp) {
  let msg = 'HTTP ' + resp.status;
  try {
    const j = await resp.json();
    if (j && j.error) msg = j.error;
  } catch (e) { /* 非 JSON */ }
  return msg;
}

function renderResults(oks, fails) {
  const ul = $('results');
  ul.innerHTML = '';
  $('summary').textContent = `成功 ${oks.length} 个，失败 ${fails.length} 个。`;

  for (const it of oks) {
    const li = document.createElement('li');
    const name = document.createElement('span');
    name.className = 'fname';
    name.textContent = it.file.name;
    const b = document.createElement('span');
    b.className = 'badge ok';
    b.textContent = '成功';
    li.append(name, b);
    if (it.url) {
      const a = document.createElement('a');
      a.className = 'dl';
      a.href = it.url;
      a.download = it.outName || 'output.mp3';
      a.textContent = '下载';
      li.append(a);
    }
    ul.append(li);
  }

  for (const it of fails) {
    const li = document.createElement('li');
    const name = document.createElement('span');
    name.className = 'fname';
    name.textContent = it.file.name;
    const b = document.createElement('span');
    b.className = 'badge fail';
    b.textContent = '失败';
    b.title = it.err || '';
    li.append(name, b);
    if (it.err) {
      const e = document.createElement('span');
      e.className = 'fmeta';
      e.textContent = it.err;
      e.title = it.err;
      li.append(e);
    }
    ul.append(li);
  }

  $('resultsPanel').hidden = false;
}

/* ------------------------------------------------------- ZIP（自定义帧） */
async function postZip(items) {
  const parts = [];
  const enc = new TextEncoder();
  let size = 0;

  for (const it of items) {
    if (!it.blob) continue;
    const nameBytes = enc.encode(it.outName || 'output.mp3');
    const data = new Uint8Array(await it.blob.arrayBuffer());
    parts.push({ nameBytes, data });
    size += 4 + nameBytes.length + 4 + data.length;
  }

  const buf = new ArrayBuffer(size);
  const view = new DataView(buf);
  const bytes = new Uint8Array(buf);
  let off = 0;
  for (const p of parts) {
    view.setUint32(off, p.nameBytes.length, false); off += 4;
    bytes.set(p.nameBytes, off); off += p.nameBytes.length;
    view.setUint32(off, p.data.length, false); off += 4;
    bytes.set(p.data, off); off += p.data.length;
  }

  const resp = await fetch('/api/zip', {
    method: 'POST',
    headers: { 'Content-Type': 'application/octet-stream' },
    body: buf,
  });
  if (!resp.ok) throw new Error('打包失败：' + await readError(resp));
  window.__zipName = decodeHeaderName(resp.headers.get('X-Out-Name')) || 'mp3_output.zip';
  return await resp.blob();
}

/* ------------------------------------------------------------------ 初始化 */
function init() {
  $('pickBtn').onclick = (e) => { e.stopPropagation(); $('fileInput').click(); };
  $('fileInput').onchange = (e) => { addFiles(e.target.files); e.target.value = ''; };

  const dz = $('dropzone');
  let dragDepth = 0;
  dz.onclick = () => { if (!state.running) $('fileInput').click(); };
  dz.onkeydown = (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('fileInput').click(); }
  };
  ['dragenter', 'dragover'].forEach((ev) =>
    dz.addEventListener(ev, (e) => {
      e.preventDefault();
      if (ev === 'dragenter') dragDepth++;
      dz.classList.add('hover');
    }));
  ['dragleave', 'drop'].forEach((ev) =>
    dz.addEventListener(ev, (e) => {
      e.preventDefault();
      if (ev === 'dragleave') { dragDepth = Math.max(0, dragDepth - 1); if (dragDepth) return; }
      if (ev === 'drop') dragDepth = 0;
      dz.classList.remove('hover');
    }));
  dz.addEventListener('drop', (e) => {
    if (e.dataTransfer && e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
  });
  // 整页拖入时不要让浏览器直接打开文件
  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('drop', (e) => e.preventDefault());

  $('clearBtn').onclick = () => {
    if (state.running) return;
    state.files.forEach(revoke);
    state.files = [];
    $('resultsPanel').hidden = true;
    clearToast();
    renderList();
  };

  $('convertBtn').onclick = () => convert(state.files.slice());
  $('retryBtn').onclick = () => convert(state.files.filter((it) => it.status === 'fail'));

  // 用后端返回的格式列表更新文件选择器的接受范围（顺带做健康检查）
  fetch('/api/formats').then((r) => r.json()).then((data) => {
    if (data && Array.isArray(data.extensions)) {
      $('fileInput').accept = data.extensions.map((x) => '.' + x).join(',');
    }
    if (data && data.max_file_mb) { state.limits.maxFileMb = data.max_file_mb; renderList(); }
  }).catch(() => {});

  fetch('/api/health').then((r) => r.json()).then((h) => {
    if (h && h.max_file_mb) state.limits.maxFileMb = h.max_file_mb;
    const bits = [`服务正常`, h.engine || 'codecpod'];
    if (h.bind_family) bits.push(h.dual_stack ? `${h.bind_family}+IPv4` : h.bind_family);
    if (h.listen) bits.push(h.listen);
    if (h.max_file_mb) bits.push(`单文件 ≤ ${h.max_file_mb} MB`);
    if (h.auth_required) bits.push('已启用令牌');
    $('health').textContent = bits.join(' · ');
    $('health').classList.remove('bad');
    renderList();
  }).catch(() => {
    $('health').textContent = '服务连接失败';
    $('health').classList.add('bad');
    toast('无法连接后端服务：请确认服务已启动；'
        + 'IPv6 地址需写成 http://[IPv6地址]:端口/ 的形式。');
  });

  renderList();
}

init();
