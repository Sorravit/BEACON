
// AI Assistant Web UI - JavaScript (multi-session)

// ── DOM refs ──────────────────────────────────────────────────────────────────
const messagesEl   = document.getElementById('messages');
const textarea     = document.getElementById('user-input');
const sendBtn      = document.getElementById('send-btn');
const statusDot    = document.getElementById('status-dot');
const statusText   = document.getElementById('status-text');
const toast        = document.getElementById('toast');
const chatTitleEl  = document.getElementById('chat-title');
const sessionsList = document.getElementById('sessions-list');
const sidebar      = document.getElementById('sidebar');
const sidebarToggle   = document.getElementById('sidebar-toggle');
const sidebarOpenBtn  = document.getElementById('sidebar-open-btn');

// ── State ─────────────────────────────────────────────────────────────────────
const _streamingSet  = new Set();   // set of session IDs currently streaming
let currentSessionId = null;
let _renameTarget    = null;
let taskLogStreams   = {};
let _dragInit        = false;
let _currentReader   = null;   // active SSE reader so we can cancel client-side too
let _activityPollTimer = null; // activity polling interval
// Per-session streaming check
function isStreamingSession(sid) { return _streamingSet.has(sid); }

// ── Stop button + activity bar refs ───────────────────────────────────────────
const stopBtn      = document.getElementById('stop-btn');
const activityText = document.getElementById('activity-text');

function showStopBtn()  { if (stopBtn) stopBtn.style.display = 'flex'; }
function hideStopBtn()  { if (stopBtn) stopBtn.style.display = 'none'; }
function setActivity(t) { if (activityText) activityText.textContent = t || ''; }

async function stopAI() {
  if (!currentSessionId) return;
  if (_currentReader) { try { _currentReader.cancel(); } catch(e) {} _currentReader = null; }
  try { await fetch('/chat/stop/' + currentSessionId, { method: 'POST' }); } catch(e) {}
  hideStopBtn();
  setActivity('');
  stopActivityPoll();
  _streamingSet.delete(currentSessionId);
  sendBtn.disabled = false;
  setStatus('ready', 'Stopped');
}

function startActivityPoll(sid) {
  stopActivityPoll();
  _activityPollTimer = setInterval(async () => {
    if (!_streamingSet.has(sid)) { stopActivityPoll(); return; }
    try {
      const r = await fetch('/chat/status/' + sid);
      const d = await r.json();
      if (d.running && d.activity) setActivity('\u2699 ' + d.activity);
      else if (!d.running) stopActivityPoll();
    } catch(e) {}
  }, 800);
}

function stopActivityPoll() {
  if (_activityPollTimer) { clearInterval(_activityPollTimer); _activityPollTimer = null; }
}

// ── File attachment state ─────────────────────────────────────────────────────
let _attachedFiles = [];  // [{name, size, type, content}]
const filePreviewsEl = document.getElementById('file-previews');
const dropOverlay    = document.getElementById('drop-overlay');

function fmtSize(b){return b<1024?b+'B':b<1048576?(b/1024).toFixed(1)+'KB':(b/1048576).toFixed(1)+'MB';}

function addAttachedFile(name, size, type, content) {
  _attachedFiles.push({name, size, type, content});
  renderFilePreviews();
}

function removeAttachedFile(idx) {
  _attachedFiles.splice(idx, 1);
  renderFilePreviews();
}

function renderFilePreviews() {
  if (!filePreviewsEl) return;
  filePreviewsEl.innerHTML = '';
  _attachedFiles.forEach((f, i) => {
    const chip = document.createElement('div');
    const isImg = f.type.startsWith('image/');
    chip.className = 'file-chip' + (isImg ? ' image-chip' : '');
    if (isImg) {
      chip.innerHTML = '<img src="'+escHtml(f.content)+'" alt="'+escHtml(f.name)+'">';
    } else {
      const icon = f.type.includes('pdf') ? '📄' : f.type.includes('zip') ? '📦' : f.type.startsWith('text') ? '📝' : '📎';
      chip.innerHTML = '<span class="file-chip-icon">'+icon+'</span>';
    }
    chip.innerHTML += '<span class="file-chip-name" title="'+escHtml(f.name)+'">'+escHtml(f.name)+'</span>'
      +'<span class="file-chip-size">'+fmtSize(f.size)+'</span>'
      +'<button class="file-chip-remove" title="Remove" onclick="removeAttachedFile('+i+')">✕</button>';
    filePreviewsEl.appendChild(chip);
  });
}

function readFileAsAttachment(file) {
  return new Promise((resolve) => {
    const reader = new FileReader();
    const isImg  = file.type.startsWith('image/');
    const isText = file.type.startsWith('text/') || /\.(txt|md|json|csv|xml|yaml|yml|js|ts|py|sh|css|html|log)$/i.test(file.name);
    reader.onload = e => resolve({
      name: file.name, size: file.size, type: file.type,
      content: e.target.result,
      isImg, isText
    });
    if (isImg) reader.readAsDataURL(file);
    else reader.readAsText(file);
  });
}

async function processDroppedFiles(files) {
  for (const file of files) {
    if (file.size > 50 * 1024 * 1024) { showToast(file.name + ' is too large (max 50 MB)'); continue; }
    // Upload file to server so AI knows the path
    let serverPath = null;
    try {
      const fd = new FormData();
      fd.append('file', file);
      const res = await fetch('/upload', { method: 'POST', body: fd });
      if (res.ok) {
        const d = await res.json();
        serverPath = d.path;
      }
    } catch(e) { console.warn('Upload failed, falling back to inline content', e); }

    // Also read content for text/code files so AI sees it immediately
    let content = null;
    const isImg = file.type.startsWith('image/');
    const isText = file.type.startsWith('text/') || /\.(txt|md|json|csv|xml|yaml|yml|js|ts|py|sh|css|html|log|ini|toml|conf|sql)$/i.test(file.name);
    if (isImg) {
      const att = await readFileAsAttachment(file);
      content = att.content; // data URL for preview
    } else if (isText && file.size <= 2 * 1024 * 1024) {
      const att = await readFileAsAttachment(file);
      content = att.content; // full text
    }

    _attachedFiles.push({ name: file.name, size: file.size, type: file.type, content, serverPath, isImg, isText });
    renderFilePreviews();
  }
}

