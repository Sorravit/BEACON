// AI Assistant Web UI - JavaScript

const messagesEl = document.getElementById('messages');
const emptyState = document.getElementById('empty-state');
const textarea   = document.getElementById('user-input');
const sendBtn    = document.getElementById('send-btn');
const statusDot  = document.getElementById('status-dot');
const statusText = document.getElementById('status-text');
const toast      = document.getElementById('toast');

let isStreaming = false;

// ── Markdown renderer (lightweight) ──────────────────────────────────────────
function renderMarkdown(text) {
  let html = text
    // Escape HTML special chars first
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    // Fenced code blocks ```lang\n...\n```
    .replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) =>
      `<pre><code class="lang-${lang}">${code.trim()}</code></pre>`)
    // Inline code `...`
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    // Bold **text**
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    // Italic *text*
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    // Headers ### ## #
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^## (.+)$/gm,  '<h2>$1</h2>')
    .replace(/^# (.+)$/gm,   '<h1>$1</h1>')
    // Blockquote > text
    .replace(/^> (.+)$/gm, '<blockquote>$1</blockquote>')
    // Unordered list items - item
    .replace(/^[\-\*] (.+)$/gm, '<li>$1</li>')
    // Ordered list items 1. item
    .replace(/^\d+\. (.+)$/gm, '<li>$1</li>')
    // Wrap consecutive <li> in <ul>
    .replace(/(<li>[\s\S]*?<\/li>)(\n<li>[\s\S]*?<\/li>)*/g, m => `<ul>${m}</ul>`)
    // Links [text](url)
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    // Horizontal rule ---
    .replace(/^---$/gm, '<hr>')
    // Paragraph breaks (double newline)
    .replace(/\n\n+/g, '</p><p>')
    // Single newlines inside text
    .replace(/\n/g, '<br>');

  // Wrap in paragraph if doesn't start with a block element
  if (!/^<(h[1-6]|ul|ol|pre|blockquote|hr)/.test(html)) {
    html = '<p>' + html + '</p>';
  }
  return html;
}

// ── Create message bubble ─────────────────────────────────────────────────────
function createMessage(role) {
  const row = document.createElement('div');
  row.className = `msg ${role}`;

  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  avatar.textContent = role === 'user' ? '👤' : '🤖';

  const bubble = document.createElement('div');
  bubble.className = 'bubble';

  row.appendChild(avatar);
  row.appendChild(bubble);
  messagesEl.appendChild(row);
  scrollToBottom();
  return bubble;
}

// ── Create tool card ──────────────────────────────────────────────────────────
function createToolCard(bubble, name, args) {
  const card = document.createElement('div');
  card.className = 'tool-card';
  card.innerHTML = `
    <div class="tool-header">
      <span class="tool-name">⚙ ${escHtml(name)}</span>
      <span class="tool-badge">running</span>
    </div>
    ${args ? `<div class="tool-args">${escHtml(args)}</div>` : ''}
    <div class="tool-result-text"></div>`;
  bubble.appendChild(card);
  scrollToBottom();
  return card;
}

