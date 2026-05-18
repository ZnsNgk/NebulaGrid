# -*- coding: utf-8 -*-
"""
Web front-end for the distributed deep learning task manager.

It is designed to be embedded into master/main.py and to reuse the existing
master-side queues/state, so the old PyQt client and the new web UI can coexist.
"""
from __future__ import print_function

import base64
import datetime
import functools
import hashlib
import hmac
import io
import json
import os
import queue
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import zipfile

from flask import (
    Flask, Response, abort, flash, jsonify, redirect, render_template_string,
    request, send_file, session, stream_with_context, url_for
)

from global_var import wait_queue, wait_task, exec_task, hist_task, task_lock, stop_task, stop_lock
from logger import log


STATE_TEXT = {
    "wait": "等待中",
    "exec": "运行中",
    "pexec": "准备中",
    "accp": "完成",
    "err": "错误",
    "offline_error": "错误, 节点掉线",
    "term": "中止",
}


LOGIN_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ title }}</title>
  <style>
    :root{--bg:#0b1020;--card:rgba(255,255,255,.08);--line:rgba(255,255,255,.12);--text:#eef2ff;--muted:#a6b0cf;--accent:#7c3aed;--accent2:#06b6d4;--danger:#ef4444;--ok:#22c55e}
    *{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(circle at 20% 20%,rgba(124,58,237,.35),transparent 35%),radial-gradient(circle at 80% 10%,rgba(6,182,212,.25),transparent 30%),var(--bg);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",Arial,sans-serif;color:var(--text);display:grid;place-items:center;padding:24px}
    .card{width:min(460px,100%);padding:34px;border:1px solid var(--line);background:linear-gradient(180deg,rgba(255,255,255,.10),rgba(255,255,255,.06));backdrop-filter:blur(18px);border-radius:28px;box-shadow:0 24px 80px rgba(0,0,0,.35)}
    h1{margin:0 0 8px;font-size:30px}.sub{color:var(--muted);margin:0 0 24px;line-height:1.6}.row{display:grid;gap:8px;margin:14px 0}label{color:#dbe4ff;font-size:14px}input{width:100%;padding:13px 14px;border-radius:14px;border:1px solid var(--line);background:rgba(8,13,32,.74);color:var(--text);outline:none}input:focus{border-color:rgba(124,58,237,.75);box-shadow:0 0 0 3px rgba(124,58,237,.18)}button{width:100%;margin-top:18px;border:0;border-radius:16px;padding:13px 16px;color:white;background:linear-gradient(135deg,var(--accent),var(--accent2));font-weight:700;cursor:pointer}.msg{padding:12px 14px;border-radius:14px;background:rgba(239,68,68,.14);border:1px solid rgba(239,68,68,.28);color:#fecaca;margin:12px 0}.ok{background:rgba(34,197,94,.14);border-color:rgba(34,197,94,.28);color:#bbf7d0}.links{margin-top:18px;color:var(--muted);font-size:14px;text-align:center}.links a{color:#c4b5fd;text-decoration:none}
  </style>
</head>
<body>
  <form class="card" method="post">
    <h1>{{ title }}</h1>
    <p class="sub">分布式深度学习任务管理系统 Web 控制台</p>
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% for cat, msg in messages %}<div class="msg {{ 'ok' if cat == 'ok' else '' }}">{{ msg }}</div>{% endfor %}
    {% endwith %}
    <div class="row"><label>用户名</label><input name="username" autocomplete="username" required></div>
    <div class="row"><label>密码</label><input name="password" type="password" autocomplete="current-password" required></div>
    {% if mode == 'register' %}<div class="row"><label>确认密码</label><input name="password2" type="password" required></div>{% endif %}
    <button>{{ button }}</button>
    <div class="links">
      {% if mode == 'login' and register_enabled %}<a href="{{ url_for('register') }}">注册新账号</a>{% endif %}
      {% if mode == 'register' %}<a href="{{ url_for('login') }}">已有账号，返回登录</a>{% endif %}
    </div>
  </form>
</body>
</html>
"""


APP_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DDLTM2.0 Web</title>
  <style>
    :root{--bg:#081120;--panel:#101a2d;--panel2:#0f172a;--card:#121d32;--line:#24324a;--text:#edf3ff;--muted:#91a0bd;--accent:#8b5cf6;--accent2:#06b6d4;--ok:#22c55e;--warn:#f59e0b;--danger:#ef4444;--blue:#38bdf8;--shadow:0 14px 42px rgba(0,0,0,.32)}
    *{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 10% 0%,rgba(139,92,246,.25),transparent 25%),radial-gradient(circle at 92% 8%,rgba(6,182,212,.18),transparent 28%),var(--bg);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",Arial,sans-serif;color:var(--text)}
    .layout{display:grid;grid-template-columns:270px 1fr;min-height:100vh}.side{position:sticky;top:0;height:100vh;padding:24px 18px;border-right:1px solid var(--line);background:rgba(8,17,32,.82);backdrop-filter:blur(18px)}.brand{padding:10px 12px 22px}.brand h1{font-size:22px;margin:0}.brand p{color:var(--muted);font-size:13px;margin:8px 0 0;line-height:1.5}.nav{display:grid;gap:8px}.nav button,.nav a{border:0;text-align:left;border-radius:16px;padding:13px 14px;color:#dce7ff;background:transparent;cursor:pointer;font-weight:650;text-decoration:none;display:block}.nav button:hover,.nav button.active,.nav a:hover{background:linear-gradient(135deg,rgba(139,92,246,.26),rgba(6,182,212,.18));box-shadow:inset 0 0 0 1px rgba(255,255,255,.08)}.logout{position:absolute;bottom:18px;left:18px;right:18px;color:#cbd5e1;text-decoration:none;padding:12px 14px;border:1px solid var(--line);border-radius:16px;text-align:center;background:rgba(255,255,255,.04)}
    main{padding:26px;max-width:1680px;width:100%}.top{display:flex;justify-content:space-between;align-items:center;gap:18px;margin-bottom:20px}.top h2{font-size:30px;margin:0}.top .meta{color:var(--muted);font-size:13px}.grid{display:grid;gap:16px}.cards{grid-template-columns:repeat(4,minmax(160px,1fr))}.card{background:linear-gradient(180deg,rgba(255,255,255,.075),rgba(255,255,255,.045));border:1px solid var(--line);border-radius:22px;padding:18px;box-shadow:var(--shadow)}.metric{font-size:30px;font-weight:800}.label{color:var(--muted);font-size:13px;margin-top:4px}.section{display:none}.section.active{display:block}.toolbar{display:flex;flex-wrap:wrap;gap:10px;margin:14px 0}.btn{border:0;border-radius:14px;padding:10px 14px;color:white;background:linear-gradient(135deg,var(--accent),var(--accent2));cursor:pointer;font-weight:700}.btn.secondary{background:#1e293b;border:1px solid var(--line)}.btn.danger{background:linear-gradient(135deg,#ef4444,#f97316)}.btn.ok{background:linear-gradient(135deg,#16a34a,#06b6d4)}.btn:disabled{opacity:.45;cursor:not-allowed}.input,select,textarea{background:#0b1324;border:1px solid var(--line);border-radius:14px;color:var(--text);padding:10px 12px;outline:none}textarea{min-height:96px;resize:vertical;font-family:ui-monospace,SFMono-Regular,Consolas,monospace}.path-pick{display:grid;grid-template-columns:1fr auto;gap:8px}.btn.small{padding:9px 12px;white-space:nowrap}.picker-list{max-height:420px;overflow:auto;border:1px solid var(--line);border-radius:16px;background:#091225;padding:8px}.picker-item{width:100%;display:flex;align-items:center;justify-content:space-between;gap:12px;border:0;background:transparent;color:var(--text);padding:11px 12px;border-radius:12px;text-align:left;cursor:pointer}.picker-item:hover{background:rgba(139,92,246,.16)}.form-grid{display:grid;grid-template-columns:repeat(3,minmax(180px,1fr));gap:12px}.field{display:grid;gap:7px}.field label{font-size:13px;color:#c9d5f0}.span2{grid-column:span 2}.span3{grid-column:span 3}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:18px}table{width:100%;border-collapse:collapse;min-width:920px}th,td{padding:11px 12px;border-bottom:1px solid rgba(255,255,255,.07);text-align:left;vertical-align:top}th{position:sticky;top:0;background:#111c30;color:#cbd5e1;font-size:12px;z-index:1}td{font-size:13px;color:#e7eefc}.pill{display:inline-flex;align-items:center;gap:6px;padding:4px 9px;border-radius:999px;font-size:12px;border:1px solid rgba(255,255,255,.11);background:rgba(255,255,255,.06)}.okc{color:#86efac}.warnc{color:#fbbf24}.badc{color:#fca5a5}.muted{color:var(--muted)}.node-grid{grid-template-columns:repeat(auto-fill,minmax(290px,1fr))}.node h3{margin:0 0 10px}.gpu{margin-top:10px;padding:10px;border-radius:14px;background:rgba(15,23,42,.7);border:1px solid rgba(255,255,255,.07)}.bar{height:8px;background:#0b1324;border-radius:999px;overflow:hidden;margin-top:7px}.bar span{display:block;height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2));width:0}.split{display:grid;grid-template-columns:330px 1fr;gap:16px}.file-list{max-height:640px;overflow:auto}.file-item{display:flex;gap:10px;align-items:center;justify-content:space-between;width:100%;border:0;background:transparent;color:var(--text);padding:10px;border-radius:12px;text-align:left;cursor:pointer}.file-item:hover,.file-item.active{background:rgba(139,92,246,.14)}.editor{min-height:560px;width:100%;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;line-height:1.5}.logbox{white-space:pre;background:#050914;border:1px solid var(--line);border-radius:18px;padding:14px;min-height:420px;max-height:680px;overflow:auto;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;color:#d7e2f7;line-height:1.45;tab-size:4;word-break:normal}.log-status{display:flex;align-items:center;gap:10px;margin:8px 0 10px;padding:9px 12px;border:1px solid var(--line);border-radius:14px;background:#0b1324;color:#cbd5e1;font-size:13px;font-family:ui-monospace,SFMono-Regular,Consolas,monospace}.log-status .dot{width:8px;height:8px;border-radius:50%;background:var(--muted);display:inline-block}.log-status.running .dot{background:var(--ok)}.log-status.error .dot{background:var(--danger)}.toast{position:fixed;right:20px;bottom:20px;z-index:99;display:grid;gap:10px}.toast div{padding:12px 14px;border-radius:14px;background:#172037;border:1px solid var(--line);box-shadow:var(--shadow)}dialog{border:1px solid var(--line);border-radius:22px;background:#0f172a;color:var(--text);padding:0;width:min(900px,94vw);box-shadow:0 25px 90px rgba(0,0,0,.55)}dialog::backdrop{background:rgba(0,0,0,.55)}.modal-head{display:flex;justify-content:space-between;align-items:center;padding:18px 20px;border-bottom:1px solid var(--line)}.modal-body{padding:20px}.x{background:transparent;border:0;color:var(--text);font-size:22px;cursor:pointer}.checkboxes{display:flex;flex-wrap:wrap;gap:8px}.check{padding:8px 10px;border:1px solid var(--line);border-radius:12px;background:#0b1324}.check input{margin-right:6px}.manual-wrap{display:grid;grid-template-columns:260px 1fr;gap:18px}.manual-toc{position:sticky;top:20px;align-self:start;max-height:calc(100vh - 80px);overflow:auto}.manual-toc a{display:block;color:#cbd5e1;text-decoration:none;padding:8px 10px;border-radius:10px;font-size:13px}.manual-toc a:hover{background:rgba(139,92,246,.15);color:#fff}.manual-content{display:grid;gap:16px}.manual-content h3{margin:0 0 10px;font-size:22px}.manual-content h4{margin:16px 0 8px;font-size:17px;color:#dbeafe}.manual-content p,.manual-content li{line-height:1.78;color:#d7e2f7}.manual-content ul,.manual-content ol{margin-top:8px}.manual-content code{background:#071021;border:1px solid var(--line);padding:2px 6px;border-radius:7px;color:#bae6fd}.manual-content pre{white-space:pre-wrap;background:#050914;border:1px solid var(--line);border-radius:14px;padding:12px;overflow:auto;color:#d7e2f7}.manual-note{border-left:4px solid var(--accent2);background:rgba(6,182,212,.08);padding:12px 14px;border-radius:12px;margin:10px 0}.manual-warn{border-left:4px solid var(--warn);background:rgba(245,158,11,.08);padding:12px 14px;border-radius:12px;margin:10px 0}.manual-danger{border-left:4px solid var(--danger);background:rgba(239,68,68,.08);padding:12px 14px;border-radius:12px;margin:10px 0}.manual-kbd{display:inline-block;padding:1px 6px;border:1px solid var(--line);border-bottom-width:2px;border-radius:6px;background:#0b1324;color:#e5e7eb;font-size:12px}@media(max-width:980px){.layout{grid-template-columns:1fr}.side{position:relative;height:auto}.logout{position:static;margin-top:14px;display:block}.cards{grid-template-columns:repeat(2,1fr)}.split,.form-grid,.manual-wrap{grid-template-columns:1fr}.span2,.span3{grid-column:span 1}main{padding:18px}}
  </style>
</head>
<body>
<div class="layout">
  <aside class="side">
    <div class="brand"><h1>DDLTM2.0</h1><p>网页端控制台 · 任务 / 节点 / 文件 / 环境 / 日志</p></div>
    <div class="nav">
      <button data-tab="dashboard" class="active">总览与节点</button>
      <button data-tab="tasks">训练任务</button>
      <button data-tab="files">文件管理</button>
      <button data-tab="envs">环境管理</button>
      <button data-tab="logs">日志查看</button>
      <button data-tab="terminal">命令行</button>
      <button data-tab="manual">使用手册</button>
      {% if is_admin %}<a class="admin-nav admin-link-v3" href="/admin">管理员后台</a>{% endif %}
    </div>
    <a class="logout" href="{{ url_for('logout') }}">退出登录：{{ username }}</a>
  </aside>
  <main>
    <div class="top"><div><h2 id="pageTitle">总览与节点</h2><div class="meta" id="lastUpdate">等待刷新...</div></div><button class="btn secondary" onclick="refreshAll()">立即刷新</button></div>

    <section id="dashboard" class="section active">
      <div class="grid cards">
        <div class="card"><div class="metric" id="mNodes">0</div><div class="label">在线节点</div></div>
        <div class="card"><div class="metric" id="mWait">0</div><div class="label">等待任务</div></div>
        <div class="card"><div class="metric" id="mExec">0</div><div class="label">运行任务</div></div>
        <div class="card"><div class="metric" id="mHist">0</div><div class="label">历史任务</div></div>
      </div>
      <div class="grid node-grid" id="nodes" style="margin-top:16px"></div>
    </section>

    <section id="tasks" class="section">
      <div class="toolbar">
        <button class="btn" onclick="openTaskModal('add')">添加任务</button>
        <button class="btn secondary" onclick="openMultiModal()">批量添加</button>
        <button class="btn secondary" onclick="editSelectedTask()">修改选中任务</button>
        <button class="btn danger" onclick="deleteSelectedTask()">删除选中任务</button>
        <button class="btn danger" onclick="stopSelectedTask()">中止运行任务</button>
        <button class="btn ok" onclick="resubmitSelectedTask()">重新提交</button>
        <button class="btn secondary" id="loadAllHistBtn" onclick="loadAllHistory()">显示所有历史任务</button>
      </div>
      <div class="toolbar">
        <select id="taskTableSelect" class="input" onchange="renderTaskTable()"><option value="wait">等待区</option><option value="exec">执行区</option><option value="hist">历史任务</option></select>
        <input id="taskFilter" class="input" placeholder="按任务ID/命令/节点过滤" oninput="renderTaskTable()">
        <button class="btn secondary" onclick="showSelectedLog()">查看任务日志</button>
      </div>
      <div class="table-wrap"><table id="taskTable"></table></div>
    </section>

    <section id="files" class="section">
      <div class="split">
        <div class="card file-list">
          <div class="toolbar"><button class="btn secondary" onclick="loadFiles('/')">根目录</button><button class="btn secondary" onclick="goParent()">上级</button><button class="btn secondary" onclick="loadFiles(currPath)">刷新</button></div>
          <div class="muted" id="currPath">/</div>
          <div id="fileList" style="margin-top:12px"></div>
          <hr style="border-color:var(--line);border-style:solid none none;margin:16px 0">
          <div class="toolbar"><button class="btn secondary" onclick="makeFileOp('mkdir')">新建文件夹</button><button class="btn secondary" onclick="makeFileOp('touch')">新建文件</button><button class="btn secondary" onclick="renameSelected()">重命名</button><button class="btn secondary" onclick="copyMoveSelected('cp')">复制到</button><button class="btn secondary" onclick="copyMoveSelected('mv')">移动到</button><button class="btn danger" onclick="deleteSelectedFile()">删除</button></div>
          <div class="toolbar"><input type="file" id="uploadInput" multiple><button class="btn" onclick="uploadFiles()">上传到当前目录</button><button class="btn secondary" onclick="downloadSelected()">下载选中</button></div>
        </div>
        <div class="card">
          <div class="toolbar"><span class="pill" id="openedFile">未打开文件</span><button class="btn ok" onclick="saveOpenedFile()">保存</button></div>
          <textarea class="editor" id="editor" placeholder="选择左侧文本文件后可在这里预览/编辑"></textarea>
        </div>
      </div>
    </section>

    <section id="envs" class="section">
      <div class="card">
        <div class="toolbar"><button class="btn secondary" onclick="loadEnvs()">刷新环境列表</button><select id="envSelect" class="input"></select><button class="btn" onclick="envAction('test_env')">测试环境</button><button class="btn secondary" onclick="envAction('py_v')">Python版本</button><button class="btn secondary" onclick="envAction('cuda_pt')">PyTorch CUDA</button><button class="btn secondary" onclick="envAction('cuda_tf')">TensorFlow CUDA</button><button class="btn secondary" onclick="envAction('pkgs')">包列表</button></div>
        <div class="logbox" id="envResult">请选择环境并执行操作。</div>
      </div>
    </section>

    <section id="logs" class="section">
      <div class="toolbar"><button class="btn secondary" onclick="loadServerLog()">刷新服务端日志</button><input id="logTaskId" class="input" placeholder="任务ID"><button class="btn secondary" onclick="loadTaskLogByInput()">查看任务日志</button></div>
      <div class="log-status" id="logStatus"><span class="dot"></span><span id="logStatusText">日志刷新模块：polling，等待选择任务。</span></div>
      <div class="logbox" id="logBox">日志内容会显示在这里。</div>
    </section>

    <section id="terminal" class="section">
      <div class="card">
        <p class="muted">命令行功能默认可在配置中关闭。开启后会在 master 机器本地执行命令，等价于高权限管理入口，请只部署在可信网络内。</p>
        <textarea id="shellCmd" class="editor" style="min-height:120px" placeholder="例如：pwd\nls -lah"></textarea>
        <div class="toolbar"><button class="btn danger" onclick="runShell()">执行命令</button></div>
        <div class="logbox" id="shellOut"></div>
      </div>
    </section>
    <section id="manual" class="section">
      <div class="manual-wrap">
        <div class="card manual-toc">
          <strong>使用手册目录</strong>
          <a href="#manual-overview">1. DDLTM系统是做什么的</a>
          <a href="#manual-login">2. 登录、注册与安全</a>
          <a href="#manual-dashboard">3. 总览与节点监控</a>
          <a href="#manual-task-basic">4. 添加单个任务</a>
          <a href="#manual-task-batch">5. 批量添加任务</a>
          <a href="#manual-task-manage">6. 修改、删除、中止与重提</a>
          <a href="#manual-logs">7. 任务日志与服务端日志</a>
          <a href="#manual-files">8. 文件管理与在线编辑</a>
          <a href="#manual-envs">9. 环境管理</a>
          <a href="#manual-terminal">10. 命令行功能</a>
          <a href="#manual-workflow">11. 推荐使用流程</a>
          <a href="#manual-faq">12. 常见问题排查</a>
        </div>
        <div class="manual-content">
          <div class="card" id="manual-overview">
            <h3>1. DDLTM系统是做什么的</h3>
            <p>这个 Web 控制台用于管理分布式深度学习任务。它嵌入在 master 端运行，功能目标是尽量与原 PyQt 客户端保持一致：查看节点状态、提交训练任务、管理任务队列、查看日志、浏览 master 可见文件、测试 conda 环境，并在需要时执行 master 本地命令。</p>
            <div class="manual-note"><strong>核心概念：</strong>master 负责调度任务；slave / 节点负责实际运行训练；任务通常由“环境 + 项目路径 + 执行命令 + GPU需求 + 节点限制”等信息组成。</div>
            <ul>
              <li><strong>等待任务：</strong>已经提交，但尚未被分配到节点运行。</li>
              <li><strong>运行任务：</strong>已经被 master 分配到某个节点，正在执行或准备执行。</li>
              <li><strong>历史任务：</strong>已经完成、失败、节点掉线失败或被中止的任务。</li>
              <li><strong>项目路径：</strong>训练命令执行时所在的工作目录，通常是代码仓库目录。</li>
              <li><strong>执行命令：</strong>真正执行的训练命令，例如 <code>python train.py --config xxx.yaml</code>。</li>
            </ul>
          </div>

          <div class="card" id="manual-login">
            <h3>2. 登录、注册与安全</h3>
            <p>第一次使用时，如果系统开启了注册功能，可以在登录页点击“注册新账号”。注册成功后回到登录页输入用户名和密码进入系统。</p>
            <ol>
              <li>打开浏览器访问 Web 控制台地址，例如 <code>http://master-ip:8080</code>。</li>
              <li>如果还没有账号，点击“注册新账号”。</li>
              <li>输入用户名、密码和确认密码。密码至少 6 位。</li>
              <li>注册完成后回到登录页登录。</li>
            </ol>
            <div class="manual-warn"><strong>建议：</strong>管理员创建好账号后，应在 <code>conf.json</code> 中关闭 <code>allow_register</code>，避免无关人员自行注册。</div>
            <pre>"web_info": {
  "allow_register": false,
  "terminal_enabled": false
}</pre>
            <p>如果控制台部署在公网、校园网公网入口或多人共享网络中，建议额外使用防火墙、VPN 或 Nginx 访问控制。不要把带命令行权限的 Web 控制台直接暴露到不可信网络。</p>
          </div>

          <div class="card" id="manual-dashboard">
            <h3>3. 总览与节点监控</h3>
            <p>“总览与节点”页面用于查看当前集群的大致状态，包括在线节点数量、等待任务数、运行任务数、历史任务数，以及每个节点的 CPU、内存、GPU、显存等信息。</p>
            <h4>节点状态怎么看</h4>
            <ul>
              <li><strong>在线：</strong>master 能连接到该节点，节点可参与任务调度。</li>
              <li><strong>离线：</strong>master 当前没有检测到该节点在线，任务不会被正常分配到它。</li>
              <li><strong>No data：</strong>该节点尚未返回完整监控数据，或 Web 端暂时没有缓存到最新状态。</li>
              <li><strong>GPU 使用率 / 显存：</strong>用于判断节点是否空闲、是否适合分配新任务。</li>
            </ul>
            <div class="manual-note">页面会定时刷新状态；也可以点击右上角“立即刷新”。如果 PyQt 客户端能显示节点信息而 Web 端不能显示，优先确认当前运行的 <code>web_app.py</code> 是否为最新版，并访问 <code>/api/web-version</code> 检查版本。</div>
          </div>

          <div class="card" id="manual-task-basic">
            <h3>4. 添加单个任务</h3>
            <p>进入“训练任务”页面，点击“添加任务”即可打开任务提交窗口。</p>
            <h4>字段说明</h4>
            <ul>
              <li><strong>Conda环境：</strong>选择任务运行时要激活的 conda 环境。环境列表来自 master/节点配置。</li>
              <li><strong>项目路径：</strong>点击“选择文件夹”，从 master 可见目录中选择代码所在目录。不要手动输入路径，避免路径拼写错误。</li>
              <li><strong>指定节点：</strong>可以选择某个节点运行，也可以按系统提供的选项让 master 调度。</li>
              <li><strong>所需GPU数量：</strong>任务需要占用的 GPU 数量。普通单卡训练通常填 <code>1</code>；CPU 任务可填 <code>0</code>。</li>
              <li><strong>前驱任务：</strong>用于设置任务依赖。只有前驱任务完成后，当前任务才会进入可执行状态。</li>
              <li><strong>紧急任务：</strong>让任务更优先进入调度队列，适合少量需要插队的任务。</li>
              <li><strong>复用GPU：</strong>允许任务复用已有 GPU 资源。是否适合开启取决于你的训练脚本和显存占用情况。</li>
              <li><strong>GPU型号需求：</strong>用于限制任务只能运行在指定 GPU 型号上。</li>
              <li><strong>执行命令：</strong>在项目路径下执行的命令，例如 <code>python train.py --config configs/a.yaml</code>。</li>
            </ul>
            <h4>单任务提交示例</h4>
            <pre>项目路径：/data/my_project/
Conda环境：torch201
所需GPU数量：1
执行命令：python train.py --config configs/vit.yaml --batch-size 128</pre>
            <div class="manual-warn"><strong>注意：</strong>执行命令中的相对路径，是相对于“项目路径”的。如果你的命令依赖数据集、配置文件或输出目录，请先确认这些路径在节点上可访问。</div>
          </div>

          <div class="card" id="manual-task-batch">
            <h3>5. 批量添加任务</h3>
            <p>“批量添加”适合一次性提交多条训练命令。批量任务会共用同一组环境、项目路径、节点/GPU限制等参数，但每一行命令会生成一个独立任务。</p>
            <h4>命令输入规则</h4>
            <ul>
              <li>每行一条命令。</li>
              <li>空行会自动忽略。</li>
              <li>以 <code>#</code> 开头的行会被当作注释忽略。</li>
              <li>行内 <code>#</code> 之后的内容会被视为注释。命令本身需要 <code>#</code> 时请谨慎。</li>
            </ul>
            <pre>python train.py --config configs/a.yaml
python train.py --config configs/b.yaml
# 下面这一行暂时不运行
# python train.py --config configs/c.yaml
python train.py --config configs/d.yaml  # 这个注释会被忽略</pre>
            <div class="manual-note">批量添加后，建议先观察“等待任务”表，确认任务数量、命令、环境和项目路径是否正确，再让它们进入运行。</div>
          </div>

          <div class="card" id="manual-task-manage">
            <h3>6. 修改、删除、中止与重新提交任务</h3>
            <p>训练任务页面中有等待区、运行区和历史区。点击任务行可选中任务，然后使用顶部按钮进行操作。</p>
            <h4>常用操作</h4>
            <ul>
              <li><strong>修改选中任务：</strong>主要用于修改等待区或历史区任务的配置。已经运行中的任务通常不建议修改。</li>
              <li><strong>删除选中任务：</strong>从任务列表中删除该任务。对于已经运行的任务，通常应先中止再删除。</li>
              <li><strong>中止运行任务：</strong>向 master 提交中止请求，master 会通知对应节点停止任务。</li>
              <li><strong>重新提交：</strong>把历史任务重新加入队列，常用于失败后修正代码/参数再运行。</li>
              <li><strong>删除当前任务及后继任务：</strong>如果任务之间存在依赖关系，可以删除某任务以及依赖它的后续任务。</li>
            </ul>
            <h4>状态说明</h4>
            <ul>
              <li><code>wait</code> / 等待中：任务在等待调度。</li>
              <li><code>pexec</code> / 准备中：任务正在准备执行。</li>
              <li><code>exec</code> / 运行中：任务正在节点上运行。</li>
              <li><code>accp</code> / 完成：任务正常完成。</li>
              <li><code>err</code> / 错误：任务运行失败。</li>
              <li><code>offline_error</code>：运行过程中节点掉线或连接异常。</li>
              <li><code>term</code> / 中止：任务被用户或系统中止。</li>
            </ul>
          </div>

          <div class="card" id="manual-logs">
            <h3>7. 任务日志与服务端日志</h3>
            <p>日志查看页面可以查看 master 服务端日志，也可以根据任务 ID 查看训练任务日志。任务日志区域会每 2 秒自动刷新一次；当任务完成、错误、节点掉线或被中止后，会自动停止刷新。</p>
            <h4>查看任务日志</h4>
            <ol>
              <li>进入“训练任务”页面，选中一个任务并点击“查看日志”；或进入“日志查看”页面手动输入任务 ID。</li>
              <li>日志窗口顶部会显示刷新状态，例如 <code>polling-v5</code> 和最近刷新时间。</li>
              <li>如果训练脚本使用 <code>tqdm</code>，页面会尽量按终端覆盖行的方式处理 <code>\r</code>，减少进度条刷屏。</li>
              <li>任务结束后，页面会提示已自动停止刷新。</li>
            </ol>
            <h4>查看服务端日志</h4>
            <p>点击“刷新服务端日志”可以查看 master 端日志，用于排查节点连接、调度、文件操作、接口错误等问题。</p>
            <div class="manual-note">如果日志不刷新，请先访问 <code>/api/web-version</code> 确认当前版本；再打开浏览器开发者工具查看是否每 2 秒请求一次 <code>/api/task/log/&lt;task_id&gt;</code>。</div>
          </div>

          <div class="card" id="manual-files">
            <h3>8. 文件管理与在线编辑</h3>
            <p>文件管理页面用于浏览 master 配置允许访问的目录，支持上传、下载、预览、编辑、保存、新建、重命名、复制、移动和删除。</p>
            <h4>基本用法</h4>
            <ol>
              <li>点击“根目录”或可见目录进入文件树。</li>
              <li>点击文件夹进入下级目录，点击“上级”返回上一级。</li>
              <li>点击文本文件可以在右侧编辑器中预览/编辑。</li>
              <li>修改后点击“保存”。</li>
              <li>上传文件时，文件会上传到当前目录。</li>
            </ol>
            <div class="manual-warn"><strong>注意：</strong>文件管理直接操作 master 可见文件系统。删除、移动、覆盖保存都可能影响训练任务，请谨慎操作。建议重要代码通过 Git 管理。</div>
            <p>可见目录通常由 <code>conf.json</code> 中的 <code>visible_folders</code> 控制。Web 端不会任意开放整个服务器文件系统。</p>
          </div>

          <div class="card" id="manual-envs">
            <h3>9. 环境管理</h3>
            <p>环境管理页面用于检查 conda 环境和深度学习框架是否可用。</p>
            <ul>
              <li><strong>刷新环境列表：</strong>重新读取可用环境。</li>
              <li><strong>测试环境：</strong>检查环境是否能正常激活和执行基本命令。</li>
              <li><strong>Python版本：</strong>查看该环境中的 Python 版本。</li>
              <li><strong>PyTorch CUDA：</strong>检查 PyTorch 是否能识别 CUDA。</li>
              <li><strong>TensorFlow CUDA：</strong>检查 TensorFlow 是否能识别 GPU/CUDA。</li>
              <li><strong>包列表：</strong>查看该环境中的已安装包。</li>
            </ul>
            <div class="manual-note">这些按钮只显示返回结果中的 <code>msg</code> 字段，避免把完整 JSON 暴露给普通用户。若结果为空或报错，请查看服务端日志。</div>
          </div>

          <div class="card" id="manual-terminal">
            <h3>10. 命令行功能</h3>
            <p>命令行页面用于在 master 本机执行 shell 命令。这个功能风险很高，默认建议关闭。</p>
            <div class="manual-danger"><strong>高风险：</strong>命令行等价于给 Web 用户提供 master 的命令执行入口。只有在可信内网、账号受控、明确需要远程维护时才建议开启。</div>
            <pre>"web_info": {
  "terminal_enabled": false
}</pre>
            <p>如果开启，请避免让普通用户执行删除、格式化、杀进程、修改系统配置等危险命令。</p>
          </div>

          <div class="card" id="manual-workflow">
            <h3>11. 推荐使用流程</h3>
            <ol>
              <li>登录 Web 控制台。</li>
              <li>进入“总览与节点”，确认至少有一个节点在线，并且 GPU/显存资源满足需求。</li>
              <li>进入“环境管理”，选择训练环境，测试 Python / PyTorch CUDA 是否正常。</li>
              <li>进入“文件管理”，确认项目代码、配置文件、数据路径和输出目录存在。</li>
              <li>进入“训练任务”，点击“添加任务”或“批量添加”。</li>
              <li>选择项目路径、环境、GPU数量和执行命令。</li>
              <li>提交后观察等待区和运行区。</li>
              <li>任务开始后查看任务日志，确认没有路径错误、环境错误或 CUDA 错误。</li>
              <li>任务完成后在历史区确认状态；失败任务可修正后“重新提交”。</li>
            </ol>
          </div>

          <div class="card" id="manual-faq">
            <h3>12. 常见问题排查</h3>
            <h4>Q1：节点显示在线，但状态信息是 No data？</h4>
            <p>一般是 Web 端尚未拿到监控缓存，或 master 与 Web 版本不一致。先确认 PyQt 客户端是否正常；如果客户端正常，检查 <code>/api/web-version</code> 和 <code>monitor.py</code> 是否为新版。</p>
            <h4>Q2：任务提交后一直等待？</h4>
            <ul>
              <li>检查是否有在线节点。</li>
              <li>检查 GPU 数量是否超过空闲资源。</li>
              <li>检查是否指定了不存在或离线的节点。</li>
              <li>检查 GPU 型号筛选是否过于严格。</li>
              <li>检查是否设置了尚未完成的前驱任务。</li>
            </ul>
            <h4>Q3：任务一运行就失败？</h4>
            <ul>
              <li>查看任务日志，重点看 Python 报错、路径错误、缺包、CUDA 不匹配。</li>
              <li>确认项目路径在节点上可访问。</li>
              <li>确认 conda 环境在节点上存在。</li>
              <li>确认训练命令在手动 SSH 到节点后可以运行。</li>
            </ul>
            <h4>Q4：日志没有自动刷新？</h4>
            <ul>
              <li>确认页面顶部显示 <code>polling-v5</code>。</li>
              <li>访问 <code>/api/web-version</code> 检查运行版本。</li>
              <li>使用 <span class="manual-kbd">Ctrl</span> + <span class="manual-kbd">F5</span> 强制刷新浏览器缓存。</li>
              <li>确认任务日志文件本身还在写入。</li>
            </ul>
            <h4>Q5：项目路径选择器里看不到想要的目录？</h4>
            <p>说明该目录没有加入 master 的可见目录配置。需要在 <code>conf.json</code> 的 <code>visible_folders</code> 中添加，并重启 master。</p>
            <h4>Q6：环境测试按钮显示错误？</h4>
            <p>优先查看服务端日志；其次手动 SSH 到节点，激活同名环境后测试 Python、PyTorch、TensorFlow 是否正常。</p>
            <h4>Q7：修改了 web_app.py 但页面没变化？</h4>
            <ol>
              <li>确认替换的是实际运行目录下的 <code>master1.2/web_app.py</code>。</li>
              <li>重启 master。</li>
              <li>访问 <code>/api/web-version</code> 看版本号是否变化。</li>
              <li>浏览器按 <span class="manual-kbd">Ctrl</span> + <span class="manual-kbd">F5</span> 强制刷新。</li>
            </ol>
          </div>
        </div>
      </div>
    </section>
  </main>
</div>

<dialog id="taskModal"><div class="modal-head"><strong id="taskModalTitle">添加任务</strong><button class="x" onclick="taskModal.close()">×</button></div><div class="modal-body">
  <div class="form-grid">
    <input type="hidden" id="taskId">
    <div class="field"><label>Conda环境</label><select id="taskEnv"></select></div>
    <div class="field"><label>项目路径</label><div class="path-pick"><input id="taskPath" class="input" value="/data/" readonly><button type="button" class="btn secondary small" onclick="openFolderPicker('taskPath')">选择文件夹</button></div></div>
    <div class="field"><label>指定节点</label><select id="taskSlaver" onchange="loadGpuTypesForTask()"></select></div>
    <div class="field"><label>所需GPU数量</label><input id="taskGpus" class="input" type="number" min="0" value="1"></div>
    <div class="field"><label>前驱任务</label><select id="taskPrev"></select></div>
    <div class="field"><label>选项</label><div><label class="check"><input type="checkbox" id="taskUrgent">紧急任务</label><label class="check"><input type="checkbox" id="taskReuse">复用GPU</label></div></div>
    <div class="field span3"><label>GPU型号需求</label><div class="checkboxes" id="gpuTypeBox"></div></div>
    <div class="field span3"><label>执行命令</label><textarea id="taskExec" placeholder="python train.py --config ..."></textarea></div>
  </div>
  <div class="toolbar"><button class="btn ok" onclick="submitTaskForm()">提交</button><button class="btn secondary" onclick="taskModal.close()">取消</button></div>
</div></dialog>

<dialog id="multiModal"><div class="modal-head"><strong>批量添加任务</strong><button class="x" onclick="multiModal.close()">×</button></div><div class="modal-body">
  <div class="form-grid">
    <div class="field"><label>Conda环境</label><select id="multiEnv"></select></div>
    <div class="field"><label>项目路径</label><div class="path-pick"><input id="multiPath" class="input" value="/data/" readonly><button type="button" class="btn secondary small" onclick="openFolderPicker('multiPath')">选择文件夹</button></div></div>
    <div class="field"><label>指定节点</label><select id="multiSlaver" onchange="loadGpuTypesForMulti()"></select></div>
    <div class="field"><label>所需GPU数量</label><input id="multiGpus" class="input" type="number" min="0" value="1"></div>
    <div class="field"><label>选项</label><div><label class="check"><input type="checkbox" id="multiUrgent">紧急任务</label><label class="check"><input type="checkbox" id="multiReuse">复用GPU</label></div></div>
    <div class="field span3"><label>GPU型号需求</label><div class="checkboxes" id="multiGpuTypeBox"></div></div>
    <div class="field span3"><label>每行一条执行命令，# 之后视为注释</label><textarea id="multiExec" placeholder="python train_a.py&#10;# run train_b&#10;python train_b.py"></textarea></div>
  </div>
  <div class="toolbar"><button class="btn ok" onclick="submitMultiForm()">批量提交</button><button class="btn secondary" onclick="multiModal.close()">取消</button></div>
</div></dialog>


<dialog id="folderPickerModal"><div class="modal-head"><strong>选择项目文件夹</strong><button class="x" onclick="folderPickerModal.close()">×</button></div><div class="modal-body">
  <div class="toolbar">
    <button type="button" class="btn secondary" onclick="pickerGoParent()">上一级</button>
    <button type="button" class="btn ok" onclick="pickerUseCurrent()">选择当前目录</button>
    <button type="button" class="btn secondary" onclick="loadPickerPath('/')">回到根目录</button>
  </div>
  <div class="card" style="box-shadow:none;margin-bottom:12px"><span class="muted">当前目录：</span><strong id="pickerCurrent">/</strong></div>
  <div class="picker-list" id="pickerList"></div>
  <p class="muted" style="line-height:1.6;margin-bottom:0">这里只显示 master 配置里允许访问的目录。双击目录可进入；点击“选择当前目录”会把该目录填入项目路径。</p>
</div></dialog>

<div class="toast" id="toast"></div>
<script>
let STATUS={wait:[],exec:[],hist:[],nodes:[],online:[],hist_total:0,hist_recent_limit:100};
let BOOT={roots:[],slavers:[],envs:[],terminal_enabled:false};
let historyAllLoaded=false;
let selectedTaskId=null, selectedTaskArea='wait', currPath='/', selectedFile=null, openedPath=null, pickerTargetInput=null, pickerPath='/';
let taskLogTimer=null, taskLogAutoId=null, taskLogInFlight=false, taskLogSeq=0;
let taskLogSource=null, taskLogRaw='', taskLogStreamId=null;
const LOG_POLL_INTERVAL_MS=2000;
const $=id=>document.getElementById(id);
function toast(msg){const d=document.createElement('div');d.textContent=msg;$('toast').appendChild(d);setTimeout(()=>d.remove(),3600)}
async function api(url,opt={}){opt.headers=Object.assign({'Accept':'application/json'},opt.headers||{}); if(opt.body && !(opt.body instanceof FormData)){opt.headers['Content-Type']='application/json'; opt.body=JSON.stringify(opt.body)} const r=await fetch(url,opt); if(r.status===401){location.href='/login';return} const ct=r.headers.get('content-type')||''; if(!ct.includes('application/json')) return r; const j=await r.json(); if(!r.ok || j.ok===false){throw new Error(j.error||r.statusText)} return j}
function setTab(name){document.querySelectorAll('.section').forEach(x=>x.classList.remove('active'));$(name).classList.add('active');document.querySelectorAll('.nav button[data-tab]').forEach(b=>b.classList.toggle('active',b.dataset.tab===name));$('pageTitle').textContent=document.querySelector(`.nav button[data-tab="${name}"]`).textContent;if(name!=='logs')stopTaskLogAutoRefresh();if(name==='files' && !window.filesLoaded){loadFiles('/');window.filesLoaded=true} if(name==='envs' && BOOT.envs.length===0)loadEnvs()}
document.querySelectorAll('.nav button[data-tab]').forEach(b=>b.onclick=()=>setTab(b.dataset.tab));
function taskStateClass(s){if(s==='accp')return 'okc'; if(s==='wait'||s==='pexec')return 'warnc'; if(s==='exec')return 'okc'; return 'badc'}
function boolText(v){return Number(v)===1?'是':'否'}
function esc(s){return (s??'').toString().replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]))}
function stripAnsi(s){return (s??'').toString().replace(/\x1B\[[0-?]*[ -/]*[@-~]/g,'').replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g,'')}
function normalizeTerminalText(s){
  s=stripAnsi(s).replace(/\r\n/g,'\n');
  const out=[]; let line='';
  for(const ch of s){
    if(ch==='\r'){line=''; continue}
    if(ch==='\n'){out.push(line); line=''; continue}
    line+=ch;
  }
  out.push(line);
  return out.join('\n').replace(/\n{5,}/g,'\n\n\n\n');
}
function setLogText(id,text){const el=$(id); el.textContent=normalizeTerminalText(text||''); el.scrollTop=el.scrollHeight}
function appendLogText(id,text){
  taskLogRaw+=(text||'');
  // 避免长时间 tail 导致浏览器内存无限增长。这里只保留最近约 1.5MB 原始日志。
  if(taskLogRaw.length>1572864){taskLogRaw=taskLogRaw.slice(-1572864)}
  setLogText(id,taskLogRaw);
}
function extractMsg(data){
  if(data && typeof data==='object' && Object.prototype.hasOwnProperty.call(data,'msg')) return data.msg||'';
  if(typeof data==='string'){
    try{const obj=JSON.parse(data); if(obj && typeof obj==='object' && Object.prototype.hasOwnProperty.call(obj,'msg')) return obj.msg||'';}catch(e){}
    return data;
  }
  return data==null?'':JSON.stringify(data,null,2);
}
function fmtGpuTypes(t){return Array.isArray(t)&&t.length?t.join(','):'默认'}
async function bootstrap(){const j=await api('/api/bootstrap');BOOT=j.data;fillSelect($('taskEnv'),BOOT.envs);fillSelect($('multiEnv'),BOOT.envs);fillSlavers();await refreshAll();setInterval(refreshAll,2200)}
function fillSelect(sel,items,first){sel.innerHTML='';if(first!==undefined)sel.appendChild(new Option(first,first));(items||[]).forEach(x=>sel.appendChild(new Option(x,x)))}
function fillSlavers(){const names=['<默认>'].concat((BOOT.slavers||[]).map(s=>s.name));fillSelect($('taskSlaver'),names);fillSelect($('multiSlaver'),names)}
async function refreshAll(){try{const j=await api('/api/status');const fresh=j.data||{};if(historyAllLoaded){const oldHist=STATUS.hist||[];STATUS=fresh;STATUS.hist=mergeHistoryLists(oldHist, fresh.hist||[])}else{STATUS=fresh}$('lastUpdate').textContent='最后刷新：'+new Date().toLocaleString();$('mNodes').textContent=(STATUS.online||[]).length;$('mWait').textContent=STATUS.wait.length;$('mExec').textContent=STATUS.exec.length;$('mHist').textContent=(STATUS.hist_total!==undefined?STATUS.hist_total:STATUS.hist.length);updateHistoryButton();renderNodes();renderTaskTable()}catch(e){toast('刷新失败：'+e.message)}}
function renderNodes(){const box=$('nodes');box.innerHTML=''; const nodes=STATUS.nodes||[]; if(!nodes.length){box.innerHTML='<div class="card muted">暂无节点监控数据。请确认计算节点在线，或等待 master 监控线程上报。</div>';return} nodes.forEach(n=>{let online=(STATUS.online||[]).includes(n.name);let gpus=(n.gpus||[]).map((g,i)=>{let util=Math.round((g.mem_util||0)*100);return `<div class="gpu"><b>GPU ${i}</b> <span class="muted">${esc(g.name||'')}</span><div>${esc(g.gpu_used||'No data')} · 可用显存 ${esc(g.avail_vram||'No data')} · 调度占用 ${g.scheduled_used?'是':'否'}</div><div class="bar"><span style="width:${isFinite(util)?util:0}%"></span></div></div>`}).join('');box.insertAdjacentHTML('beforeend',`<div class="card node"><h3>${esc(n.name)} ${online?'<span class="pill okc">在线</span>':'<span class="pill badc">离线</span>'}</h3><div class="muted">CPU ${esc(n.cpu_usage||'No data')} · RAM ${esc(n.avail_ram||'No data')}</div><div class="muted">上行 ${esc(n.web_up||'No data')} · 下行 ${esc(n.web_down||'No data')}</div>${gpus}</div>`)})}
function mergeHistoryLists(oldList,newList){const m=new Map();(oldList||[]).forEach(t=>m.set(t.task_id,t));(newList||[]).forEach(t=>m.set(t.task_id,t));return Array.from(m.values())}
function updateHistoryButton(){const btn=$('loadAllHistBtn');if(!btn)return;const total=STATUS.hist_total!==undefined?STATUS.hist_total:(STATUS.hist||[]).length;const shown=(STATUS.hist||[]).length;if(historyAllLoaded){btn.textContent='已显示全部历史任务（'+shown+'/'+total+'）';btn.disabled=false}else{btn.textContent='显示所有历史任务（当前显示 '+shown+'/'+total+'）';btn.disabled=false}}
async function loadAllHistory(){try{const btn=$('loadAllHistBtn');if(btn){btn.disabled=true;btn.textContent='正在加载全部历史任务...'}const j=await api('/api/history/all?_='+(Date.now()));const data=j.data||{};STATUS.hist=data.items||[];STATUS.hist_total=data.total!==undefined?data.total:STATUS.hist.length;historyAllLoaded=true;$('mHist').textContent=STATUS.hist_total;$('taskTableSelect').value='hist';selectedTaskArea='hist';selectedTaskId=null;updateHistoryButton();renderTaskTable();toast('已加载全部历史任务：'+STATUS.hist.length+' 条')}catch(e){toast('加载全部历史任务失败：'+e.message);updateHistoryButton()}}
function renderTaskTable(){const area=$('taskTableSelect').value;selectedTaskArea=area;const rows=STATUS[area]||[];const kw=$('taskFilter').value.trim().toLowerCase();const filtered=rows.filter(x=>!kw||JSON.stringify(x).toLowerCase().includes(kw));let title='';if(area==='hist'){const total=STATUS.hist_total!==undefined?STATUS.hist_total:rows.length;title=`<caption style="caption-side:top;text-align:left;padding:10px 12px;color:#91a0bd">历史任务：当前表格显示 ${rows.length} / 共 ${total} 条${historyAllLoaded?'':'；如需查看全部，请点击上方“显示所有历史任务”'}</caption>`}let html=title+'<thead><tr><th></th><th>任务ID</th><th>环境</th><th>路径</th><th>命令</th><th>节点</th><th>GPU数</th><th>GPU型号</th><th>前驱</th><th>紧急</th><th>复用</th><th>状态</th></tr></thead><tbody>';filtered.forEach(t=>{html+=`<tr onclick="selectTask('${esc(t.task_id)}','${area}')"><td><input type="radio" name="taskSel" ${selectedTaskId===t.task_id?'checked':''}></td><td>${esc(t.task_id)}</td><td>${esc(t.envs)}</td><td>${esc(t.path)}</td><td>${esc(t.exec)}</td><td>${esc(t.slaver)}</td><td>${esc(t.need_gpus)}</td><td>${esc(fmtGpuTypes(t.gpu_type))}</td><td>${esc(t.prev)}</td><td>${boolText(t.is_urgent)}</td><td>${boolText(t.is_reuse_gpu)}</td><td><span class="pill ${taskStateClass(t.state)}">${esc(t.state_text||t.state)}</span></td></tr>`});html+='</tbody>';$('taskTable').innerHTML=html;updateHistoryButton()}
function selectTask(id,area){selectedTaskId=id;selectedTaskArea=area;renderTaskTable()}
function findSelectedTask(){let arr=STATUS[selectedTaskArea]||[];return arr.find(x=>x.task_id===selectedTaskId)}
function fillPrevSelect(sel,current){sel.innerHTML='';sel.appendChild(new Option('(无)','(无)'));[...STATUS.wait,...STATUS.exec].forEach(t=>{if(t.task_id!==current)sel.appendChild(new Option(t.task_id,t.task_id))})}
function gpuChecks(boxId,types,checked){const box=$(boxId);box.innerHTML='';(types||[]).forEach(t=>{const id=boxId+'_'+t.replace(/\W/g,'_');box.insertAdjacentHTML('beforeend',`<label class="check"><input type="checkbox" value="${esc(t)}" ${checked&&checked.includes(t)?'checked':''}>${esc(t)}</label>`)})}
async function loadGpuTypesForTask(checked){try{let sl=$('taskSlaver').value;const j=await api('/api/gpu-types?slaver='+encodeURIComponent(sl==='<默认>'?'':sl));gpuChecks('gpuTypeBox',j.data,checked||[])}catch(e){toast(e.message)}}
async function loadGpuTypesForMulti(){try{let sl=$('multiSlaver').value;const j=await api('/api/gpu-types?slaver='+encodeURIComponent(sl==='<默认>'?'':sl));gpuChecks('multiGpuTypeBox',j.data,[])}catch(e){toast(e.message)}}
function checkedValues(boxId){return Array.from($(boxId).querySelectorAll('input:checked')).map(x=>x.value)}
async function openTaskModal(mode){fillSelect($('taskEnv'),BOOT.envs);fillSlavers();fillPrevSelect($('taskPrev'));$('taskId').value='';$('taskPath').value='/data/';$('taskExec').value='';$('taskGpus').value='1';$('taskUrgent').checked=false;$('taskReuse').checked=false;$('taskModalTitle').textContent='添加任务';await loadGpuTypesForTask();taskModal.showModal()}
async function editSelectedTask(){const t=findSelectedTask(); if(!t){toast('请先选择任务');return} if(selectedTaskArea==='exec'){toast('运行中的任务不能修改');return} fillSelect($('taskEnv'),BOOT.envs);fillSlavers();fillPrevSelect($('taskPrev'),t.task_id);$('taskId').value=t.task_id;$('taskEnv').value=t.envs;$('taskPath').value=t.path;$('taskExec').value=t.exec;$('taskSlaver').value=t.slaver||'<默认>';$('taskGpus').value=t.need_gpus;$('taskPrev').value=t.prev;$('taskUrgent').checked=Number(t.is_urgent)===1;$('taskReuse').checked=Number(t.is_reuse_gpu)===1;$('taskModalTitle').textContent='修改任务：'+t.task_id;await loadGpuTypesForTask(t.gpu_type||[]);taskModal.showModal()}
async function submitTaskForm(){const body={task_id:$('taskId').value,envs:$('taskEnv').value,path:$('taskPath').value,exec:$('taskExec').value,need_gpus:$('taskGpus').value,slaver:$('taskSlaver').value,prev:$('taskPrev').value,is_urgent:$('taskUrgent').checked?1:0,is_reuse_gpu:$('taskReuse').checked?1:0,state:'',gpu_type:checkedValues('gpuTypeBox')};try{await api(body.task_id?'/api/task/change':'/api/task/add',{method:'POST',body});toast(body.task_id?'修改成功':'添加成功');taskModal.close();refreshAll()}catch(e){toast(e.message)}}
async function openMultiModal(){fillSelect($('multiEnv'),BOOT.envs);fillSlavers();$('multiPath').value='/data/';$('multiGpus').value='1';$('multiExec').value='';$('multiUrgent').checked=false;$('multiReuse').checked=false;await loadGpuTypesForMulti();multiModal.showModal()}
async function submitMultiForm(){const lines=$('multiExec').value.split('\n').map(x=>x.split('#')[0].trim()).filter(Boolean);if(!lines.length){toast('请输入至少一条命令');return}const common={envs:$('multiEnv').value,path:$('multiPath').value,need_gpus:$('multiGpus').value,slaver:$('multiSlaver').value,prev:'(无)',is_urgent:$('multiUrgent').checked?1:0,is_reuse_gpu:$('multiReuse').checked?1:0,state:'',gpu_type:checkedValues('multiGpuTypeBox')};const tasks=lines.map(cmd=>Object.assign({task_id:'',exec:cmd},common));try{await api('/api/task/multi-add',{method:'POST',body:{tasks}});toast('批量提交成功：'+tasks.length+' 条');multiModal.close();refreshAll()}catch(e){toast(e.message)}}
async function deleteSelectedTask(){if(!selectedTaskId){toast('请先选择任务');return}if(selectedTaskArea==='exec'){toast('运行中的任务不能删除，请先中止');return}const delete_children=confirm('是否同时删除该任务的所有后继任务？\n确定：删除当前任务及所有后继任务\n取消：只删除当前任务');if(!confirm('确认执行删除操作？该操作不可逆。'))return;try{await api('/api/task/delete',{method:'POST',body:{task_id:selectedTaskId,delete_children}});toast('删除成功');selectedTaskId=null;refreshAll()}catch(e){toast(e.message)}}
async function stopSelectedTask(){if(!selectedTaskId||selectedTaskArea!=='exec'){toast('请在执行区选择任务');return}try{await api('/api/task/stop',{method:'POST',body:{task_id:selectedTaskId}});toast('已发送中止请求')}catch(e){toast(e.message)}}
async function resubmitSelectedTask(){if(!selectedTaskId){toast('请先选择任务');return}try{await api('/api/task/resubmit',{method:'POST',body:{task_id:selectedTaskId}});toast('已重新提交');refreshAll()}catch(e){toast(e.message)}}
function setLogStatus(msg, state){
  const box=$('logStatus');
  const text=$('logStatusText');
  if(text) text.textContent=msg||'';
  if(box){box.classList.remove('running','error'); if(state) box.classList.add(state);}
}
function stopTaskLogAutoRefresh(msg, state){
  if(taskLogTimer){clearInterval(taskLogTimer);taskLogTimer=null}
  if(taskLogSource){try{taskLogSource.close()}catch(e){} taskLogSource=null}
  taskLogAutoId=null;
  taskLogStreamId=null;
  taskLogInFlight=false;
  taskLogSeq++;
  setLogStatus(msg || '日志刷新模块：polling，自动刷新已停止。', state || '');
}
function startTaskLogAutoRefresh(id){
  id=(id||'').trim();
  if(!id)return;
  stopTaskLogAutoRefresh();
  taskLogAutoId=id;
  taskLogStreamId=id;
  taskLogSeq++;
  const seq=taskLogSeq;
  const tick=()=>refreshTaskLogAuto(id, seq);
  tick();
  taskLogTimer=setInterval(tick,LOG_POLL_INTERVAL_MS);
  setLogStatus('日志刷新模块：polling · 正在自动刷新任务 '+id+' · 每 2 秒一次','running');
}
function startTaskLogStream(id){
  // 保留旧入口名，实际使用更稳定的 2 秒轮询 tail。
  return startTaskLogAutoRefresh(id);
}
function showSelectedLog(){
  if(!selectedTaskId){toast('请先选择任务');return}
  setTab('logs');
  $('logTaskId').value=selectedTaskId;
  loadTaskLog(selectedTaskId,false);
}
async function loadTaskLogByInput(){
  let id=$('logTaskId').value.trim();
  if(!id){toast('请输入任务ID');return}
  setTab('logs');
  await loadTaskLog(id,false);
}
async function refreshTaskLogAuto(id, seq){
  const logsSection=document.getElementById('logs');
  if(!logsSection || !logsSection.classList.contains('active')){stopTaskLogAutoRefresh();return}
  if(taskLogAutoId!==id){return}
  if(taskLogInFlight){return}
  taskLogInFlight=true;
  try{
    const url='/api/task/log/'+encodeURIComponent(id)+'?bytes=1048576&_='+(Date.now());
    const r=await fetch(url,{cache:'no-store',headers:{'Accept':'application/json','Cache-Control':'no-cache'}});
    if(r.status===401){location.href='/login';return}
    const j=await r.json();
    if(!r.ok || j.ok===false){throw new Error((j&&j.error)||r.statusText)}
    if(seq===taskLogSeq && taskLogAutoId===id){
      const payload=j.data;
      let content='';
      let terminal=false;
      let area='unknown';
      let state='';
      let stateText='';
      if(payload && typeof payload==='object' && Object.prototype.hasOwnProperty.call(payload,'content')){
        content=payload.content||'';
        terminal=!!payload.terminal;
        area=payload.area||'unknown';
        state=payload.state||'';
        stateText=payload.state_text||state||'';
      }else{
        content=payload||'';
      }
      taskLogRaw=content;
      const now=new Date().toLocaleTimeString();
      const suffix=terminal ? '状态：'+(stateText||area)+'，已自动停止刷新' : '每 2 秒刷新一次';
      const refreshed='[自动刷新 polling] 任务 '+id+' · '+now+' · '+suffix+'\n';
      setLogText('logBox',refreshed+taskLogRaw);
      if(terminal){
        stopTaskLogAutoRefresh('日志刷新模块：polling · 任务 '+id+' 已结束（'+(stateText||area)+'），已自动停止刷新。','');
      }else{
        setLogStatus('日志刷新模块：polling · 已刷新 '+id+' · '+now+' · 当前状态 '+(stateText||area)+' · 下一次约 2 秒后','running');
      }
    }
  }catch(e){
    if(seq===taskLogSeq && taskLogAutoId===id){
      const msg='[自动刷新失败 polling] '+new Date().toLocaleTimeString()+' · '+e.message+'\n';
      setLogText('logBox',msg+(taskLogRaw||''));
      setLogStatus('日志刷新模块：polling · 刷新失败：'+e.message,'error');
    }
  }finally{
    taskLogInFlight=false;
  }
}
async function loadTaskLog(id,silent){
  id=(id||'').trim();
  if(!id){toast('请输入任务ID');return}
  stopTaskLogAutoRefresh();
  taskLogRaw='';
  $('logTaskId').value=id;
  setLogText('logBox','正在读取任务日志：'+id+'\n刷新模块版本：polling\n随后将每 2 秒自动刷新。\n');
  setLogStatus('日志刷新模块：polling · 正在启动任务 '+id+' 的自动刷新','running');
  startTaskLogAutoRefresh(id);
  if(!silent)toast('任务日志已开启 2 秒自动刷新：'+id);
}
async function loadServerLog(){
  stopTaskLogAutoRefresh();
  taskLogRaw='';
  setLogStatus('正在读取服务端日志；服务端日志不会自动刷新。','');
  try{const j=await api('/api/server-log?_='+(Date.now()));setLogText('logBox',j.data||'')}catch(e){toast(e.message);setLogStatus('读取服务端日志失败：'+e.message,'error')}
}

function normalizeFolderPath(p){
  if(!p) return '/';
  p=String(p).replace(/\\/g,'/');
  if(!p.startsWith('/')) p='/'+p;
  if(!p.endsWith('/')) p+='/';
  return p.replace(/\/+/g,'/');
}
async function openFolderPicker(inputId){
  pickerTargetInput=inputId;
  let start=$(inputId).value || '/';
  await loadPickerPath(start);
  folderPickerModal.showModal();
}
async function loadPickerPath(path){
  try{
    const j=await api('/api/fs/list?path='+encodeURIComponent(path || '/'));
    pickerPath=j.data.path || '/';
    $('pickerCurrent').textContent=pickerPath;
    renderPickerFolders(j.data.folders || []);
  }catch(e){toast(e.message)}
}
function renderPickerFolders(folders){
  const box=$('pickerList');
  box.innerHTML='';
  if(!folders.length){box.innerHTML='<div class="muted" style="padding:12px">当前目录下没有可进入的子文件夹。</div>';return}
  folders.forEach(name=>{
    box.insertAdjacentHTML('beforeend',`<button type="button" class="picker-item" onclick="loadPickerPath('${esc(joinPath(pickerPath,name))}')"><span>📁 ${esc(name)}</span><span class="muted">进入</span></button>`);
  });
}
function pickerGoParent(){
  if(pickerPath==='/'||!pickerPath){loadPickerPath('/');return}
  let p=pickerPath.replace(/\/$/,'').split('/').slice(0,-1).join('/') || '/';
  loadPickerPath(p);
}
function pickerUseCurrent(){
  if(!pickerTargetInput){toast('没有目标输入框');return}
  $(pickerTargetInput).value=normalizeFolderPath(pickerPath);
  folderPickerModal.close();
}
async function loadFiles(path){try{const j=await api('/api/fs/list?path='+encodeURIComponent(path));currPath=j.data.path;selectedFile=null;$('currPath').textContent=currPath;renderFiles(j.data)}catch(e){toast(e.message)}}
function renderFiles(d){const box=$('fileList');box.innerHTML='';[...(d.folders||[]).map(x=>({name:x,type:'dir'})),...(d.files||[]).map(x=>({name:x,type:'file'}))].forEach(it=>{let icon=it.type==='dir'?'📁':'📄';box.insertAdjacentHTML('beforeend',`<button class="file-item" onclick="selectFile('${esc(it.name)}','${it.type}')" ondblclick="openFileItem('${esc(it.name)}','${it.type}')"><span>${icon} ${esc(it.name)}</span><span class="muted">${it.type}</span></button>`)})}
function selectFile(name,type){selectedFile={name,type};document.querySelectorAll('.file-item').forEach(b=>b.classList.remove('active'));event.currentTarget.classList.add('active')}
function joinPath(a,b){if(a.endsWith('/'))return a+b;return a+'/'+b}
async function openFileItem(name,type){if(type==='dir')return loadFiles(joinPath(currPath,name));let p=joinPath(currPath,name);try{const j=await api('/api/fs/read?path='+encodeURIComponent(p));openedPath=p;$('openedFile').textContent=p;$('editor').value=j.data.content||''}catch(e){toast(e.message)}}
function goParent(){if(currPath==='/'||!currPath)return;let p=currPath.replace(/\/$/,'').split('/').slice(0,-1).join('/')||'/';loadFiles(p)}
async function saveOpenedFile(){if(!openedPath){toast('未打开文件');return}try{await api('/api/fs/write',{method:'POST',body:{path:openedPath,content:$('editor').value}});toast('保存成功')}catch(e){toast(e.message)}}
async function makeFileOp(op){let name=prompt(op==='mkdir'?'请输入文件夹名称':'请输入文件名称');if(!name)return;try{await api('/api/fs/op',{method:'POST',body:{op,path:currPath,name}});toast('操作成功');loadFiles(currPath)}catch(e){toast(e.message)}}
async function renameSelected(){if(!selectedFile){toast('请选择文件/文件夹');return}let name=prompt('请输入新名称',selectedFile.name);if(!name||name===selectedFile.name)return;try{await api('/api/fs/op',{method:'POST',body:{op:'rename',path:currPath,name,old_name:selectedFile.name}});toast('重命名成功');loadFiles(currPath)}catch(e){toast(e.message)}}
async function copyMoveSelected(op){if(!selectedFile){toast('请选择文件/文件夹');return}let dest=prompt((op==='cp'?'复制到':'移动到')+'目标目录',currPath);if(!dest)return;try{await api('/api/fs/op',{method:'POST',body:{op,path:dest,src:joinPath(currPath,selectedFile.name),name:selectedFile.name}});toast(op==='cp'?'复制成功':'移动成功');loadFiles(currPath)}catch(e){toast(e.message)}}
async function deleteSelectedFile(){if(!selectedFile){toast('请选择文件/文件夹');return}if(!confirm('确认删除 '+selectedFile.name+' ?'))return;try{await api('/api/fs/delete',{method:'POST',body:{path:joinPath(currPath,selectedFile.name)}});toast('删除成功');loadFiles(currPath)}catch(e){toast(e.message)}}
async function uploadFiles(){const inp=$('uploadInput');if(!inp.files.length){toast('请选择文件');return}const fd=new FormData();fd.append('path',currPath);Array.from(inp.files).forEach(f=>fd.append('files',f));try{await api('/api/fs/upload',{method:'POST',body:fd,headers:{}});toast('上传成功');inp.value='';loadFiles(currPath)}catch(e){toast(e.message)}}
function downloadSelected(){if(!selectedFile){toast('请选择文件/文件夹');return}location.href='/api/fs/download?path='+encodeURIComponent(joinPath(currPath,selectedFile.name))}
async function loadEnvs(){try{const j=await api('/api/envs');BOOT.envs=j.data||[];fillSelect($('envSelect'),BOOT.envs);fillSelect($('taskEnv'),BOOT.envs);fillSelect($('multiEnv'),BOOT.envs)}catch(e){toast(e.message)}}
async function envAction(action){let env=$('envSelect').value;if(!env){toast('请选择环境');return}setLogText('envResult','执行中...');try{const j=await api('/api/env/action',{method:'POST',body:{action,env}});setLogText('envResult',extractMsg(j.data))}catch(e){setLogText('envResult','失败：'+e.message)}}
async function runShell(){let cmd=$('shellCmd').value.trim();if(!cmd){toast('请输入命令');return}try{const j=await api('/api/shell',{method:'POST',body:{cmd}});setLogText('shellOut',($('shellOut').textContent?'\n':'')+'$ '+cmd+'\n'+(j.data.stdout||'')+(j.data.stderr?'\n[stderr]\n'+j.data.stderr:''));$('shellCmd').value=''}catch(e){setLogText('shellOut',$('shellOut').textContent+'\n失败：'+e.message)}}
bootstrap().catch(e=>toast('初始化失败：'+e.message));
</script>
</body>
</html>
"""


class UserStore(object):
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self._init_db()

    def _conn(self):
        return sqlite3.connect(self.path)

    def _init_db(self):
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent and not os.path.exists(parent):
            os.makedirs(parent)
        with self._conn() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS users ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "username TEXT UNIQUE NOT NULL, "
                "password_hash TEXT NOT NULL, "
                "salt TEXT NOT NULL, "
                "created_at TEXT NOT NULL)"
            )

    def has_users(self):
        with self._conn() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM users")
            return cur.fetchone()[0] > 0

    def create_user(self, username, password):
        username = (username or "").strip()
        if not username or not password:
            raise ValueError("用户名和密码不能为空")
        if len(username) > 64:
            raise ValueError("用户名过长")
        salt = secrets.token_hex(16)
        password_hash = self._hash(password, salt)
        with self.lock:
            try:
                with self._conn() as conn:
                    conn.execute(
                        "INSERT INTO users(username,password_hash,salt,created_at) VALUES(?,?,?,?)",
                        (username, password_hash, salt, datetime.datetime.now().isoformat(timespec="seconds")),
                    )
            except sqlite3.IntegrityError:
                raise ValueError("用户名已存在")

    def verify(self, username, password):
        with self._conn() as conn:
            cur = conn.execute("SELECT password_hash,salt FROM users WHERE username=?", (username,))
            row = cur.fetchone()
        if not row:
            return False
        return hmac.compare_digest(row[0], self._hash(password, row[1]))

    @staticmethod
    def _hash(password, salt):
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200000)
        return dk.hex()


