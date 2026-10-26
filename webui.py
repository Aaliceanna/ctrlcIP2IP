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
  .netrow{display:grid;grid-template-columns:1.4fr .8fr;gap:10px;margin:10px 0}
  .netrow.one{grid-template-columns:1fr}
  .netrow label{display:block;color:var(--muted);font-size:12px}
  .netrow input{margin-top:3px}
  .tag{font-size:11px;font-weight:400;color:var(--muted);margin-left:auto;
    background:#f3f4f6;padding:2px 8px;border-radius:20px}
  .hint{color:var(--muted);font-size:12px;margin:8px 0 0}
  code{background:#f3f4f6;padding:2px 6px;border-radius:5px;font-size:12px;word-break:break-all}
  details summary{cursor:pointer;color:var(--accent);font-size:12px;margin-bottom:4px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line)}
  th{color:var(--muted);font-weight:500;font-size:12px}
  .dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px}
  .dot.ok{background:var(--ok)} .dot.bad{background:var(--bad)} .dot.unk{background:#c7cbd1}
  .mutual{font-size:11px;font-weight:400;color:var(--ok);background:#e8f5ec;
    border:1px solid #cbe8d4;padding:1px 6px;border-radius:20px;margin-left:4px;white-space:nowrap}
  .mutual.off{color:#b45309;background:#fef3c7;border-color:#fde68a}
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
    <h2>监听地址与端口 <span class="tag" id="netTag">-</span></h2>
    <div class="netrow">
      <label>监听地址（0.0.0.0 = 所有网卡）
        <input id="nb" list="ipList" placeholder="0.0.0.0">
      </label>
      <label>端口
        <input id="np" inputmode="numeric" placeholder="8765">
      </label>
    </div>
    <div class="netrow one">
      <label>对外公布的地址（写进配对链接；留空=自动取本机 IP）
        <input id="na" list="ipList" placeholder="自动">
      </label>
    </div>
    <datalist id="ipList"></datalist>
    <label class="switch"><input type="checkbox" id="swPortAuto" onchange="null"> 端口被占用时自动换一个空闲端口</label>
    <div class="row">
      <button class="primary" onclick="saveNet()">保存并应用</button>
      <button onclick="loadNet()">重新读取</button>
    </div>
    <p class="hint">保存后立即换端口，无需重启。改了端口或公布地址后配对链接会变，需要把新链接重新发给对方绑定一次。</p>
  </section>

  <section class="card">
    <h2>我的配对链接</h2>
    <div class="row">
      <input id="mylink" readonly>
      <button onclick="copyLink()">复制</button>
    </div>
    <p class="hint">在另一台机器上粘贴绑定即可。绑定是双向的：A 绑了 B，B 的对端列表里也会自动出现 A。</p>
  </section>

  <section class="card">
    <h2>绑定对端 <span class="tag">一次绑定，双向互通</span></h2>
    <div class="row">
      <input id="linkinput" placeholder="粘贴配对链接：clipshare://IP:端口/令牌?name=主机名">
      <button class="primary" onclick="bindLink()">绑定</button>
    </div>
    <label class="switch"><input type="checkbox" id="swReverse" checked> 双向绑定（同时让对方把本机加为对端）</label>
    <label class="switch"><input type="checkbox" id="swFwCheck" checked> 绑定时检测防火墙 / 连通性，被拦截时提示一键放行</label>
    <label class="switch"><input type="checkbox" id="swFwAuto"> 检测到被拦截时直接自动放行（不再询问）</label>
    <div id="bindMsg" class="hint"></div>
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
    <h2>防火墙与端口放行 <span class="tag" id="fwTag">检测中…</span></h2>
    <div id="fwMsg" class="hint">正在检测…</div>
    <div class="row" style="margin-top:10px">
      <button class="primary" id="btnFwOpen" onclick="fwOpen()">一键放行端口（临时）</button>
      <button id="btnFwClose" onclick="fwClose()">恢复（删除规则）</button>
      <button onclick="fwCheck()">重新检测</button>
    </div>
    <label class="switch"><input type="checkbox" id="swFwPersist"> 放行规则设为常驻（取消勾选＝关闭软件后自动恢复）</label>
    <p class="hint">
      临时规则：软件正常退出、闪退、被强杀后都会自动撤销，下次启动还会再扫一遍残留。<br>
      放行端口需要管理员权限，Windows 会弹一次 UAC，点“是”即可；Linux 需要 sudo / pkexec。
    </p>
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
  if (r.status === 401) throw new Error('未授权');
  let data = {};
  try { data = await r.json(); } catch(e) { throw new Error('HTTP ' + r.status); }
  if (!r.ok && data.ok !== false) throw new Error('HTTP ' + r.status);
  return data;
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
    document.getElementById('listen').textContent = s.listen
      ? (s.listen[0] + ':' + s.listen[1]) : s.bind;
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
        const mutual = p.mutual
          ? '<span class="mutual" title="对方也绑定了本机">双向</span>'
          : '<span class="mutual off" title="只有本机单向绑定，建议重新绑定一次">单向</span>';
        return `<tr>
          <td>${p.name||''} ${mutual}</td>
          <td>${p.host}:${p.port}</td>
          <td><span class="dot ${cls}"></span>${txt}</td>
          <td><button class="mini" onclick="unbind('${p.key}')">删除</button></td>
        </tr>`;
      }).join('');
    }

    const link = await api('/api/mylink');
    document.getElementById('mylink').value = link.link;

    const cfg = await api('/api/config');
    const c = cfg.config || {};
    const ae = document.activeElement;
    if (!ae || ['swFwCheck','swFwAuto'].indexOf(ae.id) < 0){
      document.getElementById('swFwCheck').checked = c.fw_autocheck !== false;
      document.getElementById('swFwAuto').checked = !!c.fw_autoopen;
    }
  }catch(e){
    document.getElementById('state').textContent='连接失败';
    document.getElementById('state').className='badge off';
  }
}

