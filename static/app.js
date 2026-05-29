
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
let _currentSessionEmpty = true; // true when current session has no messages yet
// Per-conversation active agent task tracking: sessionId → { taskId, progressEl, done }
const activeTaskByConversation = new Map();

// Module-level refs to IIFE-internal functions (populated at IIFE bootstrap)
let _streamTaskEvents = null;    // set by IIFE once ready
let _appendTaskProgress = null;  // set by IIFE once ready
// Per-session streaming check
function isStreamingSession(sid) { return _streamingSet.has(sid); }

// ── Tab visibility + title flash ──────────────────────────────────────────────
let _tabHidden = false;
let _origTitle = document.title;
let _flashInterval = null;

function flashTabTitle(msg) {
    if (!_tabHidden) return;
    if (_flashInterval) return; // already flashing
    let alt = false;
    _flashInterval = setInterval(() => {
        document.title = alt ? msg : _origTitle;
        alt = !alt;
    }, 1000);
}

function stopFlashTitle() {
    if (_flashInterval) { clearInterval(_flashInterval); _flashInterval = null; }
    document.title = _origTitle;
}

document.addEventListener('visibilitychange', () => {
    _tabHidden = document.hidden;
    if (!document.hidden) stopFlashTitle();
});

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
  _streamingSet.delete(currentSessionId);
  sendBtn.disabled = false;
  setStatus('ready', 'Stopped');
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
  // Use textContent (not innerText) to get the full raw text regardless of
  // layout/visibility — innerText is layout-dependent and truncates off-screen
  // content. Then decode HTML entities introduced by renderMarkdown's escaping
  // step (&amp; → &, &lt; → <, &gt; → >).
  const raw = code.textContent
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>');
  navigator.clipboard.writeText(raw).then(() => {
    btn.textContent = '✓ Copied';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 1800);
  }).catch(() => {
    // Fallback for older browsers
    const ta = document.createElement('textarea');
    ta.value = raw;
    ta.style.cssText = 'position:fixed;opacity:0;pointer-events:none';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    btn.textContent = '✓ Copied';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 1800);
  });
}

function renderMarkdown(text) {
  let html = text
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/```(\w*)\n?([\s\S]*?)```/g,(_,lang,code)=>`<pre><button class="copy-btn" onclick="copyCode(this)">Copy</button><code class="lang-${lang}">${code.trim()}</code></pre>`)
    // Markdown tables
    .replace(/^(\|.+\|)\n\|([-| :]+)\|\n((?:\|.+\|\n?)*)/gm, (_, header, sep, rows) => {
        const ths = header.split('|').filter((_,i,a) => i > 0 && i < a.length-1)
            .map(h => `<th>${h.trim()}</th>`).join('');
        const trs = rows.trim().split('\n').map(row => {
            const tds = row.split('|').filter((_,i,a) => i > 0 && i < a.length-1)
                .map(c => `<td>${c.trim()}</td>`).join('');
            return `<tr>${tds}</tr>`;
        }).join('');
        return `<table><thead><tr>${ths}</tr></thead><tbody>${trs}</tbody></table>`;
    })
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

function createSessionItem(s) {
  const item = document.createElement('div');
  item.className = 'session-item'
    + (s.id === currentSessionId ? ' active' : '')
    + (s.pinned ? ' pinned' : '');
  item.dataset.id = s.id;

  const label = document.createElement('a');
  label.className = 'session-label';
  label.href = '/?session=' + encodeURIComponent(s.id);
  const prefix = s.running ? '<span class="session-running-dot" title="AI working\u2026"></span>' : '';
  label.innerHTML = prefix + escHtml(s.title || 'New Chat');
  label.title = (s.running ? '\u2699 Working\u2026 ' : '') + (s.title || 'New Chat');
  label.addEventListener('click', e => {
    e.preventDefault();
    switchSession(s.id);
    closeSidebarOnMobile();
  });

  const actions = document.createElement('div');
  actions.className = 'session-actions';

  const pinBtn = document.createElement('button');
  pinBtn.className = 'session-action-btn' + (s.pinned ? ' pinned-active' : '');
  pinBtn.title = s.pinned ? 'Unpin' : 'Pin to top';
  pinBtn.textContent = s.pinned ? '\uD83D\uDCCC' : '\u2606';
  pinBtn.onclick = e => { e.stopPropagation(); togglePin(s.id); };

  const renameBtn = document.createElement('button');
  renameBtn.className = 'session-action-btn';
  renameBtn.title = 'Rename';
  renameBtn.textContent = '\u270F';
  renameBtn.onclick = e => { e.stopPropagation(); openRenameModal(s.id, s.title); };

  const deleteBtn = document.createElement('button');
  deleteBtn.className = 'session-action-btn danger';
  deleteBtn.title = 'Delete';
  deleteBtn.textContent = '\uD83D\uDDD1';
  deleteBtn.onclick = e => { e.stopPropagation(); deleteSession(s.id); };

  actions.appendChild(pinBtn);
  actions.appendChild(renameBtn);
  actions.appendChild(deleteBtn);
  item.appendChild(label);
  item.appendChild(actions);
  return item;
}

