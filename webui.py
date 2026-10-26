# -*- coding: utf-8 -*-
"""ClipShare 本地设置面板（单页 HTML，由内置 HTTP 服务直接吐出）。"""

HTML_UI = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ClipShare 设置</title>
<style>
  :root{
    --bg:#f5f6f8; --card:#fff; --line:#e5e7eb; --text:#1f2328; --muted:#6b7280;
    --accent:#2f6fed; --ok:#16a34a; --bad:#dc2626; --radius:10px;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
    font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;}
  .wrap{max-width:820px;margin:0 auto;padding:24px 16px 60px}
  header{display:flex;align-items:center;gap:12px;margin-bottom:20px}
  .logo{width:40px;height:40px;border-radius:11px;background:var(--accent);color:#fff;
    display:flex;align-items:center;justify-content:center;font-weight:700;font-size:18px}
  h1{font-size:18px;margin:0}
  .sub{color:var(--muted);font-size:12px}
  .badge{margin-left:auto;background:#e8f5ec;color:var(--ok);border:1px solid #cbe8d4;
    padding:3px 10px;border-radius:20px;font-size:12px}
  .badge.off{background:#fdeaea;color:var(--bad);border-color:#f6cece}
  .card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
    padding:16px 18px;margin-bottom:14px}
  h2{font-size:14px;margin:0 0 12px;display:flex;align-items:center;gap:8px}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
  .grid label{display:block;color:var(--muted);font-size:12px;margin-bottom:2px}
  .grid div>div{font-weight:600;word-break:break-all}
  input[type=text],input:not([type]){width:100%;padding:8px 10px;border:1px solid var(--line);
    border-radius:8px;font:inherit;background:#fff}
  input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgba(47,111,237,.12)}
  button{padding:8px 14px;border:1px solid var(--line);background:#fff;border-radius:8px;
    font:inherit;cursor:pointer}
  button:hover{background:#f3f4f6}
  button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
  button.primary:hover{filter:brightness(.95)}
  button.mini{padding:2px 8px;font-size:12px}
  .row{display:flex;gap:8px;margin-bottom:8px}
  .row3{display:grid;grid-template-columns:2fr 1fr 2fr;gap:8px;margin:8px 0}
  .hint{color:var(--muted);font-size:12px;margin:8px 0 0}
  code{background:#f3f4f6;padding:2px 6px;border-radius:5px;font-size:12px;word-break:break-all}
  details summary{cursor:pointer;color:var(--accent);font-size:12px;margin-bottom:4px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line)}
  th{color:var(--muted);font-weight:500;font-size:12px}
  .dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px}
  .dot.ok{background:var(--ok)} .dot.bad{background:var(--bad)} .dot.unk{background:#c7cbd1}
  .switch{display:flex;align-items:center;gap:8px;margin:8px 0;cursor:pointer}
  .switch input{width:16px;height:16px;accent-color:var(--accent)}
  pre#log{background:#0f172a;color:#d6e2f0;padding:12px;border-radius:8px;height:240px;
    overflow:auto;font:12px/1.6 Consolas,Menlo,monospace;white-space:pre-wrap;margin:0}
  .toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%) translateY(20px);
    background:#1f2328;color:#fff;padding:9px 18px;border-radius:20px;font-size:13px;
    opacity:0;transition:.25s;pointer-events:none}
  .toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo">C</div>
    <div>
      <h1>ClipShare</h1>
      <div class="sub" id="hostname">加载中…</div>
    </div>
    <div class="badge" id="state">运行中</div>
  </header>

  <section class="card">
    <h2>状态</h2>
    <div class="grid">
      <div><label>监听地址</label><div id="listen">-</div></div>
      <div><label>已绑定对端</label><div id="peersCount">-</div></div>
      <div><label>推送 / 接收</label><div id="counts">-</div></div>
      <div><label>最近接收</label><div id="lastRecv">-</div></div>
    </div>
  </section>

  <section class="card">
    <h2>我的配对链接</h2>
    <div class="row">
      <input id="mylink" readonly>
      <button onclick="copyLink()">复制</button>
    </div>
    <p class="hint">在另一台机器上粘贴绑定即可，需要双向请互相绑定一次。</p>
  </section>

  <section class="card">
    <h2>绑定对端</h2>
    <div class="row">
      <input id="linkinput" placeholder="粘贴配对链接：clipshare://IP:端口/令牌?name=主机名">
      <button class="primary" onclick="bindLink()">绑定</button>
    </div>
    <details>
      <summary>或手动填写 IP / 端口 / 令牌</summary>
      <div class="row3">
        <input id="h" placeholder="IP，如 192.168.1.184">
        <input id="p" placeholder="端口 8765">
        <input id="k" placeholder="对方令牌">
      </div>
      <div class="row">
        <input id="n" placeholder="备注名（可选）">
        <button onclick="bindManual()">绑定</button>
      </div>
    </details>
  </section>

  <section class="card">
    <h2>已绑定对端</h2>
    <table>
      <thead><tr><th>名称</th><th>地址</th><th>状态</th><th></th></tr></thead>
      <tbody id="peerRows"><tr><td colspan="4" style="color:#6b7280">尚未绑定</td></tr></tbody>
    </table>
  </section>

  <section class="card">
    <h2>开关</h2>
    <label class="switch"><input type="checkbox" id="swSync" onchange="setPause(this)"> 启用同步（取消勾选则暂停收发）</label>
    <label class="switch"><input type="checkbox" id="swDebug" onchange="setDebug(this)"> 详细日志（debug 级别，排查问题用）</label>
    <label class="switch"><input type="checkbox" id="swAuto" onchange="setAutostart(this)"> 开机自启</label>
  </section>

  <section class="card">
    <h2>日志 <button class="mini" onclick="loadLog()">刷新</button></h2>
    <pre id="log">加载中…</pre>
  </section>