function escHtml(s) {
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Status helpers ────────────────────────────────────────────────────────────
function setStatus(state, text) {
  statusDot.className = 'status-dot' + (state !== 'ready' ? ' ' + state : '');
  statusText.textContent = text;
}

function scrollToBottom() {
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function showToast(msg) {
  toast.textContent = msg;
  toast.classList.add('show');
  setTimeout(() => toast.classList.remove('show'), 2500);
}

// ── Send message ──────────────────────────────────────────────────────────────
async function sendMessage() {
  const text = textarea.value.trim();
  if (!text || isStreaming) return;

  // Hide empty state
  if (emptyState) emptyState.style.display = 'none';

  // Show user bubble
  const userBubble = createMessage('user');
  userBubble.innerHTML = renderMarkdown(text);

  textarea.value = '';
  textarea.style.height = 'auto';
  isStreaming = true;
  sendBtn.disabled = true;
  setStatus('thinking', 'Thinking…');

  // Create AI bubble
  const aiBubble = createMessage('ai');
  const textNode = document.createElement('div');
  aiBubble.appendChild(textNode);

  let accumulated = '';
  let activeToolCard = null;
  let toolCardMap = {};

  try {
    const resp = await fetch('/chat/stream', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: text})
    });

    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const {done, value} = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, {stream: true});
      const lines = buffer.split('\n');
      buffer = lines.pop(); // keep incomplete line

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed) continue;

        // SSE lines start with optional " " but our server sends " {...}"
        const jsonStr = trimmed.startsWith('data:')
          ? trimmed.slice(5).trim()
          : trimmed.trim();

        let event;
        try { event = JSON.parse(jsonStr); } catch { continue; }

        if (event.type === 'tool') {
          setStatus('thinking', `Running ${event.name}…`);
          activeToolCard = createToolCard(aiBubble, event.name, event.args);
          toolCardMap[event.name] = activeToolCard;
          // Auto-open tasks panel when a background task is started
          if (event.name === 'delegate_background_task') {
            setTimeout(() => { openTasksPanel(); refreshTasksList(); }, 800);
          }
        } else if (event.type === 'result') {
          const card = toolCardMap[event.name] || activeToolCard;
          if (card) {
            const badge = card.querySelector('.tool-badge');
            const resultEl = card.querySelector('.tool-result-text');
            if (badge) { badge.textContent = 'done'; badge.style.background = '#1a2a1a'; }
            if (resultEl) resultEl.textContent = event.content || '';
          }
          setStatus('thinking', 'Processing…');
        } else if (event.type === 'token') {
          accumulated += event.content || '';
          textNode.innerHTML = renderMarkdown(accumulated);
          scrollToBottom();
          setStatus('thinking', 'Responding…');
        } else if (event.type === 'done') {
          textNode.innerHTML = renderMarkdown(accumulated);
          scrollToBottom();
          // Refresh badge after task operations
          refreshTasksBadge();
        } else if (event.type === 'error') {
          const errDiv = document.createElement('div');
          errDiv.style.cssText = 'color:#f87171;font-size:13px;margin-top:6px';
          errDiv.textContent = '⚠ ' + (event.content || 'Unknown error');
          aiBubble.appendChild(errDiv);
          setStatus('error', 'Error');
        }
      }
    }
  } catch (err) {
    console.error('Stream error:', err);
    const errDiv = document.createElement('div');
    errDiv.style.cssText = 'color:#f87171;font-size:13px';
    errDiv.textContent = '⚠ Connection error: ' + err.message;
    aiBubble.appendChild(errDiv);
    setStatus('error', 'Error');
  } finally {
    isStreaming = false;
    sendBtn.disabled = false;
    setStatus('ready', 'Ready');
    scrollToBottom();
  }
}

// ── Clear conversation ────────────────────────────────────────────────────────
async function clearChat() {
  await fetch('/chat/clear', {method: 'POST'});
  messagesEl.innerHTML = '';
  if (emptyState) {
    emptyState.style.display = 'flex';
    messagesEl.appendChild(emptyState);
  }
  showToast('Conversation cleared');
}

// ── Suggestion chips ──────────────────────────────────────────────────────────
function useSuggestion(text) {
  textarea.value = text;
  autoResize();
  textarea.focus();
}

// ── Auto-resize textarea ──────────────────────────────────────────────────────
function autoResize() {
  textarea.style.height = 'auto';
  textarea.style.height = Math.min(textarea.scrollHeight, 160) + 'px';
}

// ── Event listeners ───────────────────────────────────────────────────────────
sendBtn.addEventListener('click', sendMessage);

textarea.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

textarea.addEventListener('input', autoResize);

window.addEventListener('load', () => {
  textarea.focus();
  setStatus('ready', 'Ready');
  refreshTasksBadge();
});

// ── Background Tasks Panel ────────────────────────────────────────────────────
let taskLogStreams = {};  // name -> EventSource