async function togglePin(id) {
  try {
    await fetch('/sessions/' + id + '/pin', { method: 'PATCH' });
    await loadSessions();
  } catch(e) { showToast('Failed to toggle pin'); }
}

function initPinnedDrag(listEl) {
  let dragging = null;
  listEl.querySelectorAll('.session-item').forEach(item => {
    item.setAttribute('draggable', 'true');
    item.addEventListener('dragstart', e => {
      dragging = item;
      item.classList.add('drag-ghost');
      e.dataTransfer.effectAllowed = 'move';
    });
    item.addEventListener('dragend', () => {
      dragging = null;
      item.classList.remove('drag-ghost');
      listEl.querySelectorAll('.session-item').forEach(i => i.classList.remove('drop-before','drop-after'));
      const order = Array.from(listEl.querySelectorAll('.session-item')).map(i => i.dataset.id);
      fetch('/sessions/reorder-pins', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ order }),
      }).catch(err => console.warn('reorder-pins failed', err));
    });
    item.addEventListener('dragover', e => {
      if (!dragging || dragging === item) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      // Remove indicators from all items first
      listEl.querySelectorAll('.drop-before,.drop-after').forEach(el => {
        el.classList.remove('drop-before','drop-after');
      });
      // Determine top vs bottom half
      const rect = item.getBoundingClientRect();
      const midY = rect.top + rect.height / 2;
      if (e.clientY < midY) {
        item.classList.add('drop-before');
      } else {
        item.classList.add('drop-after');
      }
    });
    item.addEventListener('dragleave', () => {
      item.classList.remove('drop-before','drop-after');
    });
    item.addEventListener('drop', e => {
      if (!dragging || dragging === item) return;
      e.preventDefault();
      // Find where to insert based on drop-before/drop-after class
      const dropBefore = item.classList.contains('drop-before');
      item.classList.remove('drop-before','drop-after');
      dragging.classList.remove('drag-ghost');
      if (dropBefore) {
        listEl.insertBefore(dragging, item);
      } else {
        listEl.insertBefore(dragging, item.nextSibling);
      }
    });
  });
}

function renderSessionsList(sessions) {
  sessionsList.innerHTML = '';
  if (!sessions.length) {
    sessionsList.innerHTML = '<div class="sessions-empty">No chats yet</div>';
    return;
  }
  const pinned   = sessions.filter(s => s.pinned);
  const unpinned = sessions.filter(s => !s.pinned);
  if (pinned.length > 0) {
    const header = document.createElement('div');
    header.className = 'sessions-section-header';
    header.textContent = '\uD83D\uDCCC Pinned';
    sessionsList.appendChild(header);
    const pinnedList = document.createElement('div');
    pinnedList.id = 'pinned-sessions';
    pinnedList.className = 'pinned-sessions-list';
    pinned.forEach(s => pinnedList.appendChild(createSessionItem(s)));
    sessionsList.appendChild(pinnedList);
    initPinnedDrag(pinnedList);
    const divider = document.createElement('div');
    divider.className = 'sessions-divider';
    sessionsList.appendChild(divider);
  }
  unpinned.forEach(s => sessionsList.appendChild(createSessionItem(s)));
}

async function newChat() {
  try {
    const res  = await fetch('/sessions', {method:'POST'});
    const data = await res.json();
    await loadSessions();
    _currentSessionEmpty = true; // new chat starts empty
    await switchSession(data.id);
    closeSidebarOnMobile();
  } catch(e) { showToast('Failed to create chat'); }
}