// ── Drag-and-drop event handlers (document-level so whole page is a drop zone) ─
let _dragCounter = 0;

document.addEventListener('dragenter', e => {
  if (!e.dataTransfer || !e.dataTransfer.types.includes('Files')) return;
  e.preventDefault(); _dragCounter++;
  if (_dragCounter === 1 && dropOverlay) dropOverlay.classList.add('active');
});
document.addEventListener('dragleave', e => {
  // Only count leaves that exit the browser window entirely
  if (e.relatedTarget) return;
  _dragCounter = 0;
  if (dropOverlay) dropOverlay.classList.remove('active');
});
document.addEventListener('dragover', e => {
  if (!e.dataTransfer || !e.dataTransfer.types.includes('Files')) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = 'copy';
});
document.addEventListener('drop', async e => {
  e.preventDefault(); _dragCounter = 0;
  if (dropOverlay) dropOverlay.classList.remove('active');
  const files = Array.from(e.dataTransfer.files);
  if (files.length) await processDroppedFiles(files);
});

// Also allow paste of files (Ctrl+V image paste)
document.addEventListener('paste', async e => {
  const items = Array.from(e.clipboardData?.items || []);
  const files = items.filter(i => i.kind === 'file').map(i => i.getAsFile()).filter(Boolean);
  if (files.length) { e.preventDefault(); await processDroppedFiles(files); }
});

// ── Markdown renderer ─────────────────────────────────────────────────────────
function copyCode(btn) {
  const pre = btn.closest('pre');
  const code = pre ? pre.querySelector('code') : null;
  if (!code) return;
  navigator.clipboard.writeText(code.innerText).then(() => {
    btn.textContent = '✓ Copied';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 1800);
  }).catch(() => {
    // Fallback for older browsers
    const sel = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(code);
    sel.removeAllRanges();
    sel.addRange(range);
    document.execCommand('copy');
    sel.removeAllRanges();
    btn.textContent = '✓ Copied';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 1800);
  });
}

