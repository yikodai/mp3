/* UI 端到端验证（无第三方依赖，直接通过 CDP 驱动本机 Edge headless）
 *
 * 步骤：
 *   1. 启动 headless Edge（用户数据目录放在工作区 .tmp 内）
 *   2. 打开 http://127.0.0.1:8765/，收集控制台报错 / 未捕获异常
 *   3. 校验 init 后的 DOM 状态（health / count / accept / 按钮禁用）
 *   4. 通过 CDP 上传一个正常 wav + 一个损坏文件
 *   5. 点击「转换为 MP3」，等待完成，校验成功/失败统计与下载入口
 *   6. 截图保存到 tests/out/ui.png
 *
 * 用法： node tests/ui_test.js
 */
'use strict';

const { spawn, execSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const EDGE = process.env.A2M_EDGE
  || 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const URL = process.env.A2M_URL || 'http://127.0.0.1:8765/';
const CDP_PORT = 9333;
const ROOT = path.resolve(__dirname, '..');
const PROFILE = path.join(ROOT, '.tmp', 'browser');
const OUT = path.join(ROOT, 'tests', 'out');
const SAMPLE = path.join(OUT, 'sample_中文.wav');
const BROKEN = path.join(OUT, 'broken.txt');

const results = [];
function record(name, ok, detail) {
  results.push({ name, ok: !!ok, detail: detail || '' });
  console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${name}` + (detail ? `  -> ${detail}` : ''));
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function getJson(url) {
  const r = await fetch(url);
  return r.json();
}

class CDP {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    this.events = [];
    ws.onmessage = (e) => {
      const m = JSON.parse(e.data);
      if (m.id && this.pending.has(m.id)) {
        const { res, rej } = this.pending.get(m.id);
        this.pending.delete(m.id);
        m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result);
      } else if (m.method) {
        this.events.push(m);
      }
    };
  }
  send(method, params) {
    const id = ++this.id;
    return new Promise((res, rej) => {
      this.pending.set(id, { res, rej });
      this.ws.send(JSON.stringify({ id, method, params: params || {} }));
    });
  }
  async waitEvent(method, timeout = 15000) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      const i = this.events.findIndex((e) => e.method === method);
      if (i >= 0) return this.events.splice(i, 1)[0];
      await sleep(50);
    }
    return null;
  }
  async evaluate(expression) {
    const r = await this.send('Runtime.evaluate', {
      expression, returnByValue: true, awaitPromise: true,
    });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.text);
    return r.result.value;
  }
}

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  fs.mkdirSync(PROFILE, { recursive: true });
  fs.writeFileSync(BROKEN, 'this is not an audio file', 'utf8');
  if (!fs.existsSync(SAMPLE)) throw new Error('缺少测试音频：' + SAMPLE);

  const child = spawn(EDGE, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--mute-audio',
    `--remote-debugging-port=${CDP_PORT}`,
    `--user-data-dir=${PROFILE}`,
    'about:blank',
  ], { stdio: 'ignore' });

  let cdp;
  try {
    // 等待调试端口就绪
    let target = null;
    for (let i = 0; i < 60 && !target; i++) {
      await sleep(300);
      try {
        const list = await getJson(`http://127.0.0.1:${CDP_PORT}/json/list`);
        target = list.find((t) => t.type === 'page');
      } catch (e) { /* 还没起来 */ }
    }
    if (!target) throw new Error('无法连接 Edge 调试端口');

    const ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    cdp = new CDP(ws);

    await cdp.send('Runtime.enable');
    await cdp.send('Log.enable');
    await cdp.send('Page.enable');
    await cdp.send('DOM.enable');

    await cdp.send('Page.navigate', { url: URL });
    await cdp.waitEvent('Page.loadEventFired');
    await sleep(2500);                       // 等 fetch(/api/health, /api/formats) 完成

    // ---- 控制台错误 ----
    const errors = cdp.events
      .filter((e) =>
        (e.method === 'Runtime.exceptionThrown') ||
        (e.method === 'Runtime.consoleAPICalled' && e.params.type === 'error') ||
        (e.method === 'Log.entryAdded' && e.params.entry.level === 'error'))
      .map((e) => e.method === 'Log.entryAdded'
        ? e.params.entry.text
        : (e.params.exceptionDetails ? e.params.exceptionDetails.text : 'console.error'));
    record('页面无控制台错误', errors.length === 0, errors.join(' | ').slice(0, 200));

    // ---- init 后的 DOM 状态 ----
    const st = JSON.parse(await cdp.evaluate(`JSON.stringify({
      title: document.title,
      health: document.getElementById('health').textContent,
      healthBad: document.getElementById('health').classList.contains('bad'),
      count: document.getElementById('count').textContent,
      btnDisabled: document.getElementById('convertBtn').disabled,
      accept: document.getElementById('fileInput').accept,
      empty: document.getElementById('empty').hidden,
      panels: document.querySelectorAll('.panel').length,
      dropzone: !!document.getElementById('dropzone')
    })`));
    record('标题正确', st.title.includes('音频转 MP3'), st.title);
    record('健康检查已渲染', st.health.includes('服务正常') && !st.healthBad, st.health);
    record('格式列表已应用为 accept', st.accept.includes('.wav') && st.accept.includes('.flac'),
      st.accept.slice(0, 60) + '…');
    record('初始状态：按钮禁用 + 空列表', st.btnDisabled === true && st.count === '共 0 个文件',
      `btn=${st.btnDisabled} count=${st.count}`);
    record('页面结构完整（拖拽区 + 面板）', st.dropzone && st.panels >= 4, `panels=${st.panels}`);

    // ---- 上传文件 ----
    const doc = await cdp.send('DOM.getDocument');
    const input = await cdp.send('DOM.querySelector', { nodeId: doc.root.nodeId, selector: '#fileInput' });
    await cdp.send('DOM.setFileInputFiles', { nodeId: input.nodeId, files: [SAMPLE, BROKEN] });
    await sleep(600);

    const afterAdd = JSON.parse(await cdp.evaluate(`JSON.stringify({
      count: document.getElementById('count').textContent,
      rows: document.querySelectorAll('#list li').length,
      btnDisabled: document.getElementById('convertBtn').disabled
    })`));
    record('上传后加入队列', afterAdd.count === '共 2 个文件' && afterAdd.rows === 2,
      `count=${afterAdd.count} rows=${afterAdd.rows}`);
    record('上传后转换按钮可用', afterAdd.btnDisabled === false, `btn=${afterAdd.btnDisabled}`);

    // ---- 点击转换 ----
    await cdp.evaluate(`document.getElementById('convertBtn').click()`);
    let done = false;
    for (let i = 0; i < 60; i++) {
      await sleep(500);
      const s = await cdp.evaluate(`document.getElementById('status').textContent`);
      if (s && s.includes('完成')) { done = true; break; }
    }

    const conv = JSON.parse(await cdp.evaluate(`JSON.stringify({
      status: document.getElementById('status').textContent,
      resultsHidden: document.getElementById('resultsPanel').hidden,
      summary: document.getElementById('summary').textContent,
      ok: document.querySelectorAll('#results .badge.ok').length,
      fail: document.querySelectorAll('#results .badge.fail').length,
      downloadLinks: document.querySelectorAll('#results a.dl').length,
      zip: document.getElementById('zipRow').hidden === false,
      convertLabel: document.getElementById('convertLabel').textContent,
      btnReenabled: document.getElementById('convertBtn').disabled === false,
      retryVisible: document.getElementById('retryBtn').hidden === false
    })`));
    record('转换完成', done && conv.status.includes('完成'), conv.status);
    record('结果面板展示（1 成功 / 1 失败）', conv.resultsHidden === false && conv.ok === 1 && conv.fail === 1,
      `ok=${conv.ok} fail=${conv.fail} summary=${conv.summary}`);
    record('成功项提供逐文件下载', conv.downloadLinks === 1, `links=${conv.downloadLinks}`);
    record('ZIP 打包入口出现', conv.zip === true, `zipRowVisible=${conv.zip}`);
    record('按钮恢复（加载态已解除）',
      conv.convertLabel === '转换为 MP3' && conv.btnReenabled === true,
      `label=${conv.convertLabel} enabled=${conv.btnReenabled}`);
    record('失败后出现「重试失败项」', conv.retryVisible === true, `retry=${conv.retryVisible}`);

    // ---- 截图 ----
    const shot = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
    fs.writeFileSync(path.join(OUT, 'ui.png'), Buffer.from(shot.data, 'base64'));
    record('截图已保存 tests/out/ui.png', true, 'ui.png');
  } finally {
    try { if (cdp) await cdp.send('Browser.close'); } catch (e) { /* ignore */ }
    await sleep(500);
    try { execSync(`taskkill /PID ${child.pid} /T /F`, { stdio: 'ignore' }); } catch (e) { /* ignore */ }
  }

  const passed = results.filter((r) => r.ok).length;
  console.log(`\n  UI 结果：${passed}/${results.length} 通过`);
  fs.writeFileSync(path.join(OUT, 'ui_results.json'), JSON.stringify(results, null, 2), 'utf8');
  process.exit(passed === results.length ? 0 : 1);
}

main().catch((e) => { console.error('UI 测试异常：', e); process.exit(2); });
