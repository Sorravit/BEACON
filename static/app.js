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
});