function renderMarkdown(text) {
  let html = text
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/```(\w*)\n?([\s\S]*?)```/g,(_,lang,code)=>`<pre><button class="copy-btn" onclick="copyCode(this)">Copy</button><code class="lang-${lang}">${code.trim()}</code></pre>`)
    .replace(/`([^`]+)`/g,'<code>$1</code>')
    .replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>')
    .replace(/\*(.+?)\*/g,'<em>$1</em>')
    .replace(/^### (.+)$/gm,'<h3>$1</h3>')
    .replace(/^## (.+)$/gm,'<h2>$1</h2>')
    .replace(/^# (.+)$/gm,'<h1>$1</h1>')
    .replace(/^> (.+)$/gm,'<blockquote>$1</blockquote>')
    .replace(/^[\-\*] (.+)$/gm,'<li>$1</li>')
    .replace(/^\d+\. (.+)$/gm,'<li>$1</li>')
    .replace(/(<li>[\s\S]*?<\/li>)(\n<li>[\s\S]*?<\/li>)*/g,m=>`<ul>${m}</ul>`)
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/^---$/gm,'<hr>')
    .replace(/\n\n+/g,'</p><p>')
    .replace(/\n/g,'<br>');
  if (!/^<(h[1-6]|ul|ol|pre|blockquote|hr)/.test(html)) html='<p>'+html+'</p>';
  return html;
}

function escHtml(s){
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function setStatus(state, text) {
  statusDot.className = 'status-dot' + (state !== 'ready' ? ' ' + state : '');
  statusText.textContent = text;
}
function scrollToBottom() { messagesEl.scrollTop = messagesEl.scrollHeight; }
function showToast(msg) {
  toast.textContent = msg;
  toast.classList.add('show');
  setTimeout(() => toast.classList.remove('show'), 2500);
}

function fmtTs(ts) {
  if (!ts) return '';
  try {
    const d = new Date(ts);
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    const time = d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
    return sameDay ? time : d.toLocaleDateString([], {month:'short', day:'numeric'}) + ' ' + time;
  } catch(e) { return ''; }
}

function createMessage(role, ts) {
  const row    = document.createElement('div');
  row.className = `msg ${role}`;
  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  avatar.textContent = role === 'user' ? '👤' : '🤖';
  const wrap = document.createElement('div');
  wrap.style.cssText = 'display:flex;flex-direction:column;max-width:76%';
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.style.maxWidth = '100%';
  wrap.appendChild(bubble);
  if (ts) {
    const tsEl = document.createElement('div');
    tsEl.className = 'msg-ts';
    tsEl.textContent = fmtTs(ts);
    wrap.appendChild(tsEl);
  }
  row.appendChild(avatar);
  row.appendChild(wrap);
  messagesEl.appendChild(row);
  scrollToBottom();
  return bubble;
}

// ── Sidebar collapse ──────────────────────────────────────────────────────────
function openSidebar() {
  sidebar.classList.remove('collapsed');
  if (sidebarOpenBtn) sidebarOpenBtn.style.display = 'none';
}

function closeSidebarOnMobile() {
  if (window.innerWidth <= 700) {
    sidebar.classList.add('collapsed');
    if (sidebarOpenBtn) sidebarOpenBtn.style.display = 'flex';
  }
}

if (sidebarToggle) {
  sidebarToggle.addEventListener('click', () => {
    sidebar.classList.add('collapsed');
    if (sidebarOpenBtn) sidebarOpenBtn.style.display = 'flex';
  });
}

// ── Session sidebar ───────────────────────────────────────────────────────────
async function loadSessions() {
  try {
    const res  = await fetch('/sessions');
    const data = await res.json();
    renderSessionsList(data.sessions || []);
  } catch(e) { console.error('Failed to load sessions', e); }
}

function renderSessionsList(sessions) {
  sessionsList.innerHTML = '';
  if (!sessions.length) {
    sessionsList.innerHTML = '<div class="sessions-empty">No chats yet</div>';
    return;
  }
  sessions.forEach(s => {
    const item   = document.createElement('div');
    item.className = 'session-item' + (s.id === currentSessionId ? ' active' : '');
    item.dataset.id = s.id;

    const label  = document.createElement('div');
    label.className = 'session-label';
    const prefix = s.running ? '<span class="session-running-dot" title="AI working\u2026"></span>' : '';
    label.innerHTML = prefix + escHtml(s.title || 'New Chat');
    label.title = (s.running ? '\u2699 Working\u2026 ' : '') + (s.title || 'New Chat');
    label.onclick = () => { switchSession(s.id); closeSidebarOnMobile(); };

    const actions = document.createElement('div');
    actions.className = 'session-actions';

    const renameBtn = document.createElement('button');
    renameBtn.className = 'session-action-btn';
    renameBtn.title = 'Rename';
    renameBtn.textContent = '✏';
    renameBtn.onclick = e => { e.stopPropagation(); openRenameModal(s.id, s.title); };

    const deleteBtn = document.createElement('button');
    deleteBtn.className = 'session-action-btn danger';
    deleteBtn.title = 'Delete';
    deleteBtn.textContent = '🗑';
    deleteBtn.onclick = e => { e.stopPropagation(); deleteSession(s.id); };

    actions.appendChild(renameBtn);
    actions.appendChild(deleteBtn);
    item.appendChild(label);
    item.appendChild(actions);
    sessionsList.appendChild(item);
  });
}

async function newChat() {
  try {
    const res  = await fetch('/sessions', {method:'POST'});
    const data = await res.json();
    await loadSessions();
    await switchSession(data.id);
    closeSidebarOnMobile();
  } catch(e) { showToast('Failed to create chat'); }
}

async function switchSession(id) {
  if (id === currentSessionId) return;
  currentSessionId = id;
  document.querySelectorAll('.session-item').forEach(el => {
    el.classList.toggle('active', el.dataset.id === id);
  });
  sendBtn.disabled = isStreamingSession(id);
  if (!isStreamingSession(id)) setStatus('ready', 'Ready');
  chatTitleEl.title = 'Click to rename this chat';
  try {
    const res  = await fetch(`/sessions/${id}`);
    const data = await res.json();
    chatTitleEl.textContent = data.title || 'New Chat';
    messagesEl.innerHTML = '';
    if (data.messages && data.messages.length > 0) {
      data.messages.forEach(msg => {
        if (msg.role === 'user') { const b=createMessage('user', msg.ts); b.innerHTML=renderMarkdown(msg.content||''); }
        else if (msg.role === 'assistant') { const b=createMessage('ai', msg.ts); b.innerHTML=renderMarkdown(msg.content||''); }
      });
      scrollToBottom();
    } else {
      const es = document.createElement('div');
      es.className = 'empty-state'; es.id = 'empty-state-active';
      es.innerHTML = `<div class="empty-icon">🤖</div><div class="empty-title">Big's Personal AI Assistant</div>
        <div class="empty-subtitle">Ask me anything.</div>
        <div class="suggestions">
          <span class="suggestion-chip" onclick="useSuggestion('What time is it?')">🕐 What time is it?</span>
          <span class="suggestion-chip" onclick="useSuggestion('List files in current directory')">📁 List files</span>
          <span class="suggestion-chip" onclick="useSuggestion('Search for latest AI news')">🔍 Latest AI news</span>
        </div>`;
      messagesEl.appendChild(es);
    }
    if (data.running && !isStreamingSession(id)) reconnectToSession(id);
  } catch(e) { showToast('Failed to load session'); }
}

// ── Reconnect to a session whose agent is still running in the background ────
async function reconnectToSession(id) {
  if (isStreamingSession(id)) return;
  _streamingSet.add(id);
  if (id === currentSessionId) {
    sendBtn.disabled = true; showStopBtn(); startActivityPoll(id);
    setStatus('thinking', 'Working\u2026');
  }
  const ab = createMessage('ai');
  const tBl=document.createElement('div');tBl.className='thinking-block';
  const tHd=document.createElement('div');tHd.className='thinking-header';
  const tIc=document.createElement('span');tIc.className='thinking-icon';
  const sp=document.createElement('span');sp.className='thinking-spinner';tIc.appendChild(sp);
  const tLb=document.createElement('span');tLb.className='thinking-label';tLb.textContent='\u2699 Reconnecting\u2026';
  const tTg=document.createElement('span');tTg.className='thinking-toggle';tTg.textContent=' \u25bc';
  tHd.appendChild(tIc);tHd.appendChild(tLb);tHd.appendChild(tTg);
  const tBo=document.createElement('div');tBo.className='thinking-body open';
  tHd.addEventListener('click',()=>{tBo.classList.toggle('open');tTg.textContent=tBo.classList.contains('open')?' \u25b2':' \u25bc';});
  tBl.appendChild(tHd);tBl.appendChild(tBo);ab.appendChild(tBl);
  const tn=document.createElement('div');ab.appendChild(tn);
  let acc='',steps=0;
  function atl(txt,cls){const l=document.createElement('div');l.className='thinking-line '+(cls||'');l.textContent=txt;tBo.appendChild(l);tBo.scrollTop=tBo.scrollHeight;scrollToBottom();}
  try {
    const resp=await fetch('/chat/reconnect/'+id);
    if(!resp.ok)throw new Error('HTTP '+resp.status);
    const reader=resp.body.getReader();
    if(id===currentSessionId)_currentReader=reader;
    const dec=new TextDecoder();let buf='';
    while(true){
      const{done,value}=await reader.read();if(done)break;
      buf+=dec.decode(value,{stream:true});
      const lines=buf.split('\n');buf=lines.pop();
      for(const line of lines){
        if(!line.trim()||line.trim().startsWith(':'))continue;
        let ev;try{ev=JSON.parse(line.trim());}catch{continue;}
        if(ev.type==='tool'){steps++;if(id===currentSessionId)setStatus('thinking','Running '+ev.name+'\u2026');tLb.textContent='\u2699 Step '+steps+': '+ev.name;atl('\uD83D\uDD27 '+ev.name+(ev.args?'('+ev.args+')':'()'),'tool-call');}
        else if(ev.type==='result'){(ev.content||'').split('\n').forEach(l=>{if(l.trim())atl('   '+l,'tool-result');});if(id===currentSessionId)setStatus('thinking','Processing\u2026');}
        else if(ev.type==='token'){acc+=ev.content||'';tn.innerHTML=renderMarkdown(acc);scrollToBottom();if(id===currentSessionId)setStatus('thinking','Responding\u2026');}
        else if(ev.type==='done'){tBo.classList.remove('open');tIc.innerHTML=steps>0?'<span style="color:var(--green);font-size:13px">\u2713</span>':'<span style="color:var(--muted);font-size:13px">\u2014</span>';tLb.textContent=steps>0?('\u2699 '+steps+' step'+(steps>1?'s':'')+' completed'):'\u2699 No tools used';tn.innerHTML=renderMarkdown(acc);scrollToBottom();refreshTasksBadge();await loadSessions();if(id===currentSessionId){const ud=await(await fetch('/sessions/'+id)).json();chatTitleEl.textContent=ud.title||'New Chat';}}
        else if(ev.type==='stopped'){tBo.classList.remove('open');tIc.innerHTML='<span style="color:var(--yellow);font-size:13px">\u23F9</span>';tLb.textContent='\u2699 Stopped';if(acc)tn.innerHTML=renderMarkdown(acc);scrollToBottom();}
        else if(ev.type==='error'){atl('\u26a0 '+(ev.content||'Unknown error'),'error');tBo.classList.add('open');if(id===currentSessionId)setStatus('error','Error');}
      }
    }
  } catch(err) {
    if(err.name!=='AbortError'){const ed=document.createElement('div');ed.style.cssText='color:#f87171;font-size:13px';ed.textContent='\u26a0 '+err.message;ab.appendChild(ed);}
  } finally {
    if(id===currentSessionId){_currentReader=null;hideStopBtn();stopActivityPoll();setActivity('');setStatus('ready','Ready');}
    _streamingSet.delete(id);sendBtn.disabled=false;scrollToBottom();
  }
}

async function deleteSession(id) {
  if (!confirm('Delete this chat? This cannot be undone.')) return;
  try {
    await fetch(`/sessions/${id}`, {method:'DELETE'});
    if (id === currentSessionId) { currentSessionId=null; chatTitleEl.textContent="Big's Personal AI Assistant"; messagesEl.innerHTML=''; }
    await loadSessions();
    if (!currentSessionId) {
      const res=await fetch('/sessions'); const data=await res.json();
      if (data.sessions&&data.sessions.length>0) await switchSession(data.sessions[0].id);
      else await newChat();
    }
  } catch(e) { showToast('Failed to delete chat'); }
}

function openRenameModal(id,t){
  _renameTarget=id;
  const inp=document.getElementById('rename-input');
  if(inp)inp.value=t||'';
  const ov=document.getElementById('rename-overlay');const mo=document.getElementById('rename-modal');
  if(ov)ov.style.display='block';if(mo)mo.style.display='block';
  setTimeout(()=>{if(inp)inp.focus();},50);
}
function closeRenameModal(){
  _renameTarget=null;
  const ov=document.getElementById('rename-overlay');const mo=document.getElementById('rename-modal');
  if(ov)ov.style.display='none';if(mo)mo.style.display='none';
}
async function confirmRename(){
  if(!_renameTarget)return;
  const inp=document.getElementById('rename-input');
  const title=inp?inp.value.trim():'';if(!title)return;
  try{
    await fetch('/sessions/'+_renameTarget+'/rename',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({title})});
    if(_renameTarget===currentSessionId)chatTitleEl.textContent=title;
    closeRenameModal();await loadSessions();
  }catch(e){showToast('Failed to rename');}
}
const _ri=document.getElementById('rename-input');
if(_ri)_ri.addEventListener('keydown',e=>{if(e.key==='Enter')confirmRename();if(e.key==='Escape')closeRenameModal();});

function useSuggestion(t){textarea.value=t;autoResize();textarea.focus();}
function autoResize(){textarea.style.height='auto';textarea.style.height=Math.min(textarea.scrollHeight,160)+'px';}

async function clearChat(){
  if(!currentSessionId)return;
  await fetch('/chat/clear',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:'',session_id:currentSessionId})});
  messagesEl.innerHTML='';
  const es=document.createElement('div');es.className='empty-state';es.id='empty-state-active';
  es.innerHTML='<div class="empty-icon">🤖</div><div class="empty-title">Big\'s Personal AI Assistant</div><div class="empty-subtitle">Ask me anything.</div>';
  messagesEl.appendChild(es);showToast('Conversation cleared');
}

async function sendMessage(){
  const text=textarea.value.trim();
  if((!text && _attachedFiles.length===0)||isStreamingSession(currentSessionId))return;
  // Auto-create a session if none is active
  if(!currentSessionId){
    try{
      const r=await fetch('/sessions',{method:'POST'});
      const d=await r.json();
      currentSessionId=d.id;
      chatTitleEl.textContent='New Chat';
      await loadSessions();
    }catch(e){showToast('Could not create session');return;}
  }
  document.getElementById('empty-state-active')?.remove();
  const eo=document.getElementById('empty-state');if(eo)eo.style.display='none';
  // Build full message including any attached files
  let fullText = text;
  if (_attachedFiles.length > 0) {
    const parts = [];
    for (const f of _attachedFiles) {
      if (f.isImg) {
        const pathInfo = f.serverPath ? ' (saved to: '+f.serverPath+')' : '';
        parts.push('[Attached image: '+f.name+' ('+fmtSize(f.size)+')'+pathInfo+']');
      } else if (f.serverPath) {
        // File is on disk — instruct AI to read it
        const preview = (f.content && typeof f.content === 'string' && f.content.length > 0)
          ? '\nFirst 500 chars preview:\n```\n'+f.content.slice(0,500)+(f.content.length>500?'\n...(truncated)':'')+'\n```'
          : '';
        parts.push('I have attached the file "'+f.name+'" ('+fmtSize(f.size)+'). It has been saved to the server at path: '+f.serverPath+'\nPlease use read_file("'+f.serverPath+'") to read its full contents and then respond based on what is in the file.'+preview);
      } else {
        // Upload failed — include what we have
        const preview = (f.content && typeof f.content === 'string')
          ? '\n```\n'+f.content.slice(0,2000)+(f.content.length>2000?'\n...(truncated)':'')+'\n```'
          : '';
        parts.push('[Attached file: '+f.name+' ('+fmtSize(f.size)+')'+preview+']');
      }
    }
    if (text) parts.push(text);
    fullText = parts.join('\n\n');
  }
  const _msgNow = new Date().toISOString();
  const ub=createMessage('user', _msgNow);ub.innerHTML=renderMarkdown(fullText||text);
  textarea.value='';textarea.style.height='auto';
  _attachedFiles=[];renderFilePreviews();
  _streamingSet.add(currentSessionId);sendBtn.disabled=true;setStatus('thinking','Thinking\u2026');
  showStopBtn();startActivityPoll(currentSessionId);

  const ab=createMessage('ai', _msgNow);
  const tBl=document.createElement('div');tBl.className='thinking-block';
  const tHd=document.createElement('div');tHd.className='thinking-header';
  const tIc=document.createElement('span');tIc.className='thinking-icon';
  const sp=document.createElement('span');sp.className='thinking-spinner';tIc.appendChild(sp);
  const tLb=document.createElement('span');tLb.className='thinking-label';tLb.textContent='\u2699 Working\u2026';
  const tTg=document.createElement('span');tTg.className='thinking-toggle';tTg.textContent=' \u25bc';
  tHd.appendChild(tIc);tHd.appendChild(tLb);tHd.appendChild(tTg);
  const tBo=document.createElement('div');tBo.className='thinking-body open';
  tHd.addEventListener('click',()=>{tBo.classList.toggle('open');tTg.textContent=tBo.classList.contains('open')?' \u25b2':' \u25bc';});
  tBl.appendChild(tHd);tBl.appendChild(tBo);ab.appendChild(tBl);
  const tn=document.createElement('div');ab.appendChild(tn);
  let acc='',steps=0;

  function atl(txt,cls){
    const l=document.createElement('div');l.className='thinking-line '+(cls||'');l.textContent=txt;
    tBo.appendChild(l);tBo.scrollTop=tBo.scrollHeight;scrollToBottom();
  }

  try{
    const resp=await fetch('/chat/stream',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:fullText||text,session_id:currentSessionId})});
    if(!resp.ok)throw new Error('HTTP '+resp.status);
    const reader=resp.body.getReader();_currentReader=reader;const dec=new TextDecoder();let buf='';
    while(true){
      const{done,value}=await reader.read();if(done)break;
      buf+=dec.decode(value,{stream:true});
      const lines=buf.split('\n');buf=lines.pop();
      for(const line of lines){
        if(!line.trim()||line.trim().startsWith(':'))continue;
        let ev;try{ev=JSON.parse(line.trim());}catch{continue;}
        if(ev.type==='tool'){
          steps++;setStatus('thinking','Running '+ev.name+'\u2026');
          tLb.textContent='\u2699 Step '+steps+': '+ev.name;
          atl('\uD83D\uDD27 '+ev.name+(ev.args?'('+ev.args+')':'()'),'tool-call');
          if(ev.name==='delegate_background_task')setTimeout(()=>{openTasksPanel();refreshTasksList();},800);
        }else if(ev.type==='result'){
          (ev.content||'').split('\n').forEach(l=>{if(l.trim())atl('   '+l,'tool-result');});
          setStatus('thinking','Processing\u2026');
        }else if(ev.type==='token'){
          acc+=ev.content||'';tn.innerHTML=renderMarkdown(acc);scrollToBottom();setStatus('thinking','Responding\u2026');
        }else if(ev.type==='done'){
          tBo.classList.remove('open');
          tIc.innerHTML=steps>0?'<span style="color:var(--green);font-size:13px">\u2713</span>':'<span style="color:var(--muted);font-size:13px">\u2014</span>';
          tLb.textContent=steps>0?('\u2699 '+steps+' step'+(steps>1?'s':'')+' completed'):'\u2699 No tools used';
          tn.innerHTML=renderMarkdown(acc);scrollToBottom();refreshTasksBadge();
          await loadSessions();
          const ud=await(await fetch('/sessions/'+currentSessionId)).json();
          chatTitleEl.textContent=ud.title||'New Chat';
        }else if(ev.type==='stopped'){
          tBo.classList.remove('open');
          tIc.innerHTML='<span style="color:var(--yellow);font-size:13px">\u23F9</span>';
          tLb.textContent='\u2699 Stopped by user after '+steps+' step'+(steps!==1?'s':'');
          if(acc)tn.innerHTML=renderMarkdown(acc);
          scrollToBottom();
        }else if(ev.type==='error'){
          atl('\u26a0 '+(ev.content||'Unknown error'),'error');tBo.classList.add('open');setStatus('error','Error');
        }
      }
    }
  }catch(err){
    if(err.name!=='AbortError'){
      const ed=document.createElement('div');ed.style.cssText='color:#f87171;font-size:13px';ed.textContent='\u26a0 Connection error: '+err.message;ab.appendChild(ed);setStatus('error','Error');
    }
  }finally{
    _currentReader=null;
    _streamingSet.delete(currentSessionId);
    sendBtn.disabled=false;
    hideStopBtn();
    stopActivityPoll();
    setActivity('');
    setStatus('ready','Ready');
    scrollToBottom();
  }
}

sendBtn.addEventListener('click',sendMessage);
textarea.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendMessage();}});
textarea.addEventListener('input',autoResize);

function openTasksPanel(){
  const p=document.getElementById('tasks-panel');
  p.classList.add('open');
  // After making visible, convert CSS right:24px → explicit left so drag math works
  if(!p.style.left){
    const r=p.getBoundingClientRect();
    p.style.right='auto';
    p.style.left=r.left+'px';
    p.style.top=r.top+'px';
  }
  refreshTasksList();
  initDrag(p);
}
function closeTasksPanel(){document.getElementById('tasks-panel').classList.remove('open');}

function initDrag(panel){
  if(_dragInit)return;_dragInit=true;
  const hd=panel.querySelector('.tasks-panel-header');if(!hd)return;
  const SNAP=60;let drag=false,sx,sy,sl,st,snapState=null;
  let preW=null,preH=null,preL=null,preT=null;
  const gh=document.createElement('div');
  gh.style.cssText='position:fixed;background:rgba(108,99,255,.15);border:2px solid rgba(108,99,255,.5);border-radius:8px;pointer-events:none;z-index:299;display:none';
  document.body.appendChild(gh);
  function sg(l,t,w,h){gh.style.display='block';gh.style.left=l+'px';gh.style.top=t+'px';gh.style.width=w+'px';gh.style.height=h+'px';}
  function hg(){gh.style.display='none';}
  hd.addEventListener('mousedown',e=>{
    if(e.target.tagName==='BUTTON')return;
    if(['snap-left','snap-right','snap-top'].some(c=>panel.classList.contains(c))){
      panel.classList.remove('snap-left','snap-right','snap-top','snap-bottom');
      panel.style.width=(preW||440)+'px';panel.style.height=(preH||500)+'px';
      panel.style.left=(e.clientX-220)+'px';panel.style.top=(preT||60)+'px';
      preW=preH=preL=preT=null;
    }
    drag=true;const r=panel.getBoundingClientRect();
    sx=e.clientX;sy=e.clientY;sl=r.left;st=r.top;
    panel.style.right='auto';panel.style.left=sl+'px';panel.style.top=st+'px';e.preventDefault();
  });
  document.addEventListener('mousemove',e=>{
    if(!drag)return;
    const W=window.innerWidth,H=window.innerHeight;
    panel.classList.remove('snap-left','snap-right','snap-top','snap-bottom');
    if(e.clientX<SNAP){sg(0,0,W*.5,H);snapState='left';}
    else if(e.clientX>W-SNAP){sg(W*.5,0,W*.5,H);snapState='right';}
    else if(e.clientY<SNAP){sg(0,0,W,H);snapState='top';}
    else{hg();snapState=null;}
    panel.style.left=Math.max(0,Math.min(W-100,sl+e.clientX-sx))+'px';
    panel.style.top=Math.max(0,Math.min(H-60,st+e.clientY-sy))+'px';
  });
  document.addEventListener('mouseup',()=>{
    if(drag&&snapState){
      const W=window.innerWidth,H=window.innerHeight;
      preW=panel.offsetWidth;preH=panel.offsetHeight;
      preL=parseInt(panel.style.left)||0;preT=parseInt(panel.style.top)||0;
      if(snapState==='left'){panel.style.right='auto';panel.style.left='0';panel.style.top='0';panel.style.width=(W*.5)+'px';panel.style.height=H+'px';panel.classList.add('snap-left');}
      else if(snapState==='right'){panel.style.right='auto';panel.style.left=(W*.5)+'px';panel.style.top='0';panel.style.width=(W*.5)+'px';panel.style.height=H+'px';panel.classList.add('snap-right');}
      else{panel.style.right='auto';panel.style.left='0';panel.style.top='0';panel.style.width=W+'px';panel.style.height=H+'px';panel.classList.add('snap-top');}
      hg();snapState=null;
    }
    drag=false;
  });
  hd.addEventListener('dblclick',()=>{
    panel.classList.remove('snap-left','snap-right','snap-top','snap-bottom');
    panel.style.width=(preW||440)+'px';panel.style.height=(preH||500)+'px';
    panel.style.left=(preL||(window.innerWidth-460))+'px';panel.style.top=(preT||60)+'px';
    preW=preH=preL=preT=null;
  });
  // Resize handles
  let rs=false,rd='',rx,ry,rl,rt,rw,rh;
  panel.querySelectorAll('.resize-handle').forEach(h=>{
    h.addEventListener('mousedown',e=>{
      e.preventDefault();e.stopPropagation();
      // Remove snap classes so CSS doesn't override inline resize styles
      panel.classList.remove('snap-left','snap-right','snap-top','snap-bottom');
      rs=true;rd=h.dataset.dir;rx=e.clientX;ry=e.clientY;
      const r=panel.getBoundingClientRect();rl=r.left;rt=r.top;rw=r.width;rh=r.height;
      panel.style.right='auto';panel.style.bottom='auto';
      panel.style.left=rl+'px';panel.style.top=rt+'px';
    });
  });
  document.addEventListener('mousemove',e=>{
    if(!rs)return;
    const dx=e.clientX-rx,dy=e.clientY-ry;let nl=rl,nt=rt,nw=rw,nh=rh;
    if(rd.includes('e'))nw=Math.max(280,rw+dx);
    if(rd.includes('s'))nh=Math.max(200,rh+dy);
    if(rd.includes('w')){nw=Math.max(280,rw-dx);nl=rl+(rw-nw);}
    if(rd.includes('n')){nh=Math.max(200,rh-dy);nt=rt+(rh-nh);}
    panel.style.width=nw+'px';panel.style.height=nh+'px';
    panel.style.left=nl+'px';panel.style.top=nt+'px';
  });
  document.addEventListener('mouseup',()=>{rs=false;});
}

async function refreshTasksList(){
  try{const r=await fetch('/tasks');const d=await r.json();renderTasksList(d.tasks||[]);}catch(e){}
}
async function checkTaskNotifications() {
  try {
    const r = await fetch('/tasks/notifications');
    const d = await r.json();
    const notes = d.notifications || [];
    for (const note of notes) {
      const sid = note.session_id;
      const msg = note.message || ('Background task \'' + (note.task_name||'') + '\' completed.');
      // Inject into chat if it's the currently viewed session
      if (sid === currentSessionId) {
        const b = createMessage('ai', note.ts || new Date().toISOString());
        b.innerHTML = renderMarkdown(msg);
        scrollToBottom();
      }
      // Show a toast regardless
      showToast('📋 ' + msg.slice(0, 60) + (msg.length > 60 ? '\u2026' : ''));
      // Reload sessions list to update sidebar
      await loadSessions();
    }
  } catch(e) {}
}

async function refreshTasksBadge(){
  try{
    const r=await fetch('/tasks');const d=await r.json();
    const run=(d.tasks||[]).filter(t=>t.running).length;
    const b=document.getElementById('tasks-badge');if(b){b.textContent=run;b.classList.toggle('visible',run>0);}
    const c=document.getElementById('tasks-count');if(c)c.textContent=(d.tasks||[]).length;
  }catch(e){}
  // Check for background task completion notifications
  await checkTaskNotifications();
  setTimeout(refreshTasksBadge,3000);
}

function renderTasksList(tasks){
  const list=document.getElementById('tasks-list');
  const empty=document.getElementById('tasks-empty');
  if(!tasks.length){if(empty)empty.style.display='block';list.querySelectorAll('.task-item').forEach(el=>el.remove());return;}
  if(empty)empty.style.display='none';
  list.querySelectorAll('.task-item').forEach(el=>{if(!tasks.find(t=>t.name===el.dataset.name))el.remove();});
  tasks.forEach(task=>{
    let item=list.querySelector('.task-item[data-name="'+task.name+'"]');
    if(!item){
      item=document.createElement('div');item.className='task-item';item.dataset.name=task.name;
      const n=task.name;
      item.innerHTML=`<div class="task-item-header" onclick="toggleTaskLog('${n}')">
        <div class="task-name"><span class="task-running-dot ${task.running?'':'stopped'}" id="dot-${n}"></span>${escHtml(n)}</div>
        <div class="task-actions" id="actions-${n}">
          <button class="task-stop-btn" id="stop-${n}" onclick="event.stopPropagation();stopTask('${n}')" ${task.running?'':'disabled style="opacity:.35"'}>&#9646; Stop</button>
          <button class="task-clear-btn" onclick="event.stopPropagation();showClearConfirm('${n}')">&#128465; Clear</button>
          <div class="task-confirm" id="confirm-${n}">
            <div class="task-confirm-title">What to do?</div>
            <div class="task-confirm-btns">
              <button class="task-confirm-btn clear-only" onclick="event.stopPropagation();clearLogOnly('${n}')">&#128465; Clear log</button>
              <button class="task-confirm-btn stop-clear" onclick="event.stopPropagation();stopAndClear('${n}')">&#9646; Stop+clear</button>
              <button class="task-confirm-btn cancel" onclick="event.stopPropagation();hideClearConfirm('${n}')">Cancel</button>
            </div>
          </div>
        </div></div>
        <div class="task-log" id="log-${n}"><div class="task-log-content" id="logcontent-${n}"></div></div>`;
      list.appendChild(item);
    }else{
      const dot=document.getElementById('dot-'+task.name);const sb=document.getElementById('stop-'+task.name);
      if(dot)dot.className='task-running-dot '+(task.running?'':'stopped');
      if(sb){sb.disabled=!task.running;sb.style.opacity=task.running?'1':'0.35';}
    }
  });
}

function toggleTaskLog(name){const l=document.getElementById('log-'+name);if(!l)return;if(l.classList.toggle('expanded'))startLogStream(name);else stopLogStream(name);}

async function stopAllTasks(){
  const btn=document.getElementById('tasks-stop-all-btn');
  if(btn){btn.disabled=true;btn.textContent='\u2026';}
  try{
    await fetch('/tasks/stop-all',{method:'POST'});
    document.querySelectorAll('.task-item').forEach(el=>el.remove());
    const empty=document.getElementById('tasks-empty');if(empty)empty.style.display='block';
    const cnt=document.getElementById('tasks-count');if(cnt)cnt.textContent='0';
    const badge=document.getElementById('tasks-badge');if(badge){badge.textContent='0';badge.classList.remove('visible');}
    showToast('All tasks stopped and cleared');
  }catch(e){showToast('Failed to stop all tasks');}
  finally{if(btn){btn.disabled=false;btn.textContent='\u23F9 Stop All';}}
}

async function startLogStream(name){
  if(taskLogStreams[name])return;
  const ce=document.getElementById('logcontent-'+name);if(!ce)return;
  const ctrl=new AbortController();taskLogStreams[name]=ctrl;
  try{
    const resp=await fetch('/tasks/'+name+'/logs',{signal:ctrl.signal});
    if(!resp.ok){appendLogLine(ce,name,'[error: HTTP '+resp.status+']');return;}
    const reader=resp.body.getReader();const dec=new TextDecoder();let buf='';
    while(true){
      const{done,value}=await reader.read();if(done)break;
      buf+=dec.decode(value,{stream:true});
      const lines=buf.split('\n');buf=lines.pop();
      for(const raw of lines){
        const t=raw.trim();if(!t)continue;
        let ev;try{ev=JSON.parse(t);}catch{continue;}
        if(ev.line!==undefined&&ev.line!=='')appendLogLine(ce,name,ev.line);
        if(ev.done){
          const dot=document.getElementById('dot-'+name);const sb=document.getElementById('stop-'+name);
          if(dot)dot.className='task-running-dot stopped';
          if(sb){sb.disabled=true;sb.style.opacity='0.35';}
          delete taskLogStreams[name];return;
        }
      }
    }
  }catch(err){if(err.name!=='AbortError')appendLogLine(ce,name,'[stream ended: '+err.message+']');}
  finally{delete taskLogStreams[name];}
}

function appendLogLine(ce,name,text){
  const l=document.createElement('div');l.className='task-log-line';l.textContent=text;ce.appendChild(l);
  const le=document.getElementById('log-'+name);if(le)le.scrollTop=le.scrollHeight;
}
function stopLogStream(name){if(taskLogStreams[name]){taskLogStreams[name].abort();delete taskLogStreams[name];}}

async function stopTask(name){
  const btn=document.getElementById('stop-'+name);if(btn){btn.disabled=true;btn.textContent='\u2026';}
  try{await fetch('/tasks/'+name+'/stop',{method:'POST'});showToast("Task '"+name+"' stopped");await refreshTasksList();refreshTasksBadge();}
  catch(e){showToast('Failed to stop task');if(btn){btn.disabled=false;btn.textContent='\u25a0 Stop';}}
}
function showClearConfirm(name){
  document.querySelectorAll('.task-confirm.show').forEach(el=>{if(el.id!=='confirm-'+name)el.classList.remove('show');});
  const c=document.getElementById('confirm-'+name);if(!c)return;
  c.classList.toggle('show');
  if(c.classList.contains('show'))setTimeout(()=>{document.addEventListener('click',function h(e){if(!c.contains(e.target)){c.classList.remove('show');document.removeEventListener('click',h);}});},0);
}
function hideClearConfirm(name){const el=document.getElementById('confirm-'+name);if(el)el.classList.remove('show');}
async function clearLogOnly(name){
  hideClearConfirm(name);
  try{await fetch('/tasks/'+name+'/log',{method:'DELETE'});const ce=document.getElementById('logcontent-'+name);if(ce)ce.innerHTML='';showToast("Log cleared for '"+name+"'");}
  catch(e){showToast('Failed to clear log');}
}
async function stopAndClear(name){
  hideClearConfirm(name);const btn=document.getElementById('stop-'+name);if(btn){btn.disabled=true;btn.textContent='\u2026';}
  try{await fetch('/tasks/'+name+'/stop-and-clear',{method:'POST'});const ce=document.getElementById('logcontent-'+name);if(ce)ce.innerHTML='';showToast("Task '"+name+"' stopped and cleared");await refreshTasksList();refreshTasksBadge();}
  catch(e){showToast('Failed to stop and clear');if(btn){btn.disabled=false;btn.textContent='Stop';}}
}

// ── Inline title editing (single click) ──────────────────────────────────────
chatTitleEl.style.cursor = 'text';
chatTitleEl.title = 'Click to rename this chat';

chatTitleEl.addEventListener('click', () => {
  if (!currentSessionId) return;
  if (chatTitleEl.querySelector('input')) return; // already editing

  const originalTitle = chatTitleEl.textContent.trim();
  chatTitleEl.textContent = '';

  const input = document.createElement('input');
  input.type = 'text';
  input.value = originalTitle;
  input.className = 'title-edit-input';
  input.maxLength = 80;
  chatTitleEl.appendChild(input);
  input.focus();
  input.select();

  async function commitRename() {
    const newTitle = input.value.trim() || originalTitle;
    chatTitleEl.textContent = newTitle;
    if (newTitle !== originalTitle) {
      try {
        await fetch('/sessions/' + currentSessionId + '/rename', {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title: newTitle }),
        });
        await loadSessions();
      } catch(e) { showToast('Failed to rename'); }
    }
  }

  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
    if (e.key === 'Escape') { chatTitleEl.textContent = originalTitle; }
  });
  input.addEventListener('blur', commitRename);
});

window.addEventListener('load',async()=>{
  try{
    // Auto-collapse sidebar on small screens
    if(window.innerWidth<=700){
      sidebar.classList.add('collapsed');
      if(sidebarOpenBtn)sidebarOpenBtn.style.display='flex';
    }
    setStatus('ready','Ready');
    textarea.focus();
    refreshTasksBadge();
    await loadSessions();
    const res=await fetch('/sessions');const data=await res.json();
    if(data.sessions&&data.sessions.length>0)await switchSession(data.sessions[0].id);
    else await newChat();
  }catch(e){console.error('Init error:',e);setStatus('ready','Ready');}
});

// Also close sidebar overlay on mobile when clicking outside it
document.addEventListener('click', e => {
  if(window.innerWidth > 700) return;
  if(sidebar.classList.contains('collapsed')) return;
  if(!sidebar.contains(e.target) && !sidebarOpenBtn?.contains(e.target)){
    sidebar.classList.add('collapsed');
    if(sidebarOpenBtn) sidebarOpenBtn.style.display='flex';
  }
});