async function loadNet(){
  try{
    const n = await api('/api/network');
    const ae = document.activeElement;
    const editing = ae && ['nb','np','na'].indexOf(ae.id) >= 0;
    if (!editing){
      document.getElementById('nb').value = n.bind || '0.0.0.0';
      document.getElementById('np').value = n.port;
      document.getElementById('na').value = n.advertise || '';
      document.getElementById('swPortAuto').checked = !!n.port_auto;
    }
    const opts = ['0.0.0.0'].concat(n.candidates || []);
    document.getElementById('ipList').innerHTML =
      [...new Set(opts)].map(v=>'<option value="'+v+'">').join('');
    const tag = document.getElementById('netTag');
    tag.textContent = n.listen ? ('实际监听 ' + n.listen[0] + ':' + n.listen[1]) : '未监听';
  }catch(e){ /* 轮询失败时静默 */ }
}

async function saveNet(){
  const bind = document.getElementById('nb').value.trim() || '0.0.0.0';
  const port = parseInt(document.getElementById('np').value.trim(), 10);
  const advertise = document.getElementById('na').value.trim();
  const portAuto = document.getElementById('swPortAuto').checked;
  if (!port || port < 1 || port > 65535) return toast('端口要填 1-65535');
  try{
    const r = await api('/api/network', {bind, port, advertise, port_auto: portAuto});
    if (!r.ok){ toast('保存失败：' + (r.error || '未知错误')); loadNet(); return; }
    toast('已生效：' + r.bind + ':' + r.port);
    if (r.moved && r.ui_url){
      setTimeout(()=>{ location.href = r.ui_url; }, 700);
      return;
    }
    refresh(); loadNet();
  }catch(e){ toast('保存失败：' + e.message); }
}

async function copyLink(){
  const v = document.getElementById('mylink').value;
  try{ await navigator.clipboard.writeText(v); toast('已复制配对链接'); }
  catch(e){ document.getElementById('mylink').select(); document.execCommand('copy'); toast('已复制'); }
}

async function bindLink(){
  const v = document.getElementById('linkinput').value.trim();
  if (!v) return toast('请先粘贴配对链接');
  const reverse = document.getElementById('swReverse').checked;
  try{
    const r = await api('/api/bind', {link:v, reverse});
    showBindResult(r);
    if (r.ok) document.getElementById('linkinput').value='';
    refresh();
  }catch(e){ toast('绑定失败：' + e.message); }
}