async function switchSession(id) {
  // Auto-delete the current session if it's empty (no messages sent)
  if (currentSessionId && currentSessionId !== id) {
    // Use server-side check to safely delete only if truly empty
    // This prevents the client-side flag from incorrectly deleting sessions
    // that have server-side messages (e.g., background notifications)
    try {
      const delRes = await fetch('/sessions/' + currentSessionId + '/delete-if-empty', {method: 'POST'});
      if (delRes.ok) {
        const delData = await delRes.json();
        if (delData.status === 'deleted') {
          // Session was empty and deleted — refresh sidebar so the stale item disappears
          await loadSessions();
        }
      }
    } catch(e) {}
  }
  if (id === currentSessionId) return;

  // Before clearing the DOM, mark any live task as needing re-attach on switch-back.
  // We do NOT save HTML — we save only the taskId and let the stream replay from cursor=0.
  if (currentSessionId) {
    const record = activeTaskByConversation.get(currentSessionId);
    if (record && !record.done) {
      // Nullify progressEl — it will be re-created fresh on switch-back
      record.progressEl = null;
    }
  }

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
    window.history.replaceState({}, '', '/?session=' + encodeURIComponent(id));
    messagesEl.innerHTML = '';
    _currentSessionEmpty = (!data.messages || data.messages.length === 0);
    if (data.messages && data.messages.length > 0) {
      data.messages.forEach(msg => {
        if (msg.role === 'user') {
          const b = createMessage('user', msg.ts);
          b.innerHTML = renderMarkdown(msg.content || '');
        } else if (msg.role === 'assistant') {
          const isNotif = msg.notification === true;
          const b = createMessage('ai', msg.ts);
          b.innerHTML = renderMarkdown(msg.content || '');
          if (isNotif) {
            const levelIcon = {alert: '🚨', warning: '⚠️', success: '✅', info: '📋'};
            const icon = levelIcon[msg.level] || '⚙';
            const msgRow = b.closest ? b.closest('.msg') : null;
            if (msgRow) {
              const avatar = msgRow.querySelector('.avatar');
              if (avatar) avatar.textContent = icon;
              msgRow.classList.add('notif-' + (msg.level || 'info'));
            }
          }
        }
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

    // Restore active agent task progress card and reconnect stream if still running.
    const taskRecord = activeTaskByConversation.get(id);
    if (taskRecord && !taskRecord.done && _streamTaskEvents && _appendTaskProgress) {
      // Create a FRESH progress card (do not reuse or re-inject old DOM)
      const freshCard = _appendTaskProgress('(reconnecting task…)');
      taskRecord.progressEl = freshCard;
      // Stream replays from cursor=0 — the card will fill in correctly
      _streamTaskEvents(taskRecord.taskId, freshCard);
    }
  } catch(e) { showToast('Failed to load session'); }
}

// ── Reconnect to a session whose agent is still running in the background ────
async function reconnectToSession(id) {
  if (isStreamingSession(id)) return;
  _streamingSet.add(id);
  if (id === currentSessionId) {
    sendBtn.disabled = true; showStopBtn();
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
    if(id===currentSessionId){_currentReader=null;hideStopBtn();setActivity('');setStatus('ready','Ready');}
    _streamingSet.delete(id);sendBtn.disabled=false;scrollToBottom();
  }
}

async function deleteSession(id) {
  if (!confirm('Delete this chat? This cannot be undone.')) return;
  try {
    await fetch(`/sessions/${id}`, {method:'DELETE'});
    currentSessionId = null;
    await loadSessions();
    await newChat();
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
  _currentSessionEmpty = false; // a message is being sent — session is no longer empty
  const ub=createMessage('user', _msgNow);ub.innerHTML=renderMarkdown(fullText||text);
  textarea.value='';textarea.style.height='auto';
  _attachedFiles=[];renderFilePreviews();
  _streamingSet.add(currentSessionId);sendBtn.disabled=true;setStatus('thinking','Thinking\u2026');
  showStopBtn();

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
          flashTabTitle("✅ Big's AI replied");
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

// ── SSE event stream (replaces polling) ──────────────────────────────────────
let _eventSource = null;

function initEventStream() {
  if (_eventSource) { _eventSource.close(); _eventSource = null; }
  _eventSource = new EventSource('/events');

  _eventSource.onmessage = async (e) => {
    try {
      const d = JSON.parse(e.data);

      // Update tasks badge
      const tasks = d.tasks || [];
      const run = tasks.filter(t => t.running).length;
      const b = document.getElementById('tasks-badge');
      if (b) { b.textContent = run; b.classList.toggle('visible', run > 0); }
      const c = document.getElementById('tasks-count');
      if (c) c.textContent = tasks.length;

      // Update tasks list if panel is open
      const panel = document.getElementById('tasks-panel');
      if (panel && panel.classList.contains('open')) {
        renderTasksList(tasks);
      }

      // Process notifications
      const notes = d.notifications || [];
      for (const note of notes) {
        const sid = note.session_id;
        const msg = note.message || ('Background task \'' + (note.task_name || '') + '\' completed.');
        if (sid === currentSessionId) {
          const b2 = createMessage('ai', note.ts || new Date().toISOString());
          b2.innerHTML = renderMarkdown(msg);
          const levelIcon = {alert: '🚨', warning: '⚠️', success: '✅', info: '📋'};
          const icon = levelIcon[note.level] || '⚙';
          const msgRow = b2.closest ? b2.closest('.msg') : null;
          if (msgRow) {
            const avatar = msgRow.querySelector('.avatar');
            if (avatar) avatar.textContent = icon;
            msgRow.classList.add('notif-' + (note.level || 'info'));
          }
          scrollToBottom();
        }
        const levelEmoji = {alert: '🚨', warning: '⚠️', success: '✅', info: '📋'};
        const emoji = levelEmoji[note.level] || '📋';
        showToast(emoji + ' ' + msg.slice(0, 60) + (msg.length > 60 ? '\u2026' : ''));
        flashTabTitle('🔔 ' + msg.slice(0, 30));
        await loadSessions();
      }

      // Update activity status for current session via SSE (replaces /chat/status/ polling)
      const activity = d.activity || {};
      const currentActivity = activity[currentSessionId];
      if (currentActivity) {
        setActivity('\u2699 ' + currentActivity);
      } else if (currentSessionId && !isStreamingSession(currentSessionId)) {
        setActivity('');
      }
    } catch(err) { console.warn('Event stream parse error:', err); }
  };

  _eventSource.onerror = () => {
    _eventSource.close();
    _eventSource = null;
    setTimeout(initEventStream, 5000);
  };
}

// Keep refreshTasksBadge as a one-shot helper (used after task stop/clear actions)
async function refreshTasksBadge(){
  try{
    const r=await fetch('/tasks');const d=await r.json();
    const run=(d.tasks||[]).filter(t=>t.running).length;
    const b=document.getElementById('tasks-badge');if(b){b.textContent=run;b.classList.toggle('visible',run>0);}
    const c=document.getElementById('tasks-count');if(c)c.textContent=(d.tasks||[]).length;
  }catch(e){}
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
          <button class="task-clear-btn" onclick="event.stopPropagation();clearLogOnly('${n}')">&#128465; Clear</button>
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

function collapseAllLogs(){
  document.querySelectorAll('.task-log.expanded').forEach(log=>{
    const name=log.id.replace('log-','');
    log.classList.remove('expanded');
    stopLogStream(name);
  });
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

window.addEventListener('pagehide', () => {
    // Clean up empty chat sessions when user closes/refreshes the tab
    if (currentSessionId && _currentSessionEmpty) {
        navigator.sendBeacon('/sessions/' + currentSessionId + '/delete-if-empty');
    }
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
    initEventStream();
    await loadSessions();
    // Check for ?session= URL param
    const urlParams = new URLSearchParams(window.location.search);
    const requestedSession = urlParams.get('session');
    const res=await fetch('/sessions');const data=await res.json();
    if (requestedSession && data.sessions && data.sessions.find(s => s.id === requestedSession)) {
        await switchSession(requestedSession);
    } else {
        await newChat();
    }
    // Recover any in-progress agent tasks from localStorage (page reload scenario).
    // Run after session is established so currentSessionId is set.
    // Use setTimeout(0) to ensure IIFE bootstrap has run and _streamTaskEvents is set.
    setTimeout(() => recoverActiveTasksFromStorage(), 0);
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


// ═══════════════════════════════════════════════════════════
// TASK MODE — AgentExecutor wiring
// Adds a ⚡ Task Mode toggle next to the Send button.
// When active, messages go to POST /agent/task instead of
// POST /chat/stream, and progress is streamed via SSE.
// ═══════════════════════════════════════════════════════════

(function () {
  'use strict';

  // ── State ────────────────────────────────────────────────
  let taskModeActive = false;
  let activeTaskId = null;
  let taskEventSource = null;

  // ── Inject toggle button into the input area ─────────────
  function injectTaskToggle() {
    // Find the send button — try common ids/classes
    const sendBtn =
      document.getElementById('send-btn') ||
      document.getElementById('sendBtn') ||
      document.querySelector('[data-action="send"]') ||
      document.querySelector('button[type="submit"]') ||
      document.querySelector('.send-btn') ||
      document.querySelector('.input-area button') ||
      document.querySelector('.chat-input-row button') ||
      document.querySelector('#input-area button') ||
      document.querySelector('form button');

    if (!sendBtn) {
      console.warn('[TaskMode] Could not find send button — retrying in 1s');
      setTimeout(injectTaskToggle, 1000);
      return;
    }

    if (document.getElementById('task-mode-toggle')) return; // already injected

    const toggle = document.createElement('button');
    toggle.id = 'task-mode-toggle';
    toggle.type = 'button';
    toggle.title = 'Task Mode — structured plan → execute → verify';
    toggle.innerHTML = '⚡';
    toggle.style.cssText = [
      'width: 44px',
      'height: 44px',
      'background: transparent',
      'border: 1.5px solid #44446a',
      'border-radius: 10px',
      'color: #888',
      'cursor: pointer',
      'font-size: 18px',
      'flex-shrink: 0',
      'display: flex',
      'align-items: center',
      'justify-content: center',
      'transition: all .2s',
      'padding: 0',
    ].join(';');

    toggle.addEventListener('mouseenter', () => {
      if (!taskModeActive) toggle.style.background = 'rgba(108,99,255,0.15)';
    });
    toggle.addEventListener('mouseleave', () => {
      if (!taskModeActive) toggle.style.background = 'transparent';
    });
    toggle.addEventListener('click', () => {
      taskModeActive = !taskModeActive;
      toggle.style.background = taskModeActive ? '#6c63ff' : 'transparent';
      toggle.style.borderColor = taskModeActive ? '#6c63ff' : '#44446a';
      toggle.style.color = taskModeActive ? '#fff' : '#888';
      toggle.title = taskModeActive
        ? 'Task Mode ON — structured plan → execute → verify (click to disable)'
        : 'Task Mode — structured plan → execute → verify (click to enable)';
    });

    sendBtn.parentNode.insertBefore(toggle, sendBtn);
    console.log('[TaskMode] Toggle injected');
  }

  // ── Intercept form submit / send button click ─────────────
  function interceptSend() {
    // Hook into the document-level keydown (Enter key) and click
    document.addEventListener('keydown', (e) => {
      if (!taskModeActive) return;
      if (e.key !== 'Enter' || e.shiftKey) return;
      const ta =
        document.querySelector('textarea') ||
        document.querySelector('input[type="text"]');
      if (document.activeElement === ta) {
        e.preventDefault();
        e.stopImmediatePropagation();
        submitAsTask(ta.value.trim());
        ta.value = '';
        ta.style.height = '';
      }
    }, true); // capture phase — runs before app's own handler

    // Also hook the send button
    document.addEventListener('click', (e) => {
      if (!taskModeActive) return;
      const sendBtn =
        document.getElementById('send-btn') ||
        document.getElementById('sendBtn') ||
        document.querySelector('[data-action="send"]') ||
        document.querySelector('button[type="submit"]') ||
        document.querySelector('.send-btn') ||
        document.querySelector('.chat-input-row button:not(#task-mode-toggle)') ||
        document.querySelector('#input-area button:not(#task-mode-toggle)');
      if (e.target === sendBtn || (sendBtn && sendBtn.contains(e.target))) {
        e.preventDefault();
        e.stopImmediatePropagation();
        const ta =
          document.querySelector('textarea') ||
          document.querySelector('input[type="text"]');
        if (ta) {
          const msg = ta.value.trim();
          if (msg) {
            submitAsTask(msg);
            ta.value = '';
            ta.style.height = '';
          }
        }
      }
    }, true);
  }

  // ── Get current session id ────────────────────────────────
  function getSessionId() {
    // Try common globals used by app.js
    if (typeof currentSessionId !== "undefined" && currentSessionId) return currentSessionId;
    if (window.sessionId) return window.sessionId;
    if (window.app && window.app.sessionId) return window.app.sessionId;
    // Try URL param
    const params = new URLSearchParams(location.search);
    if (params.get('session')) return params.get('session');
    // Last resort: read from a data attribute
    const el = document.querySelector('[data-session-id]');
    if (el) return el.dataset.sessionId;
    return null;
  }

  // ── Submit message as AgentExecutor task ─────────────────
  async function submitAsTask(description) {
    if (!description) return;

    const sessionId = getSessionId();
    appendTaskMessage('user', description);
    const progressEl = appendTaskProgress(description);

    try {
      const resp = await fetch('/agent/task', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ description, session_id: sessionId }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const { task_id } = await resp.json();
      activeTaskId = task_id;

      // Register in per-conversation map so switchSession() can restore it on switch-back
      if (sessionId) {
        activeTaskByConversation.set(sessionId, { taskId: task_id, progressEl, done: false });
        try {
          localStorage.setItem(
            'activeTask_' + sessionId,
            JSON.stringify({ taskId: task_id, sessionId: sessionId })
          );
        } catch(e) {}
      }

      streamTaskEvents(task_id, progressEl);
    } catch (err) {
      progressEl.innerHTML = '<span style="color:#f38ba8">\u26a0\ufe0f Task submit failed: ' + err.message + '</span>';
    }
  }

  // ── Stream SSE events from /agent/task/{id}/stream ────────
  // Uses fetch()+ReadableStream (same pattern as /chat/stream) instead of
  // native EventSource. The backend yields lines like:
  //   "data: {...}\n\n"  (proper SSE)  OR  " {...}\n\n" (space-prefix variant)
  // Parsing manually handles both, and also fixes the EventSource onerror
  // reconnect loop that could swallow all events on fast-completing tasks.
  async function streamTaskEvents(taskId, progressEl) {
    if (taskEventSource) { taskEventSource.close(); taskEventSource = null; }

    const ctrl = new AbortController();
    taskEventSource = { close: () => ctrl.abort() };

    try {
      const resp = await fetch(`/agent/task/${taskId}/stream`, { signal: ctrl.signal });
      if (!resp.ok) {
        updateProgress(progressEl, `\u274c Stream error: HTTP ${resp.status}`, 'failed');
        taskEventSource = null;
        return;
      }

      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      let buf = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const lines = buf.split('\n');
        buf = lines.pop();

        for (const raw of lines) {
          const trimmed = raw.trim();
          if (!trimmed || trimmed.startsWith(':')) continue;

          // Strip "data:" prefix (with or without leading space) then parse JSON
          const jsonStr = trimmed.startsWith('data:')
            ? trimmed.slice(5).trim()
            : trimmed;

          let ev;
          try { ev = JSON.parse(jsonStr); } catch { continue; }

          switch (ev.type) {
            case 'task_started':
              updateProgress(progressEl, '\u23f3 Starting task\u2026', 'planning');
              break;

            case 'task_researching':
              updateProgress(progressEl, '\ud83d\udd0d Researching requirements and context\u2026', 'researching');
              if (ev.summary) appendPhaseLog(progressEl, '\ud83d\udd0d', 'Research Summary', ev.summary);
              break;

            case 'task_planning':
              updateProgress(progressEl,
                ev.message ? '\ud83d\udccb ' + ev.message : '\ud83d\udccb Planning steps\u2026',
                'planning');
              break;

            case 'task_planned':
              updateProgress(progressEl,
                '\ud83d\udccb Plan ready \u2014 ' + (ev.steps ? ev.steps.length : '?') + ' steps', 'planned');
              renderStepList(progressEl, ev.steps || []);
              if (ev.steps && ev.steps.length) {
                const planText = ev.steps.map(function(s) { return s.step_id + '. ' + s.description; }).join('\n');
                appendPhaseLog(progressEl, '\ud83d\udccb', 'Execution Plan', planText);
              }
              break;

            case 'task_executing':
              updateProgress(progressEl,
                ev.message ? '\u2699\ufe0f ' + ev.message : '\u2699\ufe0f Executing steps\u2026',
                'executing');
              break;

            case 'task_verifying':
              updateProgress(progressEl, '\ud83d\udd0d Verifying results\u2026', 'verifying');
              break;

            case 'task_verified':
              updateProgress(progressEl,
                ev.passed ? '\u2705 Verification passed' : '\u26a0\ufe0f ' + (ev.message || 'Verification failed'),
                ev.passed ? 'completed' : 'executing');
              appendPhaseLog(progressEl,
                ev.passed ? '\u2705' : '\u26a0\ufe0f',
                'Verification',
                ev.message || (ev.passed ? 'Output meets requirements.' : 'Requirements not fully met.'));
              break;

            case 'step_started': {
              const sid = ev.step ? ev.step.step_id : ev.step_id;
              const sdesc = ev.step ? ev.step.description : (ev.description || '');
              updateProgress(progressEl, '\u2699\ufe0f Step ' + sid + ': ' + sdesc, 'executing');
              highlightStep(progressEl, sid, 'running');
              break;
            }

            case 'step_completed': {
              const sid = ev.step ? ev.step.step_id : ev.step_id;
              const sdesc = ev.step ? ev.step.description : '';
              const sresult = ev.step ? ev.step.result : (ev.result || '');
              highlightStep(progressEl, sid, 'done');
              if (sresult) appendPhaseLog(progressEl, '\u2705', 'Step ' + sid + ': ' + sdesc, sresult);
              break;
            }

            case 'step_failed': {
              const sid = ev.step ? ev.step.step_id : ev.step_id;
              const serr = ev.step ? ev.step.error : (ev.error || 'unknown error');
              highlightStep(progressEl, sid, 'failed');
              appendPhaseLog(progressEl, '\u274c', 'Step ' + sid + ' failed', serr);
              break;
            }

            case 'task_completed':
              updateProgress(progressEl, '\u2705 Task completed', 'completed');
              taskEventSource = null;
              activeTaskId = null;
              // Mark done in the map and clean up localStorage
              activeTaskByConversation.forEach((rec, sid) => {
                if (rec.taskId === taskId) {
                  rec.done = true;
                  try { localStorage.removeItem('activeTask_' + sid); } catch(e) {}
                }
              });
              return;

            case 'task_failed':
              updateProgress(progressEl, `\u274c Task failed: ${ev.error || 'unknown error'}`, 'failed');
              taskEventSource = null;
              activeTaskId = null;
              // Mark done in the map and clean up localStorage
              activeTaskByConversation.forEach((rec, sid) => {
                if (rec.taskId === taskId) {
                  rec.done = true;
                  try { localStorage.removeItem('activeTask_' + sid); } catch(e) {}
                }
              });
              return;

            case 'stream_done':
              taskEventSource = null;
              return;
          }
        }
      }
    } catch (err) {
      if (err.name !== 'AbortError') {
        updateProgress(progressEl, `\u274c Connection error: ${err.message}`, 'failed');
      }
      taskEventSource = null;
    }
  }

  // ── DOM helpers ───────────────────────────────────────────
  function getMessagesContainer() {
    return (
      document.getElementById('messages') ||
      document.getElementById('chat-messages') ||
      document.getElementById('message-list') ||
      document.querySelector('.messages') ||
      document.querySelector('.chat-messages') ||
      document.querySelector('.message-list') ||
      document.querySelector('[data-messages]')
    );
  }

  function appendTaskMessage(role, content) {
    const container = getMessagesContainer();
    if (!container) return;

    const el = document.createElement('div');
    el.className = role === 'user' ? 'message user-message' : 'message assistant-message';
    el.style.cssText = [
      'padding: 10px 16px',
      'margin: 6px 0',
      'border-radius: 12px',
      role === 'user'
        ? 'background:#3d3d6b; margin-left:20%; text-align:right'
        : 'background:#1e1e3a; margin-right:20%',
      'white-space: pre-wrap',
      'word-break: break-word',
    ].join(';');
    el.textContent = content;
    container.appendChild(el);
    container.scrollTop = container.scrollHeight;
    return el;
  }

  function appendTaskProgress(description) {
    const container = getMessagesContainer();
    if (!container) return document.createElement('div');

    const wrapper = document.createElement('div');
    wrapper.className = 'task-progress-card';
    wrapper.style.cssText = [
      'background: #12122a',
      'border: 1.5px solid #44446a',
      'border-radius: 12px',
      'font-size: 13px',
      'margin: 8px 0',
      'margin-right: 20%',
      'padding: 12px 16px',
    ].join(';');

    wrapper.innerHTML =
      '<div style="color:#aaa;margin-bottom:6px;font-weight:600">' +
        '\u26a1 Task Mode ' +
        '<span style="font-size:11px;font-weight:400;color:#666;margin-left:8px">' +
          description.slice(0, 60) + (description.length > 60 ? '\u2026' : '') +
        '</span>' +
      '</div>' +
      '<div class="task-status" style="color:#cdd6f4">\u23f3 Submitting\u2026</div>' +
      '<div class="task-phase-log" style="margin-top:8px;max-height:300px;overflow-y:auto;' +
        'background:#0d0d1f;border-radius:6px;padding:0;font-size:12px;display:none"></div>' +
      '<div class="task-steps" style="margin-top:8px"></div>';

    container.appendChild(wrapper);
    container.scrollTop = container.scrollHeight;
    return wrapper;
  }

  function appendPhaseLog(wrapper, icon, label, content) {
    if (!wrapper) return;
    const logEl = wrapper.querySelector('.task-phase-log');
    if (!logEl) return;
    logEl.style.display = 'block';
    const entry = document.createElement('div');
    entry.style.cssText = 'border-bottom:1px solid #1e1e3a;padding:6px 10px;';
    const header = document.createElement('div');
    header.style.cssText = 'color:#888;font-size:11px;margin-bottom:3px;';
    header.textContent = icon + ' ' + label;
    entry.appendChild(header);
    if (content) {
      const body = document.createElement('div');
      body.style.cssText = 'color:#cdd6f4;white-space:pre-wrap;word-break:break-word;';
      body.textContent = content.slice(0, 800) + (content.length > 800 ? '\u2026' : '');
      entry.appendChild(body);
    }
    logEl.appendChild(entry);
    logEl.scrollTop = logEl.scrollHeight;
    const cont = getMessagesContainer();
    if (cont) cont.scrollTop = cont.scrollHeight;
  }

  function updateProgress(wrapper, text, state) {
    if (!wrapper) return;
    const el = wrapper.querySelector('.task-status');
    if (!el) return;
    el.textContent = text;
    const colors = {
      planning: '#89b4fa', planned: '#89b4fa',
      researching: '#cba6f7',
      executing: '#fab387', completed: '#a6e3a1',
      verifying: '#f9e2af',
      failed: '#f38ba8', default: '#cdd6f4',
    };
    el.style.color = colors[state] || colors.default;
    const cont = getMessagesContainer();
    if (cont) cont.scrollTop = cont.scrollHeight;
  }

  function renderStepList(wrapper, steps) {
    if (!wrapper) return;
    const el = wrapper.querySelector('.task-steps');
    if (!el) return;
    // Backend emits step_id (not id) — use step_id for both the element id and display
    el.innerHTML = steps.map(s => {
      const sid = s.step_id !== undefined ? s.step_id : s.id;
      return `<div id="step-${sid}" style="color:#666;padding:2px 0;font-size:12px">` +
             `\u2b1c <span>${sid}. ${s.description}</span></div>`;
    }).join('');
  }

  function highlightStep(wrapper, stepId, state) {
    if (!wrapper) return;
    const el = wrapper.querySelector(`#step-${stepId}`);
    if (!el) return;
    const icons = { running: '🔄', done: '✅', failed: '❌' };
    const colors = { running: '#fab387', done: '#a6e3a1', failed: '#f38ba8' };
    const icon = el.querySelector('span');
    el.style.color = colors[state] || '#888';
    el.firstChild.textContent = (icons[state] || '⬜') + ' ';
    const cont = getMessagesContainer();
    if (cont) cont.scrollTop = cont.scrollHeight;
  }

  // ── Bootstrap ─────────────────────────────────────────────
  // Expose key functions to module scope so switchSession() and page-load
  // recovery can call them without breaking the IIFE encapsulation.
  function _doBootstrap() {
    _streamTaskEvents   = streamTaskEvents;
    _appendTaskProgress = appendTaskProgress;
    injectTaskToggle();
    interceptSend();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _doBootstrap);
  } else {
    _doBootstrap();
  }

})();

// ── Page-load recovery: restore in-progress agent tasks from localStorage ─────
// Runs after the IIFE has bootstrapped (_streamTaskEvents is now set).
async function recoverActiveTasksFromStorage() {
  // Scan for all activeTask_* keys
  const entries = [];
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (key && key.startsWith('activeTask_')) {
        try {
          const val = JSON.parse(localStorage.getItem(key));
          if (val && val.taskId && val.sessionId) entries.push(val);
        } catch(e) {}
      }
    }
  } catch(e) { return; }

  if (!entries.length) return;

  for (const { taskId, sessionId } of entries) {
    try {
      // Ask backend if the task is still alive
      const res = await fetch('/agent/task/' + taskId);
      if (!res.ok) {
        // Task not found — clean up stale localStorage entry
        try { localStorage.removeItem('activeTask_' + sessionId); } catch(e) {}
        continue;
      }
      const state = await res.json();
      if (state.done) {
        // Task already finished — clean up
        try { localStorage.removeItem('activeTask_' + sessionId); } catch(e) {}
        continue;
      }

      // Task is still running — register in the in-memory map if not already there
      if (!activeTaskByConversation.has(sessionId)) {
        activeTaskByConversation.set(sessionId, { taskId, progressEl: null, done: false });
      }

      // If the recovered task belongs to the session currently shown, show the card
      if (sessionId === currentSessionId && _streamTaskEvents && _appendTaskProgress) {
        const freshCard = _appendTaskProgress('(recovering task\u2026)');
        activeTaskByConversation.get(sessionId).progressEl = freshCard;
        _streamTaskEvents(taskId, freshCard);
      }
      // If the session is different from the current one, the card will be shown
      // when the user switches back to that session (switchSession handles it).
    } catch(e) {
      console.warn('[TaskRecovery] Could not recover task', taskId, e);
    }
  }
}