function openTasksPanel() {
  const panel = document.getElementById('tasks-panel');
  panel.classList.add('open');
  refreshTasksList();
  initDrag(panel);
}

function closeTasksPanel() {
  document.getElementById('tasks-panel').classList.remove('open');
}

// ── Drag-to-move for floating panel ──────────────────────────────────────────
let _dragInit = false;
function initDrag(panel) {
  if (_dragInit) return;
  _dragInit = true;
  const header = panel.querySelector('.tasks-panel-header');
  if (!header) return;

  let dragging = false;
  let startX, startY, startLeft, startTop;

  header.addEventListener('mousedown', e => {
    // Don't drag if clicking a button
    if (e.target.tagName === 'BUTTON') return;
    dragging = true;
    const rect = panel.getBoundingClientRect();
    startX = e.clientX;
    startY = e.clientY;
    startLeft = rect.left;
    startTop  = rect.top;
    // Switch from right-anchored to left-anchored positioning
    panel.style.right  = 'auto';
    panel.style.left   = startLeft + 'px';
    panel.style.top    = startTop  + 'px';
    e.preventDefault();
  });

  document.addEventListener('mousemove', e => {
    if (!dragging) return;
    const dx = e.clientX - startX;
    const dy = e.clientY - startY;
    const newLeft = Math.max(0, Math.min(window.innerWidth  - 100, startLeft + dx));
    const newTop  = Math.max(0, Math.min(window.innerHeight - 60,  startTop  + dy));
    panel.style.left = newLeft + 'px';
    panel.style.top  = newTop  + 'px';
  });

  document.addEventListener('mouseup', () => { dragging = false; });

  // ── Resize handles ────────────────────────────────────────────────
  const MIN_W = 280, MIN_H = 200;
  let resizing = false, resizeDir = '', resizeStartX, resizeStartY;
  let resizeStartLeft, resizeStartTop, resizeStartW, resizeStartH;

  panel.querySelectorAll('.resize-handle').forEach(handle => {
    handle.addEventListener('mousedown', e => {
      e.preventDefault();
      e.stopPropagation();
      resizing   = true;
      resizeDir  = handle.dataset.dir;
      resizeStartX = e.clientX;
      resizeStartY = e.clientY;
      const rect = panel.getBoundingClientRect();
      resizeStartLeft = rect.left;
      resizeStartTop  = rect.top;
      resizeStartW    = rect.width;
      resizeStartH    = rect.height;
      // Ensure left/top positioning
      panel.style.right  = 'auto';
      panel.style.bottom = 'auto';
      panel.style.left   = resizeStartLeft + 'px';
      panel.style.top    = resizeStartTop  + 'px';
    });
  });

  document.addEventListener('mousemove', e => {
    if (!resizing) return;
    const dx = e.clientX - resizeStartX;
    const dy = e.clientY - resizeStartY;
    let newLeft = resizeStartLeft, newTop = resizeStartTop;
    let newW = resizeStartW, newH = resizeStartH;

    if (resizeDir.includes('e'))  newW = Math.max(MIN_W, resizeStartW + dx);
    if (resizeDir.includes('s'))  newH = Math.max(MIN_H, resizeStartH + dy);
    if (resizeDir.includes('w')) {
      newW = Math.max(MIN_W, resizeStartW - dx);
      newLeft = resizeStartLeft + (resizeStartW - newW);
    }
    if (resizeDir.includes('n')) {
      newH = Math.max(MIN_H, resizeStartH - dy);
      newTop = resizeStartTop + (resizeStartH - newH);
    }

    panel.style.width  = newW    + 'px';
    panel.style.height = newH    + 'px';
    panel.style.left   = newLeft + 'px';
    panel.style.top    = newTop  + 'px';
  });

  document.addEventListener('mouseup', () => { resizing = false; });
}

async function refreshTasksList() {
  try {
    const res = await fetch('/tasks');
    const data = await res.json();
    const tasks = data.tasks || [];
    renderTasksList(tasks);
  } catch(e) {
    console.error('Failed to fetch tasks:', e);
  }
}