function showBindResult(r){
  const box = document.getElementById('bindMsg');
  if (!box) return;
  let html = '';
  if (r.ok){
    const mutual = r.mutual || (r.reverse && r.reverse.ok);
    html += mutual
      ? '<b style="color:var(--ok)">双向绑定成功</b>：对方已把本机加为对端，两边都能互相推送。'
      : '<b style="color:var(--ok)">单向绑定成功</b>，但对方没能回绑本机'
        + (r.reverse && r.reverse.error ? '：' + r.reverse.error : '')
        + '（可让对方也绑一次，或确认对方在线）';
  } else {
    html += '<b style="color:var(--bad)">绑定失败</b>：' + (r.error || '未知原因');
  }
  if (r.firewall){
    const fw = r.firewall;
    html += '<br>防火墙体检：' + (fw.message || '');
    if (fw.state === 'blocked'){
      html += '<br><button class="mini" style="margin-top:6px" onclick="fwOpen()">一键放行 TCP ' + fw.port + '</button>';
    }
  }
  box.innerHTML = html;
}

async function bindManual(){
  const host = document.getElementById('h').value.trim();
  const port = document.getElementById('p').value.trim() || '8765';
  const token = document.getElementById('k').value.trim();
  const name = document.getElementById('n').value.trim();
  if (!host || !token) return toast('IP 和令牌必填');
  try{
    const r = await api('/api/bind', {host, port:parseInt(port,10), token, name,
      reverse: document.getElementById('swReverse').checked});
    showBindResult(r);
    refresh();
  }catch(e){ toast('绑定失败：' + e.message); }
}

async function unbind(key){
  if (!confirm('解除绑定？同时会通知对方把本机也删掉。')) return;
  try{
    const r = await api('/api/unbind', {key, notify:true});
    const n = r.notify ? ('，对方移除 ' + (r.notify.removed || 0) + ' 个') : '';
    toast('已删除' + n);
    refresh();
  }
  catch(e){ toast('删除失败：' + e.message); }
}

/* ---------- 防火墙 ---------- */
async function fwCheck(){
  try{
    const f = await api('/api/firewall');
    const tag = document.getElementById('fwTag');
    const msg = document.getElementById('fwMsg');
    const open = !!f.rule_exists;
    tag.textContent = (f.backend === 'none' ? '未检测到防火墙' : f.backend)
      + ' · TCP ' + f.port + (open ? ' 已放行' : (f.state === 'blocked' ? ' 疑似被拦截' : ' 正常'));
    tag.style.background = open ? '#e8f5ec' : (f.state === 'blocked' ? '#fdeaea' : '#f3f4f6');
    msg.innerHTML = (f.hints || []).map(h=>'· ' + h).join('<br>') || '· 正常';
    document.getElementById('btnFwOpen').textContent = open ? '已放行' : ('一键放行 TCP ' + f.port + '（临时）');
    document.getElementById('btnFwOpen').disabled = open;
    document.getElementById('btnFwClose').disabled = !open;
    document.getElementById('swFwPersist').checked = (f.mode === 'persist');
    if (f.need_admin) msg.innerHTML += '<br>· 放行需要管理员权限，点击后会弹一次 UAC';
    return f;
  }catch(e){
    document.getElementById('fwMsg').textContent = '检测失败：' + e.message;
  }
}

async function fwOpen(){
  const persist = document.getElementById('swFwPersist').checked;
  if (!confirm(persist
      ? '将添加一条常驻防火墙规则放行该端口，除非手动删除否则不会撤销。继续？'
      : '将临时放行该端口，ClipShare 关闭或闪退后自动恢复。继续？')) return;
  try{
    const r = await api('/api/firewall', {action:'open', mode: persist ? 'persist' : 'temp'});
    if (r.ok) toast('已放行 TCP ' + r.port);
    else toast(r.elevating ? '请在 UAC 弹窗中点“是”' : ('放行失败：' + (r.error || '')));
    setTimeout(fwCheck, 1500);
  }catch(e){ toast('操作失败：' + e.message); }
}

async function fwClose(){
  try{
    const r = await api('/api/firewall', {action:'close'});
    toast(r.ok ? ('已恢复，删除 ' + (r.closed || 0) + ' 条规则') : ('恢复失败：' + (r.error || '')));
    setTimeout(fwCheck, 800);
  }catch(e){ toast('操作失败：' + e.message); }
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
loadNet();
fwCheck();
document.getElementById('swFwCheck').onchange = (e)=>api('/api/config',{fw_autocheck:e.target.checked});
document.getElementById('swFwAuto').onchange = (e)=>api('/api/config',{fw_autoopen:e.target.checked});
setInterval(refresh, 3000);
setInterval(loadLog, 3000);
setInterval(loadNet, 6000);
setInterval(fwCheck, 6000);
</script>
</body>
</html>
"""