def _json_ok(data=None):
    return jsonify({"ok": True, "data": data})


def _json_err(msg, code=400):
    return jsonify({"ok": False, "error": str(msg)}), code


def create_web_app(master):
    web_conf = master.config.config.get("web_info", {})
    db_path = web_conf.get("user_db", "./web_users.db")
    secret_key = web_conf.get("secret_key") or os.environ.get("DDLT_WEB_SECRET") or secrets.token_urlsafe(32)
    allow_register = bool(web_conf.get("allow_register", True))
    terminal_enabled = bool(web_conf.get("terminal_enabled", False))

    app = Flask(__name__)
    app.secret_key = secret_key
    app.config["MAX_CONTENT_LENGTH"] = int(web_conf.get("max_upload_mb", 512)) * 1024 * 1024

    @app.after_request
    def _disable_web_cache(resp):
        # Web UI is a single inline template. Disable browser/proxy caching so replacing
        # web_app.py immediately updates JavaScript such as the log auto-refresh logic.
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp

    users = UserStore(db_path)

    terminal_sessions = {}
    terminal_lock = threading.Lock()

    class TerminalSession(object):
        """A lightweight persistent shell session for the Web terminal.

        This mirrors the PyQt client's terminal_thread more closely than a one-shot
        subprocess.run call: commands share cwd/environment inside one shell, and
        miniconda activation is attempted once at session start.
        """
        def __init__(self, cwd):
            self.q = queue.Queue()
            self.proc = subprocess.Popen(
                ["/bin/bash"],
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                universal_newlines=True,
            )
            self.reader = threading.Thread(target=self._read_loop, daemon=True)
            self.reader.start()
            self.send("source ~/envs/miniconda3/bin/activate")

        def _read_loop(self):
            try:
                for line in iter(self.proc.stdout.readline, ""):
                    self.q.put(line)
            except Exception as e:
                self.q.put("[terminal read error] %s\n" % e)

        def alive(self):
            return self.proc.poll() is None

        def send(self, cmd):
            if not self.alive():
                raise RuntimeError("终端会话已退出")
            self.proc.stdin.write(cmd + "\n")
            self.proc.stdin.flush()

        def read(self):
            chunks = []
            while True:
                try:
                    chunks.append(self.q.get_nowait())
                except queue.Empty:
                    break
            return "".join(chunks)

        def close(self):
            try:
                self.proc.terminate()
            except Exception:
                pass

    def get_terminal_session():
        key = session.get("user", "default")
        terminal_lock.acquire()
        try:
            term = terminal_sessions.get(key)
            if term is None or not term.alive():
                term = TerminalSession(root_dir())
                terminal_sessions[key] = term
            return term
        finally:
            terminal_lock.release()


    def logged_in():
        return bool(session.get("user"))

    def require_login(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not logged_in():
                if request.path.startswith("/api/"):
                    return _json_err("请先登录", 401)
                return redirect(url_for("login"))
            return fn(*args, **kwargs)
        return wrapper

    def root_dir():
        return os.path.abspath(master.config.config.get("root", os.path.expanduser("~")))

    def visible_roots():
        return list(master.config.visible_folders)

    def resolve_path(user_path, must_exist=False):
        root = root_dir()
        user_path = (user_path or "/").replace("\\", "/")
        if user_path == "/":
            return root
        norm = os.path.normpath("/" + user_path.lstrip("/"))
        parts = [p for p in norm.split("/") if p]
        if not parts or parts[0] not in visible_roots():
            raise ValueError("路径不在允许访问的目录内")
        abs_path = os.path.abspath(os.path.join(root, *parts))
        allowed_root = os.path.abspath(os.path.join(root, parts[0]))
        if abs_path != allowed_root and not abs_path.startswith(allowed_root + os.sep):
            raise ValueError("非法路径")
        if must_exist and not os.path.exists(abs_path):
            raise ValueError("路径不存在")
        return abs_path

    def to_user_path(abs_path):
        rel = os.path.relpath(abs_path, root_dir()).replace("\\", "/")
        if rel == ".":
            return "/"
        return "/" + rel

    def copy_task_list(items):
        ret = []
        for t in items:
            x = dict(t)
            x["state_text"] = STATE_TEXT.get(x.get("state"), x.get("state", ""))
            if "gpu_type" not in x:
                x["gpu_type"] = []
            ret.append(x)
        return ret

    def parse_process_response(resp):
        try:
            data, need = resp
            if not need or not data:
                return None
            return json.loads(data)
        except Exception:
            return None

    def call_process(mode, info="", ui="web"):
        # Import lazily. process.py reads config.json on every call and reuses the old protocol.
        from process import process
        payload = json.dumps({"mode": mode, "ui": ui, "info": info}, ensure_ascii=False)
        resp = process(payload)
        parsed = parse_process_response(resp)
        if parsed is None:
            raise RuntimeError("master process 未返回有效结果")
        return parsed.get("info")

    def extract_msg_from_process_info(info):
        # Keep parity with the PyQt client for environment actions: show only
        # the user-facing msg field instead of rendering the whole {res, msg} JSON.
        if isinstance(info, dict):
            if "msg" in info:
                return info.get("msg", "")
            return json.dumps(info, ensure_ascii=False, indent=2)
        if isinstance(info, str):
            try:
                obj = json.loads(info)
                if isinstance(obj, dict) and "msg" in obj:
                    return obj.get("msg", "")
            except Exception:
                pass
            return info
        if info is None:
            return ""
        return str(info)

    def get_envs():
        try:
            info = call_process("env_list")
            return info or []
        except Exception:
            # Fallback for machines where os.getlogin() or SSH probing is unavailable.
            try:
                p = subprocess.run("conda env list | awk '{print $1}'", shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
                text = p.stdout.decode("utf-8", "ignore")
                lines = [x.strip() for x in text.splitlines() if x.strip()]
                return [x for x in lines if not x.startswith("#") and x not in ("base",)] or lines[2:]
            except Exception:
                return []

    def get_nodes():
        latest_src = getattr(master.monitor, "latest_info", {})
        latest_lock = getattr(master.monitor, "latest_lock", None)
        if latest_lock is not None:
            latest_lock.acquire()
        try:
            latest = dict(latest_src)
        finally:
            if latest_lock is not None:
                latest_lock.release()
        nodes = []
        for s in master.config.slaver_info:
            name = s.get("name")
            info = dict(latest.get(name, {}))
            info.setdefault("name", name)
            info.setdefault("cpu_usage", "No data")
            info.setdefault("avail_ram", "No data")
            info.setdefault("web_up", "No data")
            info.setdefault("web_down", "No data")
            info.setdefault("gpus", [])
            # merge scheduled GPU usage and GPU names from task scheduler state
            sched = None
            for st in master.slaver_state:
                if st.get("name") == name:
                    sched = st
                    break
            if sched:
                gpus = []
                source_gpus = info.get("gpus") or []
                gpu_names = sched.get("gpu_name", [])
                gpu_used = sched.get("gpu_used", [])
                count = max(len(source_gpus), len(gpu_names), int(s.get("gpu_count", 0)))
                for i in range(count):
                    g = dict(source_gpus[i]) if i < len(source_gpus) else {}
                    if i < len(gpu_names):
                        g["name"] = gpu_names[i]
                    if i < len(gpu_used):
                        g["scheduled_used"] = bool(gpu_used[i])
                    gpus.append(g)
                info["gpus"] = gpus
            else:
                gpus = []
                for i, gpu_name in enumerate(s.get("gpu_info", [])):
                    gpus.append({"name": gpu_name, "gpu_used": "No data", "avail_vram": "No data", "mem_util": 0, "scheduled_used": False})
                if not info.get("gpus"):
                    info["gpus"] = gpus
            nodes.append(info)
        return nodes

    @app.route("/")
    @require_login
    def index():
        return render_template_string(APP_HTML, username=session.get("user"), is_admin=(session.get("user") == "admin"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        register_enabled = allow_register or not users.has_users()
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            if users.verify(username, password):
                session["user"] = username
                return redirect(url_for("index"))
            flash("用户名或密码错误", "err")
        return render_template_string(LOGIN_HTML, title="登录", button="登录", mode="login", register_enabled=register_enabled)

    @app.route("/register", methods=["GET", "POST"])
    def register():
        register_enabled = allow_register or not users.has_users()
        if not register_enabled:
            flash("注册功能已关闭，请使用已有账号登录", "err")
            return redirect(url_for("login"))
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            password2 = request.form.get("password2", "")
            if password != password2:
                flash("两次输入的密码不一致", "err")
            elif len(password) < 6:
                flash("密码至少需要 6 位", "err")
            else:
                try:
                    users.create_user(username, password)
                    flash("注册成功，请登录", "ok")
                    return redirect(url_for("login"))
                except Exception as e:
                    flash(str(e), "err")
        return render_template_string(LOGIN_HTML, title="注册", button="注册", mode="register", register_enabled=register_enabled)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/api/bootstrap")
    @require_login
    def api_bootstrap():
        return _json_ok({
            "roots": visible_roots(),
            "slavers": master.config.slaver_info,
            "envs": get_envs(),
            "terminal_enabled": terminal_enabled,
        })

    @app.route("/api/web-version")
    @require_login
    def api_web_version():
        return _json_ok({"web_app": "polling-history-paged-manual-admin-v3-adminlink", "log_refresh": "tail-polling-2s-auto-stop", "history_mode": "recent_100_plus_load_all", "manual": True, "auto_stop_on_terminal": True, "time": datetime.datetime.now().isoformat(timespec="seconds")})

    @app.route("/api/status")
    @require_login
    def api_status():
        task_lock.acquire()
        try:
            data = {
                "wait": copy_task_list(wait_task),
                "exec": copy_task_list(exec_task),
                "hist": copy_task_list(hist_task[-100:]),
                "hist_total": len(hist_task),
                "hist_recent_limit": 100,
                "online": list(master.online_slaver),
                "nodes": get_nodes(),
            }
        finally:
            task_lock.release()
        return _json_ok(data)

    @app.route("/api/history/all")
    @require_login
    def api_history_all():
        task_lock.acquire()
        try:
            items = copy_task_list(hist_task)
            total = len(hist_task)
        finally:
            task_lock.release()
        return _json_ok({"items": items, "total": total})

    @app.route("/api/gpu-types")
    @require_login
    def api_gpu_types():
        slaver = request.args.get("slaver", "")
        gpu_types = []
        for s in master.config.slaver_info:
            if slaver and s.get("name") != slaver:
                continue
            for g in s.get("gpu_info", []):
                if g not in gpu_types:
                    gpu_types.append(g)
        return _json_ok(gpu_types)

    def normalize_task(task):
        required = ["envs", "path", "exec", "need_gpus", "slaver", "prev", "is_urgent", "is_reuse_gpu"]
        for k in required:
            if k not in task:
                raise ValueError("任务字段缺失: " + k)
        task["task_id"] = task.get("task_id", "")
        task["state"] = task.get("state", "")
        task["gpu_type"] = task.get("gpu_type", []) or []
        task["need_gpus"] = str(task["need_gpus"])
        task["is_urgent"] = 1 if int(task.get("is_urgent", 0)) else 0
        task["is_reuse_gpu"] = 1 if int(task.get("is_reuse_gpu", 0)) else 0
        return task

    @app.route("/api/task/add", methods=["POST"])
    @require_login
    def api_task_add():
        try:
            task = normalize_task(request.get_json(force=True))
            wait_queue.put(task)
            log("Web 添加了任务", master.config.config["server_log_path"])
            return _json_ok("")
        except Exception as e:
            return _json_err(e)

    @app.route("/api/task/multi-add", methods=["POST"])
    @require_login
    def api_task_multi_add():
        try:
            data = request.get_json(force=True)
            tasks = data.get("tasks", [])
            if not tasks:
                raise ValueError("任务列表为空")
            for task in tasks:
                wait_queue.put(normalize_task(task))
            log("Web 批量添加了 %d 个任务" % len(tasks), master.config.config["server_log_path"])
            return _json_ok("")
        except Exception as e:
            return _json_err(e)

    @app.route("/api/task/change", methods=["POST"])
    @require_login
    def api_task_change():
        try:
            task = normalize_task(request.get_json(force=True))
            task_id = task.get("task_id")
            if not task_id:
                raise ValueError("缺少 task_id")
            ret = False
            task_lock.acquire()
            try:
                for i in range(len(wait_task)):
                    if wait_task[i].get("task_id") == task_id:
                        task["state"] = task.get("state") or "wait"
                        wait_task[i] = task
                        ret = True
                        break
                if not ret:
                    for i in range(len(hist_task)):
                        if hist_task[i].get("task_id") == task_id:
                            hist_task.pop(i)
                            wait_queue.put(task)
                            ret = True
                            break
            finally:
                task_lock.release()
            if not ret:
                raise ValueError("修改失败：任务可能正在运行或不存在")
            log("Web 修改了任务 " + task_id, master.config.config["server_log_path"])
            return _json_ok("")
        except Exception as e:
            return _json_err(e)

    @app.route("/api/task/delete", methods=["POST"])
    @require_login
    def api_task_delete():
        try:
            payload = request.get_json(force=True)
            task_id = payload.get("task_id")
            delete_children = bool(payload.get("delete_children", False))
            if not task_id:
                raise ValueError("缺少 task_id")

            task_lock.acquire()
            try:
                ids_to_delete = set([task_id])
                if delete_children:
                    changed = True
                    while changed:
                        changed = False
                        for arr in (wait_task, hist_task):
                            for t in arr:
                                if t.get("prev") in ids_to_delete and t.get("task_id") not in ids_to_delete:
                                    ids_to_delete.add(t.get("task_id"))
                                    changed = True

                removed = []
                for arr in (wait_task, hist_task):
                    for i in range(len(arr) - 1, -1, -1):
                        if arr[i].get("task_id") in ids_to_delete:
                            removed.append(arr[i].get("task_id"))
                            arr.pop(i)
            finally:
                task_lock.release()

            if not removed:
                raise ValueError("删除失败：任务可能正在运行或不存在")
            log("Web 删除了任务: " + ",".join(removed), master.config.config["server_log_path"])
            return _json_ok({"removed": removed})
        except Exception as e:
            return _json_err(e)

    @app.route("/api/task/resubmit", methods=["POST"])
    @require_login
    def api_task_resubmit():
        try:
            task_id = request.get_json(force=True).get("task_id")
            src = None
            task_lock.acquire()
            try:
                for arr in (wait_task, exec_task, hist_task):
                    for t in arr:
                        if t.get("task_id") == task_id:
                            src = dict(t); break
                    if src: break
                if not src:
                    raise ValueError("找不到任务")
                src["task_id"] = ""
                src["state"] = ""
                wait_queue.put(src)
            finally:
                task_lock.release()
            log("Web 重新提交了任务 " + task_id, master.config.config["server_log_path"])
            return _json_ok("")
        except Exception as e:
            return _json_err(e)

    @app.route("/api/task/stop", methods=["POST"])
    @require_login
    def api_task_stop():
        try:
            task_id = request.get_json(force=True).get("task_id")
            exists = any(t.get("task_id") == task_id for t in exec_task)
            if not exists:
                raise ValueError("任务不在执行区")
            stop_lock.acquire()
            try:
                if task_id not in stop_task:
                    stop_task.append(task_id)
            finally:
                stop_lock.release()
            log("Web 请求中止任务 " + task_id, master.config.config["server_log_path"])
            return _json_ok("")
        except Exception as e:
            return _json_err(e)

    def get_task_log_path(task_id):
        # 与 PyQt 客户端保持一致：客户端也是先向 master 请求 get_train_log_path，
        # 再对返回路径执行 tail/cat。不要在 Web 端自行猜测日志路径。
        try:
            info = call_process("get_train_log_path", task_id)
            if isinstance(info, str) and info:
                return info
        except Exception:
            pass
        return os.path.join(root_dir(), master.config.config.get("train_log_path", ""), task_id + ".log")

    def find_task_status(task_id):
        terminal_states = {"accp", "err", "offline_error", "term"}
        task_lock.acquire()
        try:
            for area, items in (("wait", wait_task), ("exec", exec_task), ("hist", reversed(hist_task))):
                for t in items:
                    if t.get("task_id") == task_id:
                        state = t.get("state", "")
                        return {
                            "area": area,
                            "state": state,
                            "state_text": STATE_TEXT.get(state, state),
                            "terminal": area == "hist" or state in terminal_states,
                        }
        finally:
            task_lock.release()
        return {"area": "unknown", "state": "unknown", "state_text": "未知/未在任务队列中", "terminal": False}

    @app.route("/api/task/log/<task_id>")
    @require_login
    def api_task_log(task_id):
        try:
            path = get_task_log_path(task_id)
            status = find_task_status(task_id)
            if not os.path.exists(path):
                content = "任务日志不存在或任务尚未产生日志。\n日志路径：%s" % path
            else:
                content = read_tail(path, int(request.args.get("bytes", 1024 * 1024)))
            return _json_ok({
                "content": content,
                "path": path,
                "area": status.get("area"),
                "state": status.get("state"),
                "state_text": status.get("state_text"),
                "terminal": bool(status.get("terminal")),
            })
        except Exception as e:
            return _json_err(e)

    def sse_event(event, data):
        payload = json.dumps(data, ensure_ascii=False)
        return "event: %s\ndata: %s\n\n" % (event, payload)

    def read_tail_lines(path, line_count=100, max_bytes=1024 * 1024):
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
        lines = data.splitlines(True)
        if len(lines) > line_count:
            data = b"".join(lines[-line_count:])
        return data.decode("utf-8", "ignore")

    @app.route("/api/task/log-stream/<task_id>")
    @require_login
    def api_task_log_stream(task_id):
        # Web 端等价于客户端 log_thread 的 `tail -n 100 -f <log_path>`，
        # 但因为 Web 已经运行在 master 上，所以不再 SSH 回 master，而是直接跟随本地日志文件。
        path = get_task_log_path(task_id)

        @stream_with_context
        def generate():
            import time as _time
            yielded_missing = False
            f = None
            pos = 0
            last_heartbeat = _time.time()
            try:
                while True:
                    if f is None:
                        if not os.path.exists(path):
                            if not yielded_missing:
                                yield sse_event("chunk", "任务日志不存在或任务尚未产生日志，正在等待日志文件生成...\n")
                                yielded_missing = True
                            _time.sleep(1.0)
                            continue
                        try:
                            initial = read_tail_lines(path, line_count=100, max_bytes=1024 * 1024)
                            if initial:
                                yield sse_event("chunk", initial)
                            f = open(path, "rb")
                            f.seek(0, os.SEEK_END)
                            pos = f.tell()
                            yielded_missing = False
                        except Exception as e:
                            yield sse_event("error_msg", str(e))
                            _time.sleep(1.0)
                            continue

                    chunk = f.read(65536)
                    if chunk:
                        pos = f.tell()
                        yield sse_event("chunk", chunk.decode("utf-8", "ignore"))
                        continue

                    # 日志轮转或被清空时，重新从文件开头跟随。
                    try:
                        cur_size = os.path.getsize(path)
                        if cur_size < pos:
                            try:
                                f.close()
                            except Exception:
                                pass
                            f = open(path, "rb")
                            pos = 0
                            yield sse_event("chunk", "\n[日志文件已被截断或轮转，重新跟随]\n")
                            continue
                    except Exception:
                        pass

                    now = _time.time()
                    if now - last_heartbeat >= 15:
                        yield sse_event("heartbeat", "")
                        last_heartbeat = now
                    _time.sleep(0.5)
            except GeneratorExit:
                pass
            finally:
                if f is not None:
                    try:
                        f.close()
                    except Exception:
                        pass

        return Response(generate(), mimetype="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })

    def read_tail(path, max_bytes):
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
        return data.decode("utf-8", "ignore")

    @app.route("/api/server-log")
    @require_login
    def api_server_log():
        try:
            path = master.config.config.get("server_log_path")
            if not path or not os.path.exists(path):
                return _json_ok("服务端日志不存在。")
            return _json_ok(read_tail(path, int(request.args.get("bytes", 256 * 1024))))
        except Exception as e:
            return _json_err(e)

    @app.route("/api/envs")
    @require_login
    def api_envs():
        return _json_ok(get_envs())

    @app.route("/api/env/action", methods=["POST"])
    @require_login
    def api_env_action():
        try:
            data = request.get_json(force=True)
            action = data.get("action")
            env = data.get("env")
            if action not in ("test_env", "py_v", "cuda_pt", "cuda_tf", "pkgs"):
                raise ValueError("不支持的操作")
            return _json_ok(extract_msg_from_process_info(call_process(action, env)))
        except Exception as e:
            return _json_err(e)

    @app.route("/api/fs/list")
    @require_login
    def api_fs_list():
        try:
            user_path = request.args.get("path", "/") or "/"
            if user_path == "/":
                return _json_ok({"path": "/", "folders": visible_roots(), "files": []})
            abs_path = resolve_path(user_path, True)
            if not os.path.isdir(abs_path):
                raise ValueError("不是文件夹")
            folders, files = [], []
            for item in sorted(os.scandir(abs_path), key=lambda x: (not x.is_dir(), x.name.lower())):
                if item.name.startswith("."):
                    continue
                if item.is_dir(): folders.append(item.name)
                else: files.append(item.name)
            return _json_ok({"path": to_user_path(abs_path), "folders": folders, "files": files})
        except Exception as e:
            return _json_err(e)

    @app.route("/api/fs/read")
    @require_login
    def api_fs_read():
        try:
            abs_path = resolve_path(request.args.get("path"), True)
            if not os.path.isfile(abs_path):
                raise ValueError("不是文件")
            max_size = int(request.args.get("max", 5 * 1024 * 1024))
            if os.path.getsize(abs_path) > max_size:
                raise ValueError("文件过大，网页端默认只预览 5MB 以内的文本文件")
            with open(abs_path, "rb") as f:
                content = f.read().decode("utf-8", "ignore")
            return _json_ok({"path": to_user_path(abs_path), "content": content})
        except Exception as e:
            return _json_err(e)

    @app.route("/api/fs/write", methods=["POST"])
    @require_login
    def api_fs_write():
        try:
            data = request.get_json(force=True)
            abs_path = resolve_path(data.get("path"), False)
            parent = os.path.dirname(abs_path)
            if not os.path.isdir(parent):
                raise ValueError("父目录不存在")
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(data.get("content", ""))
            return _json_ok("")
        except Exception as e:
            return _json_err(e)

    @app.route("/api/fs/op", methods=["POST"])
    @require_login
    def api_fs_op():
        try:
            data = request.get_json(force=True)
            op = data.get("op")
            folder = resolve_path(data.get("path"), True)
            name = os.path.basename(data.get("name", ""))
            if not name:
                raise ValueError("名称不能为空")
            target = os.path.join(folder, name)
            resolve_path(to_user_path(target), False)
            if op == "mkdir":
                os.makedirs(target, exist_ok=False)
            elif op == "touch":
                open(target, "a").close()
            elif op == "rename":
                old_name = os.path.basename(data.get("old_name", ""))
                src = os.path.join(folder, old_name)
                resolve_path(to_user_path(src), True)
                os.rename(src, target)
            elif op in ("mv", "cp"):
                src = resolve_path(data.get("src"), True)
                if op == "mv": shutil.move(src, target)
                else:
                    if os.path.isdir(src): shutil.copytree(src, target)
                    else: shutil.copy2(src, target)
            else:
                raise ValueError("不支持的文件操作")
            return _json_ok("")
        except Exception as e:
            return _json_err(e)

    @app.route("/api/fs/delete", methods=["POST"])
    @require_login
    def api_fs_delete():
        try:
            abs_path = resolve_path(request.get_json(force=True).get("path"), True)
            if os.path.isdir(abs_path): shutil.rmtree(abs_path)
            else: os.remove(abs_path)
            return _json_ok("")
        except Exception as e:
            return _json_err(e)

    @app.route("/api/fs/upload", methods=["POST"])
    @require_login
    def api_fs_upload():
        try:
            folder = resolve_path(request.form.get("path", "/"), True)
            if not os.path.isdir(folder):
                raise ValueError("上传目标不是文件夹")
            files = request.files.getlist("files")
            if not files:
                raise ValueError("没有选择文件")
            saved = []
            for f in files:
                name = os.path.basename(f.filename)
                if not name:
                    continue
                target = os.path.join(folder, name)
                resolve_path(to_user_path(target), False)
                f.save(target)
                saved.append(name)
            return _json_ok(saved)
        except Exception as e:
            return _json_err(e)

    @app.route("/api/fs/download")
    @require_login
    def api_fs_download():
        try:
            abs_path = resolve_path(request.args.get("path"), True)
            if os.path.isdir(abs_path):
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
                tmp.close()
                with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
                    base = os.path.dirname(abs_path)
                    for root_, _, files in os.walk(abs_path):
                        for fn in files:
                            p = os.path.join(root_, fn)
                            zf.write(p, os.path.relpath(p, base))
                return send_file(tmp.name, as_attachment=True, download_name=os.path.basename(abs_path.rstrip(os.sep)) + ".zip")
            return send_file(abs_path, as_attachment=True, download_name=os.path.basename(abs_path))
        except Exception as e:
            return _json_err(e)

    @app.route("/api/shell", methods=["POST"])
    @require_login
    def api_shell():
        if not terminal_enabled:
            return _json_err("命令行功能已关闭")
        try:
            data = request.get_json(force=True)
            cmd = data.get("cmd", "")
            if not cmd.strip():
                raise ValueError("命令不能为空")
            term = get_terminal_session()
            if cmd.strip() == "__close__":
                term.close()
                return _json_ok({"stdout": "终端会话已关闭\n", "stderr": "", "returncode": None})
            # Drain stale bootstrap output first, then send the user's command.
            _ = term.read()
            term.send(cmd)
            # Give ordinary short commands a brief chance to produce output.
            time_to_wait = min(float(web_conf.get("shell_initial_wait", 0.25)), 2.0)
            import time as _time
            _time.sleep(time_to_wait)
            return _json_ok({"stdout": term.read(), "stderr": "", "returncode": None})
        except Exception as e:
            return _json_err(e)

    # 管理员后台：后台页面与 API 单独放在 web_admin.py，主文件只做注册。
    try:
        from web_admin import register_admin_routes
        register_admin_routes(app, master, users, require_login, _json_ok, _json_err, log)
    except Exception as e:
        try:
            log("Web管理员后台加载失败: "+str(e), master.config.config.get("server_log_path", "server_log.log"))
        except Exception:
            print("Web管理员后台加载失败:", e)

    return app


def run_web_app(master):
    web_conf = master.config.config.get("web_info", {})
    if not web_conf.get("enabled", True):
        return
    host = web_conf.get("host", "0.0.0.0")
    port = int(web_conf.get("port", 8080))
    app = create_web_app(master)
    log("Web 控制台启动: http://%s:%s" % (host, port), master.config.config["server_log_path"])
    app.run(host=host, port=port, threaded=True, use_reloader=False)
