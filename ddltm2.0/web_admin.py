# -*- coding: utf-8 -*-
"""
Admin backend for DDLTM2.0 Web.

This module is intentionally separated from web_app.py.  web_app.py only needs
one call to register_admin_routes(...); all admin UI/API logic lives here.

Admin rule:
    username == "admin"
"""
from __future__ import print_function

import datetime
import functools
import hashlib
import hmac
import json
import os
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time

from flask import jsonify, redirect, render_template_string, request, session, url_for


ADMIN_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DDLTM2.0 管理员后台</title>
  <style>
    :root{--bg:#081120;--panel:#101a2d;--card:#121d32;--line:#24324a;--text:#edf3ff;--muted:#91a0bd;--accent:#8b5cf6;--accent2:#06b6d4;--ok:#22c55e;--warn:#f59e0b;--danger:#ef4444;--shadow:0 14px 42px rgba(0,0,0,.32)}
    *{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 10% 0%,rgba(139,92,246,.25),transparent 25%),radial-gradient(circle at 92% 8%,rgba(6,182,212,.18),transparent 28%),var(--bg);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",Arial,sans-serif;color:var(--text)}
    .wrap{max-width:1680px;margin:0 auto;padding:26px}.top{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:18px}.top h1{font-size:30px;margin:0}.muted{color:var(--muted)}.nav{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px}.nav button,.btn{border:0;border-radius:14px;padding:10px 14px;color:white;background:linear-gradient(135deg,var(--accent),var(--accent2));cursor:pointer;font-weight:700}.nav button{background:#1e293b;border:1px solid var(--line)}.nav button.active{background:linear-gradient(135deg,rgba(139,92,246,.55),rgba(6,182,212,.38))}.btn.secondary{background:#1e293b;border:1px solid var(--line)}.btn.danger{background:linear-gradient(135deg,#ef4444,#f97316)}.btn.ok{background:linear-gradient(135deg,#16a34a,#06b6d4)}.btn.warn{background:linear-gradient(135deg,#f59e0b,#ef4444)}
    .grid{display:grid;gap:16px}.cards{grid-template-columns:repeat(4,minmax(160px,1fr))}.two{grid-template-columns:1fr 1fr}.card{background:linear-gradient(180deg,rgba(255,255,255,.075),rgba(255,255,255,.045));border:1px solid var(--line);border-radius:22px;padding:18px;box-shadow:var(--shadow);margin-bottom:16px}.metric{font-size:30px;font-weight:800}.label{color:var(--muted);font-size:13px;margin-top:4px}.section{display:none}.section.active{display:block}.toolbar{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0}.input,select,textarea{background:#0b1324;border:1px solid var(--line);border-radius:14px;color:var(--text);padding:10px 12px;outline:none}textarea{width:100%;min-height:340px;resize:vertical;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;line-height:1.45}.field{display:grid;gap:7px}.field label{font-size:13px;color:#c9d5f0}.form-grid{display:grid;grid-template-columns:repeat(3,minmax(170px,1fr));gap:12px}.span3{grid-column:span 3}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:18px}table{width:100%;border-collapse:collapse;min-width:820px}th,td{padding:11px 12px;border-bottom:1px solid rgba(255,255,255,.07);text-align:left;vertical-align:top}th{background:#111c30;color:#cbd5e1;font-size:12px}td{font-size:13px;color:#e7eefc}.pill{display:inline-flex;align-items:center;gap:6px;padding:4px 9px;border-radius:999px;font-size:12px;border:1px solid rgba(255,255,255,.11);background:rgba(255,255,255,.06)}.okc{color:#86efac}.warnc{color:#fbbf24}.badc{color:#fca5a5}.logbox{white-space:pre-wrap;background:#050914;border:1px solid var(--line);border-radius:18px;padding:14px;min-height:100px;max-height:320px;overflow:auto;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;color:#d7e2f7;line-height:1.45}.note{padding:12px 14px;border-radius:16px;background:rgba(245,158,11,.12);border:1px solid rgba(245,158,11,.30);color:#fde68a;line-height:1.7}.danger-note{padding:12px 14px;border-radius:16px;background:rgba(239,68,68,.12);border:1px solid rgba(239,68,68,.30);color:#fecaca;line-height:1.7}.toast{position:fixed;right:20px;bottom:20px;z-index:99;display:grid;gap:10px}.toast div{padding:12px 14px;border-radius:14px;background:#172037;border:1px solid var(--line);box-shadow:var(--shadow)}a{color:#c4b5fd;text-decoration:none}@media(max-width:980px){.cards,.two,.form-grid{grid-template-columns:1fr}.span3{grid-column:span 1}.wrap{padding:18px}}
  </style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div><h1>管理员后台</h1><div class="muted">仅用户名为 admin 的账号可访问 · <span id="versionText">等待加载...</span></div></div>
    <div class="toolbar"><a class="btn secondary" href="/">返回控制台</a><button class="btn secondary" onclick="refreshAdminAll()">刷新后台数据</button></div>
  </div>
  <div class="nav">
    <button data-tab="overview" class="active">总览</button>
    <button data-tab="nodes">节点管理</button>
    <button data-tab="config">conf.json 设置</button>
    <button data-tab="users">用户管理</button>
    <button data-tab="notes">说明与风险</button>
  </div>

  <section id="overview" class="section active">
    <div class="grid cards">
      <div class="card"><div class="metric" id="mUsers">0</div><div class="label">Web 用户</div></div>
      <div class="card"><div class="metric" id="mNodes">0</div><div class="label">配置节点</div></div>
      <div class="card"><div class="metric" id="mOnline">0</div><div class="label">在线节点</div></div>
      <div class="card"><div class="metric" id="mForced">0</div><div class="label">手动下线/重连</div></div>
    </div>
    <div class="grid two" style="margin-top:16px">
      <div class="card"><h3>运行状态</h3><div id="runtimeBox" class="logbox">等待加载...</div></div>
      <div class="card"><h3>配置文件位置</h3><div id="pathBox" class="logbox">等待加载...</div></div>
    </div>
  </section>

  <section id="nodes" class="section">
    <div class="card">
      <h3>节点列表</h3>
      <div class="toolbar"><button class="btn secondary" onclick="loadSlavers()">刷新节点</button><button class="btn warn" onclick="forceOfflineSelected()">强制下线并停止任务</button><button class="btn secondary" onclick="reconnectStaleSelected()">断开旧连接并重连</button><button class="btn ok" onclick="reconnectSelected()">重新连接节点</button><button class="btn danger" onclick="deleteSelectedSlaver()">删除选中节点</button></div>
      <div class="table-wrap"><table id="slaverTable"></table></div>
    </div>
    <div class="card">
      <h3>新增 / 更新节点</h3>
      <div class="form-grid">
        <div class="field"><label>节点名称 name</label><input class="input" id="nodeName" placeholder="node01"></div>
        <div class="field"><label>IP 地址</label><input class="input" id="nodeIp" placeholder="192.168.1.10"></div>
        <div class="field"><label>SSH 用户名 user_name</label><input class="input" id="nodeUser" placeholder="ddltm"></div>
        <div class="field"><label>GPU 数量 gpu_count</label><input class="input" id="nodeGpuCount" type="number" min="0" value="1"></div>
        <div class="field span3"><label>GPU 型号列表 gpu_info，用英文逗号分隔</label><input class="input" id="nodeGpuInfo" placeholder="RTX4090,RTX4090"></div>
      </div>
      <div class="toolbar"><button class="btn ok" onclick="upsertSlaver()">保存节点</button><button class="btn secondary" onclick="clearNodeForm()">清空表单</button></div>
      <div class="note">保存后会写入 <code>gpu_slaver_info.json</code>。新增节点通常会在 monitor 下一轮刷新后被发现；删除节点会同时清理 monitor 运行态、旧 SSH 连接和监控缓存，不再需要为了删除节点而重启 master。修改已有节点的 SSH/IP/GPU 数量时，仍建议重启 master。</div>
    </div>
  </section>

  <section id="config" class="section">
    <div class="card">
      <h3>快速设置</h3>
      <div class="form-grid">
        <div class="field"><label>root</label><input class="input" id="quickRoot"></div>
        <div class="field"><label>visible_folders，逗号分隔</label><input class="input" id="quickVisible"></div>
        <div class="field"><label>Web 端口</label><input class="input" id="quickWebPort" type="number"></div>
        <div class="field"><label>允许注册 allow_register</label><select class="input" id="quickAllowRegister"><option value="true">true</option><option value="false">false</option></select></div>
        <div class="field"><label>命令行 terminal_enabled</label><select class="input" id="quickTerminal"><option value="true">true</option><option value="false">false</option></select></div>
        <div class="field"><label>最大上传 MB</label><input class="input" id="quickUpload" type="number" min="1"></div>
      </div>
      <div class="toolbar"><button class="btn ok" onclick="applyQuickConfig()">应用到下方 JSON</button><button class="btn secondary" onclick="loadConfig()">重新读取 conf.json</button><button class="btn danger" onclick="saveConfig()">保存 conf.json</button></div>
      <div class="danger-note">修改 <code>socket_info</code>、<code>web_info.host/port</code>、<code>root</code>、<code>slaver_file</code> 等关键项后，通常需要重启 master 才能完全生效。后台会自动备份旧配置。</div>
    </div>
    <div class="card">
      <h3>conf.json 原始编辑</h3>
      <textarea id="confEditor"></textarea>
    </div>
  </section>

  <section id="users" class="section">
    <div class="card">
      <h3>用户列表</h3>
      <div class="toolbar"><button class="btn secondary" onclick="loadUsers()">刷新用户</button><button class="btn danger" onclick="deleteSelectedUser()">删除选中用户</button></div>
      <div class="table-wrap"><table id="userTable"></table></div>
    </div>
    <div class="grid two">
      <div class="card">
        <h3>创建用户</h3>
        <div class="field"><label>用户名</label><input class="input" id="newUser"></div>
        <div class="field"><label>密码</label><input class="input" id="newPass" type="password"></div>
        <div class="toolbar"><button class="btn ok" onclick="createUser()">创建</button></div>
      </div>
      <div class="card">
        <h3>重置密码</h3>
        <div class="field"><label>目标用户名</label><input class="input" id="resetUser"></div>
        <div class="field"><label>新密码</label><input class="input" id="resetPass" type="password"></div>
        <div class="toolbar"><button class="btn danger" onclick="resetPassword()">重置密码</button></div>
      </div>
    </div>
  </section>

  <section id="notes" class="section">
    <div class="card"><h3>后台功能说明</h3>
      <ul style="line-height:1.9">
        <li><strong>强制下线并停止任务：</strong>真正断开 master 与节点的当前连接，并把该节点上运行/已分配的任务标记为“错误, 节点掉线”；节点会保持手动下线，不会自动重连。</li>
        <li><strong>断开旧连接并重连：</strong>用于节点异常断电/重启后 master 仍误判在线、旧 SSH 监控会话卡死的场景；只清理旧连接和在线状态并触发 monitor 重新连接，不主动处理任务状态。</li>
        <li><strong>重新连接节点：</strong>用于恢复被“强制下线并停止任务”的节点；后台会解除手动下线状态、关闭旧 SSH 监控会话、清理在线状态和调度缓存，并让 monitor 线程立刻重新连接。</li>
        <li><strong>添加节点：</strong>写入节点配置文件；monitor 通常会在下一轮刷新后尝试连接新节点。</li>
        <li><strong>删除节点：</strong>写入配置并从运行时列表移除；已有 monitor 线程无法被精确安全停止，因此建议重启 master。</li>
        <li><strong>conf.json：</strong>保存时会生成 <code>.bak-时间戳</code> 备份。部分参数可运行时更新，监听端口等参数需要重启。</li>
        <li><strong>用户管理：</strong>当前管理员判定规则为用户名等于 <code>admin</code>，所以不要删除或改坏 admin 账号。</li>
      </ul>
    </div>
  </section>

  <div class="card"><h3>操作结果</h3><div class="logbox" id="out">等待操作...</div></div>
</div>
<div class="toast" id="toast"></div>
<script>
let ADMIN={overview:null,slavers:[],users:[],config:null,selectedSlaver:null,selectedUser:null};
const $=id=>document.getElementById(id);
function toast(msg){const d=document.createElement('div');d.textContent=msg;$('toast').appendChild(d);setTimeout(()=>d.remove(),3600)}
function out(x){$('out').textContent=typeof x==='string'?x:JSON.stringify(x,null,2)}
function esc(s){return (s??'').toString().replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]))}
async function api(url,opt={}){opt.headers=Object.assign({'Accept':'application/json'},opt.headers||{});if(opt.body&&!(opt.body instanceof FormData)){opt.headers['Content-Type']='application/json';opt.body=JSON.stringify(opt.body)}const r=await fetch(url,opt);if(r.status===401){location.href='/login';return}const j=await r.json();if(!r.ok||j.ok===false)throw new Error(j.error||r.statusText);return j.data}
document.querySelectorAll('.nav button').forEach(b=>b.onclick=()=>{document.querySelectorAll('.section').forEach(s=>s.classList.remove('active'));document.querySelectorAll('.nav button').forEach(x=>x.classList.remove('active'));$(b.dataset.tab).classList.add('active');b.classList.add('active')});
async function refreshAdminAll(){try{await Promise.all([loadOverview(),loadSlavers(),loadUsers(),loadConfig()]);toast('后台数据已刷新')}catch(e){toast(e.message);out('刷新失败：'+e.message)}}
async function loadOverview(){const d=await api('/api/admin/overview');ADMIN.overview=d;$('mUsers').textContent=d.users_count;$('mNodes').textContent=d.slavers_count;$('mOnline').textContent=d.online.length;$('mForced').textContent=(d.manual_offline||[]).length+' / '+Object.keys(d.reconnect_info||{}).length;$('versionText').textContent=d.version;$('runtimeBox').textContent=JSON.stringify(d.runtime,null,2);$('pathBox').textContent=JSON.stringify(d.paths,null,2);if(ADMIN.slavers&&ADMIN.slavers.length)renderSlavers()}
async function loadSlavers(){const d=await api('/api/admin/slavers');ADMIN.slavers=d.slavers||[];renderSlavers();}
function renderSlavers(){let html='<thead><tr><th></th><th>名称</th><th>IP</th><th>用户</th><th>GPU数量</th><th>GPU型号</th><th>在线</th><th>最近操作</th></tr></thead><tbody>';const online=(ADMIN.overview&&ADMIN.overview.online)||[];const reconnectInfo=(ADMIN.overview&&ADMIN.overview.reconnect_info)||{};const forceInfo=(ADMIN.overview&&ADMIN.overview.force_offline_info)||{};const manual=(ADMIN.overview&&ADMIN.overview.manual_offline)||[];ADMIN.slavers.forEach(s=>{const rt=reconnectInfo[s.name];const ft=forceInfo[s.name];let op='否';if(manual.includes(s.name)){op='手动下线 '+(ft||'');}else if(ft){op='强制下线 '+ft;}else if(rt){op='重连 '+rt;}html+=`<tr onclick="selectSlaver('${esc(s.name)}')"><td><input type="radio" name="slaverSel" ${ADMIN.selectedSlaver===s.name?'checked':''}></td><td>${esc(s.name)}</td><td>${esc(s.ip)}</td><td>${esc(s.user_name)}</td><td>${esc(s.gpu_count)}</td><td>${esc((s.gpu_info||[]).join(','))}</td><td>${online.includes(s.name)?'<span class="pill okc">在线</span>':'<span class="pill badc">离线</span>'}</td><td>${op!=='否'?'<span class="pill warnc">'+esc(op)+'</span>':'否'}</td></tr>`});html+='</tbody>';$('slaverTable').innerHTML=html}
function selectSlaver(name){ADMIN.selectedSlaver=name;const s=ADMIN.slavers.find(x=>x.name===name);if(s){$('nodeName').value=s.name;$('nodeIp').value=s.ip;$('nodeUser').value=s.user_name;$('nodeGpuCount').value=s.gpu_count;$('nodeGpuInfo').value=(s.gpu_info||[]).join(',')}renderSlavers()}
function clearNodeForm(){['nodeName','nodeIp','nodeUser','nodeGpuInfo'].forEach(id=>$(id).value='');$('nodeGpuCount').value='1'}
async function upsertSlaver(){const node={name:$('nodeName').value.trim(),ip:$('nodeIp').value.trim(),user_name:$('nodeUser').value.trim(),gpu_count:parseInt($('nodeGpuCount').value||'0'),gpu_info:$('nodeGpuInfo').value.split(',').map(x=>x.trim()).filter(Boolean)};try{const d=await api('/api/admin/slavers/upsert',{method:'POST',body:{node}});out(d);toast('节点已保存');await loadOverview();await loadSlavers()}catch(e){toast(e.message);out(e.message)}}
async function deleteSelectedSlaver(){if(!ADMIN.selectedSlaver){toast('请先选择节点');return}if(!confirm('确认删除节点 '+ADMIN.selectedSlaver+' ?\n该操作会从 gpu_slaver_info.json 和 monitor 运行态中移除节点；如果该节点有正在运行的任务，会按节点掉线处理。'))return;try{const d=await api('/api/admin/slavers/delete',{method:'POST',body:{name:ADMIN.selectedSlaver}});out(d);ADMIN.selectedSlaver=null;await loadOverview();await loadSlavers();toast('节点已删除并已清理 monitor 运行态')}catch(e){toast(e.message);out(e.message)}}
async function forceOfflineSelected(){if(!ADMIN.selectedSlaver){toast('请先选择节点');return}if(!confirm('确认强制下线 '+ADMIN.selectedSlaver+' ?\n该节点上正在运行/已分配的任务会被停止，并标记为“错误, 节点掉线”。节点会保持手动下线，直到点击“重新连接节点”。'))return;try{const d=await api('/api/admin/slavers/force-offline',{method:'POST',body:{name:ADMIN.selectedSlaver}});out(d);await loadOverview();renderSlavers();toast('已强制下线节点并处理运行任务；需要时可点重新连接节点')}catch(e){toast(e.message);out(e.message)}}
async function reconnectStaleSelected(){if(!ADMIN.selectedSlaver){toast('请先选择节点');return}if(!confirm('确认断开 '+ADMIN.selectedSlaver+' 的旧监控连接并立即重连？\n该操作不会主动停止任务，也不会把任务标记为节点掉线，适合节点异常断电/重启后 master 误判在线的场景。'))return;try{const d=await api('/api/admin/slavers/reconnect-stale',{method:'POST',body:{name:ADMIN.selectedSlaver}});out(d);await loadOverview();renderSlavers();toast('已断开旧连接并请求重连')}catch(e){toast(e.message);out(e.message)}}
async function reconnectSelected(){if(!ADMIN.selectedSlaver){toast('请先选择节点');return}try{const d=await api('/api/admin/slavers/reconnect',{method:'POST',body:{name:ADMIN.selectedSlaver}});out(d);await loadOverview();renderSlavers();toast('已请求重新连接节点')}catch(e){toast(e.message);out(e.message)}}
async function loadConfig(){const d=await api('/api/admin/config');ADMIN.config=d.config;$('confEditor').value=JSON.stringify(d.config,null,2);fillQuickConfig(d.config)}
function fillQuickConfig(c){$('quickRoot').value=c.root||'';$('quickVisible').value=(c.visible_folders||[]).join(',');const w=c.web_info||{};$('quickWebPort').value=w.port||'';$('quickAllowRegister').value=String(!!w.allow_register);$('quickTerminal').value=String(!!w.terminal_enabled);$('quickUpload').value=w.max_upload_mb||512}
function applyQuickConfig(){let c=JSON.parse($('confEditor').value);c.root=$('quickRoot').value.trim();c.visible_folders=$('quickVisible').value.split(',').map(x=>x.trim()).filter(Boolean);c.web_info=c.web_info||{};c.web_info.port=parseInt($('quickWebPort').value||c.web_info.port||8080);c.web_info.allow_register=$('quickAllowRegister').value==='true';c.web_info.terminal_enabled=$('quickTerminal').value==='true';c.web_info.max_upload_mb=parseInt($('quickUpload').value||512);$('confEditor').value=JSON.stringify(c,null,2);out('快速设置已应用到下方 JSON，尚未保存。')}
async function saveConfig(){let c;try{c=JSON.parse($('confEditor').value)}catch(e){toast('JSON 格式错误：'+e.message);return}if(!confirm('确认保存 conf.json？后台会自动备份旧配置。'))return;try{const d=await api('/api/admin/config/save',{method:'POST',body:{config:c}});out(d);toast('conf.json 已保存');await loadOverview()}catch(e){toast(e.message);out(e.message)}}
async function loadUsers(){const d=await api('/api/admin/users');ADMIN.users=d.users||[];renderUsers()}
function renderUsers(){let html='<thead><tr><th></th><th>ID</th><th>用户名</th><th>是否管理员</th><th>创建时间</th></tr></thead><tbody>';ADMIN.users.forEach(u=>{html+=`<tr onclick="selectUser('${esc(u.username)}')"><td><input type="radio" name="userSel" ${ADMIN.selectedUser===u.username?'checked':''}></td><td>${esc(u.id)}</td><td>${esc(u.username)}</td><td>${u.is_admin?'<span class="pill okc">admin</span>':'否'}</td><td>${esc(u.created_at||'')}</td></tr>`});html+='</tbody>';$('userTable').innerHTML=html}
function selectUser(username){ADMIN.selectedUser=username;$('resetUser').value=username;renderUsers()}
async function createUser(){const username=$('newUser').value.trim(), password=$('newPass').value;if(!username||!password){toast('用户名和密码不能为空');return}try{const d=await api('/api/admin/users/create',{method:'POST',body:{username,password}});out(d);$('newPass').value='';await loadUsers();await loadOverview();toast('用户已创建')}catch(e){toast(e.message);out(e.message)}}
async function resetPassword(){const username=$('resetUser').value.trim(), password=$('resetPass').value;if(!username||!password){toast('用户名和新密码不能为空');return}if(!confirm('确认重置 '+username+' 的密码？'))return;try{const d=await api('/api/admin/users/reset-password',{method:'POST',body:{username,password}});out(d);$('resetPass').value='';toast('密码已重置')}catch(e){toast(e.message);out(e.message)}}
async function deleteSelectedUser(){if(!ADMIN.selectedUser){toast('请先选择用户');return}if(!confirm('确认删除用户 '+ADMIN.selectedUser+' ?'))return;try{const d=await api('/api/admin/users/delete',{method:'POST',body:{username:ADMIN.selectedUser}});out(d);ADMIN.selectedUser=null;await loadUsers();await loadOverview();toast('用户已删除')}catch(e){toast(e.message);out(e.message)}}
refreshAdminAll();
</script>
</body>
</html>
"""


def register_admin_routes(app, master, users, require_login, json_ok, json_err, log_func=None):
    """Register admin page and APIs into the existing Flask app."""
    if not hasattr(master, "_web_forced_offline"):
        master._web_forced_offline = set()
    if not hasattr(master, "_web_admin_guard_started"):
        master._web_admin_guard_started = False

    def is_admin():
        return session.get("user") == "admin"

    def admin_required(fn):
        @functools.wraps(fn)
        @require_login
        def wrapper(*args, **kwargs):
            if not is_admin():
                if request.path.startswith("/api/"):
                    return json_err("需要 admin 用户权限", 403)
                return redirect(url_for("index"))
            return fn(*args, **kwargs)
        return wrapper

    def now_tag():
        return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")

    def conf_path():
        # The original master starts from master1.2 and loads config("conf.json").
        return os.path.abspath(os.environ.get("DDLT_CONF_PATH", "conf.json"))

    def slaver_path():
        p = master.config.config.get("slaver_file", "./gpu_slaver_info.json")
        if not os.path.isabs(p):
            p = os.path.join(os.path.dirname(conf_path()), p)
        return os.path.abspath(p)

    def atomic_write_json(path, obj):
        parent = os.path.dirname(os.path.abspath(path))
        if parent and not os.path.exists(parent):
            os.makedirs(parent)
        if os.path.exists(path):
            backup = path + ".bak-" + now_tag()
            shutil.copy2(path, backup)
        else:
            backup = None
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=parent or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(obj, f, indent=4, ensure_ascii=False)
                f.write("\n")
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass
        return backup

    def validate_slaver(node):
        if not isinstance(node, dict):
            raise ValueError("节点信息必须是 JSON 对象")
        for k in ("name", "ip", "user_name"):
            if not str(node.get(k, "")).strip():
                raise ValueError("节点字段缺失或为空: " + k)
            node[k] = str(node[k]).strip()
        try:
            node["gpu_count"] = int(node.get("gpu_count", 0))
        except Exception:
            raise ValueError("gpu_count 必须是整数")
        if node["gpu_count"] < 0:
            raise ValueError("gpu_count 不能小于 0")
        gpu_info = node.get("gpu_info", [])
        if isinstance(gpu_info, str):
            gpu_info = [x.strip() for x in gpu_info.split(",") if x.strip()]
        if not isinstance(gpu_info, list):
            raise ValueError("gpu_info 必须是列表或逗号分隔字符串")
        node["gpu_info"] = [str(x).strip() for x in gpu_info]
        if len(node["gpu_info"]) != node["gpu_count"]:
            # Keep it permissive but normalize to avoid scheduler index errors.
            if len(node["gpu_info"]) < node["gpu_count"]:
                node["gpu_info"].extend(["Unknown"] * (node["gpu_count"] - len(node["gpu_info"])))
            else:
                node["gpu_info"] = node["gpu_info"][:node["gpu_count"]]
        return node

    def load_slavers_from_disk():
        p = slaver_path()
        if not os.path.exists(p):
            return []
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError("slaver_file 必须是节点列表 JSON")
        return [validate_slaver(dict(x)) for x in data]

    def save_slavers_to_disk(slavers):
        clean = [validate_slaver(dict(x)) for x in slavers]
        names = [x["name"] for x in clean]
        if len(names) != len(set(names)):
            raise ValueError("节点名称不能重复")
        backup = atomic_write_json(slaver_path(), clean)
        master.config.slaver_info = clean
        try:
            master.monitor.slaver_info = master.config.slaver_info
        except Exception:
            pass
        try:
            master.task_ctrl.config.slaver_info = master.config.slaver_info
        except Exception:
            pass
        return backup, clean

    def list_users():
        with users._conn() as conn:
            rows = conn.execute("SELECT id, username, created_at FROM users ORDER BY id ASC").fetchall()
        return [
            {"id": r[0], "username": r[1], "created_at": r[2], "is_admin": r[1] == "admin"}
            for r in rows
        ]

    def set_user_password(username, password):
        username = (username or "").strip()
        if not username or not password:
            raise ValueError("用户名和密码不能为空")
        if len(password) < 6:
            raise ValueError("密码至少需要 6 位")
        salt = secrets.token_hex(16)
        password_hash = users._hash(password, salt)
        with users.lock:
            with users._conn() as conn:
                cur = conn.execute(
                    "UPDATE users SET password_hash=?, salt=? WHERE username=?",
                    (password_hash, salt, username),
                )
                if cur.rowcount <= 0:
                    raise ValueError("用户不存在")

    def apply_config_runtime(new_conf):
        master.config.config = new_conf
        master.config.visible_folders = list(new_conf.get("visible_folders", []))
        master.config.socket_info = dict(new_conf.get("socket_info", getattr(master.config, "socket_info", {})))
        try:
            from global_var import visible_folder
            visible_folder[:] = list(master.config.visible_folders)
        except Exception:
            pass
        return {
            "runtime_updated": ["config", "visible_folders", "socket_info"],
            "restart_recommended_for": ["web_info.host/port", "socket_info ports", "root", "slaver_file", "terminal_enabled"],
        }

    def forced_guard_loop():
        while True:
            try:
                forced = set(getattr(master, "_web_forced_offline", set()))
                if forced:
                    try:
                        master.online_slaver[:] = [x for x in master.online_slaver if x not in forced]
                    except Exception:
                        pass
                    try:
                        master.slaver_state[:] = [x for x in master.slaver_state if x.get("name") not in forced]
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(1.0)

    # v3 no longer uses background suppression for force-offline.  The old
    # forced_guard_loop is intentionally not started.  Force-offline is now a
    # one-shot disconnect + task offline_error operation.
    master._web_admin_guard_started = True

    @app.route("/admin")
    @admin_required
    def admin_page():
        return render_template_string(ADMIN_HTML)

    @app.route("/api/admin/overview")
    @admin_required
    def api_admin_overview():
        from global_var import wait_task, exec_task, hist_task, task_lock
        task_lock.acquire()
        try:
            runtime = {
                "wait_tasks": len(wait_task),
                "exec_tasks": len(exec_task),
                "hist_tasks": len(hist_task),
                "root": master.config.config.get("root"),
                "visible_folders": master.config.visible_folders,
                "terminal_enabled_config": bool(master.config.config.get("web_info", {}).get("terminal_enabled", False)),
            }
        finally:
            task_lock.release()
        users_list = list_users()
        return json_ok({
            "version": "admin-v5-stale-reconnect-button",
            "users_count": len(users_list),
            "slavers_count": len(master.config.slaver_info),
            "online": list(master.online_slaver),
            "forced_offline": [],
            "manual_offline": list(getattr(getattr(master, "monitor", None), "manual_offline_nodes", set()) or []),
            "reconnect_info": dict(getattr(getattr(master, "monitor", None), "last_reconnect_at", {}) or {}),
            "force_offline_info": dict(getattr(getattr(master, "monitor", None), "last_force_offline_at", {}) or {}),
            "runtime": runtime,
            "paths": {"conf_json": conf_path(), "slaver_file": slaver_path(), "user_db": os.path.abspath(users.path)},
        })

    @app.route("/api/admin/users")
    @admin_required
    def api_admin_users():
        return json_ok({"users": list_users()})

    @app.route("/api/admin/users/create", methods=["POST"])
    @admin_required
    def api_admin_users_create():
        try:
            data = request.get_json(force=True)
            users.create_user(data.get("username"), data.get("password"))
            return json_ok({"created": data.get("username")})
        except Exception as e:
            return json_err(e)

    @app.route("/api/admin/users/reset-password", methods=["POST"])
    @admin_required
    def api_admin_users_reset_password():
        try:
            data = request.get_json(force=True)
            set_user_password(data.get("username"), data.get("password"))
            return json_ok({"reset": data.get("username")})
        except Exception as e:
            return json_err(e)

    @app.route("/api/admin/users/delete", methods=["POST"])
    @admin_required
    def api_admin_users_delete():
        try:
            username = (request.get_json(force=True).get("username") or "").strip()
            if username == "admin":
                raise ValueError("不能删除 admin 用户")
            if not username:
                raise ValueError("缺少用户名")
            with users.lock:
                with users._conn() as conn:
                    cur = conn.execute("DELETE FROM users WHERE username=?", (username,))
                    if cur.rowcount <= 0:
                        raise ValueError("用户不存在")
            return json_ok({"deleted": username})
        except Exception as e:
            return json_err(e)

    @app.route("/api/admin/config")
    @admin_required
    def api_admin_config():
        return json_ok({"config": master.config.config, "path": conf_path()})

    @app.route("/api/admin/config/save", methods=["POST"])
    @admin_required
    def api_admin_config_save():
        try:
            data = request.get_json(force=True)
            conf = data.get("config")
            if not isinstance(conf, dict):
                raise ValueError("config 必须是 JSON 对象")
            if "visible_folders" not in conf or not isinstance(conf["visible_folders"], list):
                raise ValueError("visible_folders 必须是列表")
            if "root" not in conf:
                raise ValueError("缺少 root")
            if "socket_info" not in conf or not isinstance(conf["socket_info"], dict):
                raise ValueError("socket_info 必须是对象")
            if "web_info" not in conf or not isinstance(conf["web_info"], dict):
                raise ValueError("web_info 必须是对象")
            backup = atomic_write_json(conf_path(), conf)
            runtime = apply_config_runtime(conf)
            if log_func:
                try:
                    log_func("Web管理员保存了 conf.json", master.config.config.get("server_log_path", "server_log.log"))
                except Exception:
                    pass
            return json_ok({"saved": conf_path(), "backup": backup, "runtime": runtime})
        except Exception as e:
            return json_err(e)

    @app.route("/api/admin/slavers")
    @admin_required
    def api_admin_slavers():
        return json_ok({
            "slavers": list(master.config.slaver_info),
            "online": list(master.online_slaver),
            "forced_offline": [],
            "manual_offline": list(getattr(getattr(master, "monitor", None), "manual_offline_nodes", set()) or []),
            "reconnect_info": dict(getattr(getattr(master, "monitor", None), "last_reconnect_at", {}) or {}),
            "force_offline_info": dict(getattr(getattr(master, "monitor", None), "last_force_offline_at", {}) or {}),
            "path": slaver_path(),
        })

    @app.route("/api/admin/slavers/upsert", methods=["POST"])
    @admin_required
    def api_admin_slavers_upsert():
        try:
            node = validate_slaver(dict(request.get_json(force=True).get("node") or {}))
            slavers = load_slavers_from_disk()
            found = False
            for i, s in enumerate(slavers):
                if s["name"] == node["name"]:
                    slavers[i] = node
                    found = True
                    break
            if not found:
                slavers.append(node)
            backup, clean = save_slavers_to_disk(slavers)
            mon = getattr(master, "monitor", None)
            if mon is not None and hasattr(mon, "enable_node"):
                try:
                    mon.enable_node(node["name"])
                except Exception:
                    pass
            return json_ok({"saved": node["name"], "created": not found, "backup": backup, "restart_recommended": True, "slavers_count": len(clean)})
        except Exception as e:
            return json_err(e)

    @app.route("/api/admin/slavers/delete", methods=["POST"])
    @admin_required
    def api_admin_slavers_delete():
        try:
            name = (request.get_json(force=True).get("name") or "").strip()
            if not name:
                raise ValueError("缺少节点名称")
            slavers = load_slavers_from_disk()
            new_slavers = [s for s in slavers if s["name"] != name]
            if len(new_slavers) == len(slavers):
                raise ValueError("节点不存在")
            # Stop tasks first so tasks on this node do not remain stuck in exec.
            task_result = None
            try:
                tc = getattr(master, "task_ctrl", None)
                if tc is not None and hasattr(tc, "force_node_offline"):
                    task_result = tc.force_node_offline(name, ret_code=-1)
            except Exception as e:
                task_result = {"error": str(e)}
            backup, clean = save_slavers_to_disk(new_slavers)
            monitor_result = None
            mon = getattr(master, "monitor", None)
            if mon is not None and hasattr(mon, "remove_node"):
                monitor_result = mon.remove_node(name, reason="admin delete node")
            else:
                try:
                    master.online_slaver[:] = [x for x in master.online_slaver if x != name]
                    master.slaver_state[:] = [x for x in master.slaver_state if x.get("name") != name]
                except Exception:
                    pass
                monitor_result = {"removed_from_monitor": False, "note": "当前 monitor.py 不支持 remove_node；已仅清理在线状态，建议更新 monitor.py 或重启 master。"}
            return json_ok({"deleted": name, "backup": backup, "restart_recommended": False, "slavers_count": len(clean), "tasks": task_result, "monitor": monitor_result})
        except Exception as e:
            return json_err(e)

    @app.route("/api/admin/slavers/force-offline", methods=["POST"])
    @admin_required
    def api_admin_slavers_force_offline():
        try:
            name = (request.get_json(force=True).get("name") or "").strip()
            if not name:
                raise ValueError("缺少节点名称")
            task_result = None
            try:
                tc = getattr(master, "task_ctrl", None)
                if tc is not None and hasattr(tc, "force_node_offline"):
                    task_result = tc.force_node_offline(name, ret_code=-1)
                else:
                    task_result = {"supported": False, "note": "当前 task_ctrl.py 不支持 force_node_offline；只能断开监控连接，无法自动把运行任务置为 offline_error。"}
            except Exception as e:
                task_result = {"error": str(e)}
            mon = getattr(master, "monitor", None)
            if mon is not None and hasattr(mon, "disconnect_node"):
                monitor_result = mon.disconnect_node(name, reason="admin force offline")
            else:
                try:
                    master.online_slaver[:] = [x for x in master.online_slaver if x != name]
                    master.slaver_state[:] = [x for x in master.slaver_state if x.get("name") != name]
                except Exception:
                    pass
                monitor_result = {"name": name, "closed_monitor_session": False, "note": "当前 monitor.py 不支持 disconnect_node；已仅清理在线状态，建议更新 monitor.py。"}
            return json_ok({"node": name, "tasks": task_result, "monitor": monitor_result, "suppressed": False})
        except Exception as e:
            return json_err(e)

    @app.route("/api/admin/slavers/reconnect-stale", methods=["POST"])
    @admin_required
    def api_admin_slavers_reconnect_stale():
        try:
            name = (request.get_json(force=True).get("name") or "").strip()
            if not name:
                raise ValueError("缺少节点名称")
            mon = getattr(master, "monitor", None)
            if mon is not None and hasattr(mon, "request_reconnect_node"):
                result = mon.request_reconnect_node(name, reason="admin stale connection reconnect")
                if isinstance(result, dict):
                    result["mode"] = "stale_connection_reconnect"
                    result["note"] = "已断开旧SSH监控连接、清理旧在线/调度状态，并请求monitor立即重连；不会主动停止任务，也不会把任务标记为offline_error。"
            else:
                try:
                    master.online_slaver[:] = [x for x in master.online_slaver if x != name]
                    master.slaver_state[:] = [x for x in master.slaver_state if x.get("name") != name]
                except Exception:
                    pass
                result = {"name": name, "reconnect_requested": False, "mode": "stale_connection_reconnect", "note": "当前 monitor.py 不支持 request_reconnect_node；已仅清理在线状态，建议更新 monitor.py 或重启 master。"}
            return json_ok(result)
        except Exception as e:
            return json_err(e)

    @app.route("/api/admin/slavers/reconnect", methods=["POST"])
    @admin_required
    def api_admin_slavers_reconnect():
        try:
            name = (request.get_json(force=True).get("name") or "").strip()
            if not name:
                raise ValueError("缺少节点名称")
            mon = getattr(master, "monitor", None)
            if mon is not None and hasattr(mon, "request_reconnect_node"):
                result = mon.request_reconnect_node(name, reason="admin reconnect")
            else:
                try:
                    master.online_slaver[:] = [x for x in master.online_slaver if x != name]
                    master.slaver_state[:] = [x for x in master.slaver_state if x.get("name") != name]
                except Exception:
                    pass
                result = {"name": name, "reconnect_requested": False, "manual_offline_cleared": False, "note": "当前 monitor.py 不支持重新连接节点；已仅清理在线状态，建议更新 monitor.py 或重启 master。"}
            return json_ok(result)
        except Exception as e:
            return json_err(e)

    @app.route("/api/admin/slavers/release-offline", methods=["POST"])
    @admin_required
    def api_admin_slavers_release_offline():
        try:
            name = (request.get_json(force=True).get("name") or "").strip()
            if not name:
                raise ValueError("缺少节点名称")
            mon = getattr(master, "monitor", None)
            try:
                if mon is not None and hasattr(mon, "reconnect_requested"):
                    mon.reconnect_requested.discard(name)
                if mon is not None and hasattr(mon, "deleted_nodes"):
                    # 不自动解除删除节点标记；删除节点后应通过“新增/更新节点”重新添加。
                    pass
            except Exception:
                pass
            return json_ok({"released": name, "note": "当前版本不再使用后台压制式强制下线；这里仅清理重连请求标记。"})
        except Exception as e:
            return json_err(e)

    return app