async function refreshTasksBadge() {
  try {
    const res = await fetch('/tasks');
    const data = await res.json();
    const running = (data.tasks || []).filter(t => t.running).length;
    const badge = document.getElementById('tasks-badge');
    if (badge) {
      badge.textContent = running;
      badge.classList.toggle('visible', running > 0);
    }
    const count = document.getElementById('tasks-count');
    if (count) count.textContent = data.tasks.length;
  } catch(e) {}
  setTimeout(refreshTasksBadge, 3000);
}

function renderTasksList(tasks) {
  const list = document.getElementById('tasks-list');
  const empty = document.getElementById('tasks-empty');
  if (!tasks.length) {
    if (empty) empty.style.display = 'block';
    // Remove old task items
    list.querySelectorAll('.task-item').forEach(el => el.remove());
    return;
  }
  if (empty) empty.style.display = 'none';

  // Remove items no longer present
  list.querySelectorAll('.task-item').forEach(el => {
    if (!tasks.find(t => t.name === el.dataset.name)) el.remove();
  });

  tasks.forEach(task => {
    let item = list.querySelector(`.task-item[data-name="${task.name}"]`);
    if (!item) {
      item = document.createElement('div');
      item.className = 'task-item';
      item.dataset.name = task.name;
      const n = task.name;
      item.innerHTML = `
        <div class="task-item-header" onclick="toggleTaskLog('${n}')">
          <div class="task-name">
            <span class="task-running-dot ${task.running ? '' : 'stopped'}" id="dot-${n}"></span>
            ${escHtml(n)}
          </div>
          <div class="task-actions" id="actions-${n}">
            <button class="task-stop-btn" id="stop-${n}"
              onclick="event.stopPropagation();stopTask('${n}')"
              ${task.running ? '' : 'disabled style="opacity:.35"'}>
              &#9646; Stop
            </button>
            <button class="task-clear-btn" id="clear-${n}"
              onclick="event.stopPropagation();showClearConfirm('${n}')">
              &#128465; Clear
            </button>
            <button class="task-expand-btn">&#9662;</button>
            <div class="task-confirm" id="confirm-${n}">
              <div class="task-confirm-title">What would you like to do?</div>
              <div class="task-confirm-btns">
                <button class="task-confirm-btn clear-only"
                  onclick="event.stopPropagation();clearLogOnly('${n}')">
                  &#128465; Clear log only
                </button>
                <button class="task-confirm-btn stop-clear"
                  onclick="event.stopPropagation();stopAndClear('${n}')">
                  &#9646; Stop task + clear log
                </button>
                <button class="task-confirm-btn cancel"
                  onclick="event.stopPropagation();hideClearConfirm('${n}')">
                  Cancel
                </button>
              </div>
            </div>
          </div>
        </div>
        <div class="task-log" id="log-${n}">
          <div class="task-log-content" id="logcontent-${n}"></div>
        </div>`;
      list.appendChild(item);
    } else {
      // Update running state
      const dot = document.getElementById(`dot-${task.name}`);
      const stopBtn = document.getElementById(`stop-${task.name}`);
      if (dot) dot.className = `task-running-dot ${task.running ? '' : 'stopped'}`;
      if (stopBtn) {
        stopBtn.disabled = !task.running;
        stopBtn.style.opacity = task.running ? '1' : '0.35';
      }
    }
  });
}

function toggleTaskLog(name) {
  const logEl = document.getElementById(`log-${name}`);
  if (!logEl) return;
  const isOpen = logEl.classList.toggle('expanded');
  if (isOpen) {
    startLogStream(name);
  } else {
    stopLogStream(name);
  }
}

