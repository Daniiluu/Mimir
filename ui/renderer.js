// ── State labels ──────────────────────────────────────────────────────────
const STATE_CONFIG = {
  idle:       { label: 'EN ESPERA',     cls: 'state-idle',      sub: 'Di <em>Hey Mimir</em> para activar' },
  recording:  { label: 'ESCUCHANDO',    cls: 'state-recording', sub: 'Grabando tu orden...' },
  thinking:   { label: 'PROCESANDO',    cls: 'state-thinking',  sub: 'Consultando a Gemini...' },
  speaking:   { label: 'RESPONDIENDO',  cls: 'state-speaking',  sub: 'Mimir está hablando...' }
};

// ── DOM refs ──────────────────────────────────────────────────────────────
const appEl       = document.getElementById('app');
const stateLabel  = document.getElementById('state-label');
const statusText  = document.getElementById('status-text');
const statusSub   = document.getElementById('status-sub');
const chatEl      = document.getElementById('chat-messages');
const actionList  = document.getElementById('action-list');
const placeholder = document.getElementById('chat-placeholder');

let currentState = 'idle';

// ── Apply visual state ─────────────────────────────────────────────────────
function applyState(value) {
  if (!STATE_CONFIG[value]) return;
  const cfg = STATE_CONFIG[value];
  currentState = value;

  // Remove all state classes then add the right one
  Object.values(STATE_CONFIG).forEach(c => appEl.classList.remove(c.cls));
  appEl.classList.add(cfg.cls);

  if (stateLabel) stateLabel.textContent = cfg.label;
  if (statusText) statusText.textContent = cfg.label;
  if (statusSub) statusSub.innerHTML = cfg.sub;
}

// ── Add chat bubble ────────────────────────────────────────────────────────
function addBubble(role, text) {
  placeholder.style.display = 'none';

  const wrap = document.createElement('div');
  wrap.className = `bubble ${role}`;

  const label = document.createElement('div');
  label.className = 'bubble-label';
  label.textContent = role === 'mimir' ? 'MIMIR' : 'TÚ';

  const content = document.createElement('div');
  content.textContent = text;

  wrap.appendChild(label);
  wrap.appendChild(content);
  chatEl.appendChild(wrap);
  chatEl.scrollTop = chatEl.scrollHeight;
}

// ── Add action log entry ───────────────────────────────────────────────────
function addAction(tool, result) {
  const li = document.createElement('li');
  li.textContent = `⚙ ${tool}: ${result}`;
  actionList.appendChild(li);
  // Keep last 30 entries
  while (actionList.children.length > 30) actionList.removeChild(actionList.firstChild);
  actionList.scrollTop = actionList.scrollHeight;
}

// ── Listen to Mimir events ─────────────────────────────────────────────────
window.api.onMimirEvent((event) => {
  switch (event.type) {
    case 'state':
      applyState(event.value);
      break;

    case 'chat_user':
      addBubble('user', event.text);
      break;

    case 'chat_mimir':
      addBubble('mimir', event.text);
      break;

    case 'action':
      addAction(event.tool, event.result);
      break;

    case 'log':
      if (event.text) {
        const cleanMsg = event.text.replace(/^🐍\s*(\[Mimir-Voz\]\s*)?/, '');
        if (cleanMsg && !cleanMsg.includes('Cargando modelos')) {
          addAction('VOZ', cleanMsg);
        }
      }
      break;

    case 'exit':
      applyState('idle');
      if (stateLabel) stateLabel.textContent = 'PROCESO TERMINADO';
      if (statusText) statusText.textContent = 'DESCONECTADO';
      if (statusSub) statusSub.textContent  = 'Mimir se ha detenido.';
      break;
  }
});

// ── Window controls ────────────────────────────────────────────────────────
document.getElementById('btn-min').addEventListener('click', () => window.api.window.minimize());
document.getElementById('btn-max').addEventListener('click', () => window.api.window.maximize());
document.getElementById('btn-close').addEventListener('click', () => window.api.window.close());

// ── Clear chat ─────────────────────────────────────────────────────────────
document.getElementById('btn-clear').addEventListener('click', () => {
  chatEl.innerHTML = '';
  placeholder.style.display = '';
});

// ── Initial state ──────────────────────────────────────────────────────────
applyState('idle');