</div>
<div class="toast" id="toast"></div>

<script>
const T = new URLSearchParams(location.search).get('t') || '';

function toast(msg){
  const el = document.getElementById('toast');
  el.textContent = msg; el.classList.add('show');
  clearTimeout(el._t); el._t = setTimeout(()=>el.classList.remove('show'), 1800);
}

async function api(path, body){
  const opt = {headers:{'X-Token':T}};
  if (body !== undefined){
    opt.method='POST';
    opt.headers['Content-Type']='application/json';
    opt.body=JSON.stringify(body);
  }
  const r = await fetch(path, opt);
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}

function fmtTime(ts){
  if (!ts) return '-';
  const d = new Date(ts*1000);
  return d.toLocaleTimeString('zh-CN',{hour12:false});
}

async function refresh(){
  try{
    const s = await api('/api/status');
    document.getElementById('hostname').textContent = s.name + ' · v' + s.version;
    document.getElementById('listen').textContent = s.bind;
    document.getElementById('peersCount').textContent = s.peers.length + ' 个';
    document.getElementById('counts').textContent = s.push_count + ' / ' + s.recv_count;
    document.getElementById('lastRecv').textContent = fmtTime(s.last_recv_at);
    const st = document.getElementById('state');
    st.textContent = s.paused ? '已暂停' : '运行中';
    st.className = 'badge' + (s.paused ? ' off' : '');
    document.getElementById('swSync').checked = !s.paused;
    document.getElementById('swDebug').checked = (s.log_level === 'debug');
    document.getElementById('swAuto').checked = !!s.autostart;

    const rows = document.getElementById('peerRows');
    if (!s.peers.length){
      rows.innerHTML = '<tr><td colspan="4" style="color:#6b7280">尚未绑定</td></tr>';
    } else {
      rows.innerHTML = s.peers.map(p=>{
        const ok = p.state && p.state.ok;
        const cls = p.state ? (ok?'ok':'bad') : 'unk';
        const txt = p.state ? (ok?'正常':(p.state.err||'失败')) : '未探测';
        return `<tr>
          <td>${p.name||''}</td>
          <td>${p.host}:${p.port}</td>
          <td><span class="dot ${cls}"></span>${txt}</td>
          <td><button class="mini" onclick="unbind('${p.key}')">删除</button></td>
        </tr>`;
      }).join('');
    }

    const link = await api('/api/mylink');
    document.getElementById('mylink').value = link.link;
  }catch(e){
    document.getElementById('state').textContent='连接失败';
    document.getElementById('state').className='badge off';
  }
}

async function copyLink(){
  const v = document.getElementById('mylink').value;
  try{ await navigator.clipboard.writeText(v); toast('已复制配对链接'); }
  catch(e){ document.getElementById('mylink').select(); document.execCommand('copy'); toast('已复制'); }
}

async function bindLink(){
  const v = document.getElementById('linkinput').value.trim();
  if (!v) return toast('请先粘贴配对链接');
  try{
    const r = await api('/api/bind', {link:v});
    toast(r.ok ? ('绑定成功：' + (r.peer||'')) : ('失败：' + (r.error||'')));
    if (r.ok) document.getElementById('linkinput').value='';
    refresh();
  }catch(e){ toast('绑定失败：' + e.message); }
}

async function bindManual(){
  const host = document.getElementById('h').value.trim();
  const port = document.getElementById('p').value.trim() || '8765';
  const token = document.getElementById('k').value.trim();
  const name = document.getElementById('n').value.trim();
  if (!host || !token) return toast('IP 和令牌必填');
  try{
    const r = await api('/api/bind', {host, port:parseInt(port,10), token, name});
    toast(r.ok ? '绑定成功' : ('失败：' + (r.error||'')));
    refresh();
  }catch(e){ toast('绑定失败：' + e.message); }
}

async function unbind(key){
  try{ await api('/api/unbind', {key}); toast('已删除'); refresh(); }
  catch(e){ toast('删除失败：' + e.message); }
}

async function setPause(el){
  try{ await api('/api/pause', {paused: !el.checked}); toast(el.checked?'已启用同步':'已暂停同步'); refresh(); }
  catch(e){ toast('操作失败'); }
}

async function setDebug(el){
  try{ await api('/api/config', {log_level: el.checked?'debug':'info'}); toast(el.checked?'已开启详细日志':'已恢复普通日志'); }
  catch(e){ toast('操作失败'); }
}

async function setAutostart(el){
  try{
    const r = await api('/api/config', {autostart: el.checked});
    toast(r.autostart ? '已设置开机自启' : '已取消开机自启');
  }catch(e){ toast('操作失败'); }
}

async function loadLog(){
  try{
    const r = await api('/api/log?lines=200');
    const box = document.getElementById('log');
    box.textContent = r.text || '(空)';
    box.scrollTop = box.scrollHeight;
  }catch(e){ document.getElementById('log').textContent = '读取日志失败: ' + e.message; }
}

refresh();
loadLog();
setInterval(refresh, 3000);
setInterval(loadLog, 3000);
</script>
</body>
</html>
"""