async function startLogStream(name) {
  if (taskLogStreams[name]) return; // already streaming
  const contentEl = document.getElementById(`logcontent-${name}`);
  if (!contentEl) return;

  // Mark as active with an AbortController so we can cancel it
  const controller = new AbortController();
  taskLogStreams[name] = controller;

  try {
    const resp = await fetch(`/tasks/${name}/logs`, {signal: controller.signal});
    if (!resp.ok) {
      appendLogLine(contentEl, name, `[error: HTTP ${resp.status}]`);
      return;
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';

    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buf += decoder.decode(value, {stream: true});
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const raw of lines) {
        const trimmed = raw.trim();
        if (!trimmed) continue;
        let jsonStr = /^\s*/.test(trimmed) ? trimmed.replace(/^\s*/, '') : trimmed;
        let event;
        try { event = JSON.parse(jsonStr); } catch { continue; }
        if (event.line !== undefined && event.line !== '') {
          appendLogLine(contentEl, name, event.line);
        }
        if (event.done) {
          const dot = document.getElementById(`dot-${name}`);
          const stopBtn = document.getElementById(`stop-${name}`);
          if (dot) dot.className = 'task-running-dot stopped';
          if (stopBtn) stopBtn.disabled = true;
          delete taskLogStreams[name];
          return;
        }
      }
    }
  } catch(err) {
    if (err.name !== 'AbortError') {
      appendLogLine(contentEl, name, `[stream ended: ${err.message}]`);
    }
  } finally {
    delete taskLogStreams[name];
  }
}

function appendLogLine(contentEl, name, text) {
  const line = document.createElement('div');
  line.className = 'task-log-line';
  line.textContent = text;
  contentEl.appendChild(line);
  const logEl = document.getElementById(`log-${name}`);
  if (logEl) logEl.scrollTop = logEl.scrollHeight;
}

function stopLogStream(name) {
  if (taskLogStreams[name]) {
    taskLogStreams[name].abort(); // AbortController
    delete taskLogStreams[name];
  }
}

async function stopTask(name) {
  const btn = document.getElementById(`stop-${name}`);
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  try {
    await fetch(`/tasks/${name}/stop`, {method: 'POST'});
    showToast(`Task '${name}' stopped`);
    await refreshTasksList();
    refreshTasksBadge();
  } catch(e) {
    showToast('Failed to stop task');
    if (btn) { btn.disabled = false; btn.textContent = '■ Stop'; }
  }
}

function showClearConfirm(name) {
  document.querySelectorAll('.task-confirm.show').forEach(el => {
    if (el.id !== 'confirm-' + name) el.classList.remove('show');
  });
  const confirm = document.getElementById('confirm-' + name);
  if (!confirm) return;
  confirm.classList.toggle('show');
  if (confirm.classList.contains('show')) {
    setTimeout(() => {
      document.addEventListener('click', function handler(e) {
        if (!confirm.contains(e.target)) {
          confirm.classList.remove('show');
          document.removeEventListener('click', handler);
        }
      });
    }, 0);
  }
}

function hideClearConfirm(name) {
  const el = document.getElementById('confirm-' + name);
  if (el) el.classList.remove('show');
}

async function clearLogOnly(name) {
  hideClearConfirm(name);
  try {
    await fetch('/tasks/' + name + '/log', {method: 'DELETE'});
    const contentEl = document.getElementById('logcontent-' + name);
    if (contentEl) contentEl.innerHTML = '';
    showToast("Log cleared for '" + name + "'");
  } catch(e) {
    showToast('Failed to clear log');
  }
}

async function stopAndClear(name) {
  hideClearConfirm(name);
  const btn = document.getElementById('stop-' + name);
  if (btn) { btn.disabled = true; btn.textContent = '...'; }
  try {
    await fetch('/tasks/' + name + '/stop-and-clear', {method: 'POST'});
    const contentEl = document.getElementById('logcontent-' + name);
    if (contentEl) contentEl.innerHTML = '';
    showToast("Task '" + name + "' stopped and log cleared");
    await refreshTasksList();
    refreshTasksBadge();
  } catch(e) {
    showToast('Failed to stop and clear');
    if (btn) { btn.disabled = false; btn.textContent = 'Stop'; }
  }
}
