/**
 * Atlas Command Deck frontend.
 * Handles mode switching, prompt shortcuts, chat requests, and reasoning traces.
 */

const state = {
    currentMode: 'agent_v2',
    isLoading: false,
    sessionId: null,
    health: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => document.querySelectorAll(selector);

const elements = {
    sidebar: $('#sidebar'),
    sidebarOverlay: $('#sidebarOverlay'),
    menuToggle: $('#menuToggle'),
    rightPanel: $('#rightPanel'),
    closePanel: $('#closePanel'),
    clearChat: $('#clearChat'),
    apiKeyInput: $('#apiKeyInput'),
    saveApiKey: $('#saveApiKey'),
    clearApiKey: $('#clearApiKey'),
    messageInput: $('#messageInput'),
    sendBtn: $('#sendBtn'),
    messagesContainer: $('#messagesContainer'),
    messages: $('#messages'),
    testCases: $('#testCases'),
    headerMode: $('#headerMode'),
    inputModeIndicator: $('#inputModeIndicator'),
    panelContent: $('#panelContent'),
    deploymentLabel: $('#deploymentLabel'),
};

const modeDetails = {
    chatbot: { label: 'Chatbot', badge: 'Chatbot · Ready', icon: '◌' },
    agent_v1: { label: 'Agent v1', badge: 'Agent v1 · Ready', icon: '◈' },
    agent_v2: { label: 'Agent v2', badge: 'Agent v2 · Ready', icon: '✦' },
};

const prompts = {
    chatbot: [
        'Explain the best time to visit Da Lat.',
        'What should I pack for a 3-day trip to Da Nang?',
        'Give me a short itinerary for Hanoi.'
    ],
    agent_v1: [
        'Find a hotel in Da Lat under 500k and summarize the plan.',
        'Check weather for Ho Chi Minh City this weekend.',
        'Suggest activities in Nha Trang if it rains.'
    ],
    agent_v2: [
        'I want to go to Da Lat this weekend. Check the weather and suggest a hotel under 500k.',
        'Plan a 2-day trip to Hue with weather, hotel, and activity recommendations.',
        'I need a backup plan if it rains in Vung Tau tomorrow.'
    ]
};

document.addEventListener('DOMContentLoaded', () => {
    initSession();
    initApiKey();
    initModes();
    initComposer();
    initSidebar();
    initPanel();
    initClearChat();
    loadPrompts();
    refreshHealth();
});

function initSession() {
    const storageKey = 'atlas-command-session-id';
    state.sessionId = localStorage.getItem(storageKey);
    if (!state.sessionId) {
        state.sessionId = (crypto.randomUUID ? crypto.randomUUID() : `session-${Date.now()}-${Math.random().toString(16).slice(2)}`);
        localStorage.setItem(storageKey, state.sessionId);
    }
}

function initApiKey() {
    const storageKey = 'atlas-command-api-key';
    const savedKey = localStorage.getItem(storageKey) || '';
    elements.apiKeyInput.value = savedKey;

    elements.saveApiKey.addEventListener('click', () => {
        const key = elements.apiKeyInput.value.trim();
        if (!key) {
            localStorage.removeItem(storageKey);
            return;
        }
        localStorage.setItem(storageKey, key);
    });

    elements.clearApiKey.addEventListener('click', () => {
        localStorage.removeItem(storageKey);
        elements.apiKeyInput.value = '';
    });
}

function initModes() {
    $$('.mode-btn').forEach((button) => {
        button.addEventListener('click', () => {
            setMode(button.dataset.mode);
            loadPrompts();
        });
    });
    setMode(state.currentMode);
}

function setMode(mode) {
    state.currentMode = mode;
    $$('.mode-btn').forEach((button) => button.classList.toggle('active', button.dataset.mode === mode));

    const details = modeDetails[mode] || modeDetails.agent_v2;
    elements.headerMode.textContent = details.badge;
    elements.inputModeIndicator.textContent = `${details.icon} ${details.label}`;
}

function initComposer() {
    elements.messageInput.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' && !event.shiftKey) {
            event.preventDefault();
            sendMessage();
        }
    });

    elements.messageInput.addEventListener('input', autoResizeTextarea);
    elements.sendBtn.addEventListener('click', sendMessage);
}

function autoResizeTextarea() {
    elements.messageInput.style.height = 'auto';
    elements.messageInput.style.height = `${Math.min(elements.messageInput.scrollHeight, 160)}px`;
}

function initSidebar() {
    elements.menuToggle.addEventListener('click', () => {
        elements.sidebar.classList.toggle('open');
        elements.sidebarOverlay.classList.toggle('active', elements.sidebar.classList.contains('open'));
    });

    elements.sidebarOverlay.addEventListener('click', closeSidePanels);
}

function closeSidePanels() {
    elements.sidebar.classList.remove('open');
    elements.rightPanel.classList.remove('open');
    elements.sidebarOverlay.classList.remove('active');
}

function initPanel() {
    elements.closePanel.addEventListener('click', () => {
        elements.rightPanel.classList.remove('open');
        if (!elements.sidebar.classList.contains('open')) {
            elements.sidebarOverlay.classList.remove('active');
        }
    });
}

function initClearChat() {
    elements.clearChat.addEventListener('click', () => {
        const storageKey = 'atlas-command-session-id';
        localStorage.removeItem(storageKey);
        initSession();
        elements.messages.innerHTML = renderWelcomeMessage();
        elements.panelContent.innerHTML = renderEmptyState('⌘', 'No trace yet', 'Send a prompt to reveal steps, metrics, and tool calls here.');
    });
}

function loadPrompts() {
    const items = prompts[state.currentMode] || prompts.agent_v2;
    elements.testCases.innerHTML = items.map((text) => `
        <button class="prompt-btn" type="button" data-prompt="${escapeHtml(text)}">
            <span class="prompt-arrow">↗</span>
            <span>${escapeHtml(text)}</span>
        </button>
    `).join('');

    elements.testCases.querySelectorAll('.prompt-btn').forEach((button) => {
        button.addEventListener('click', () => {
            elements.messageInput.value = button.dataset.prompt || '';
            autoResizeTextarea();
            elements.messageInput.focus();
        });
    });
}

async function refreshHealth() {
    try {
        const response = await fetch('/health');
        if (!response.ok) return;
        const data = await response.json();
        state.health = data;
        elements.deploymentLabel.textContent = `${data.provider} / ${data.model}`;
    } catch (_error) {
        elements.deploymentLabel.textContent = 'Environment unavailable';
    }
}

async function sendMessage() {
    const text = elements.messageInput.value.trim();
    if (!text || state.isLoading) return;

    const apiKey = elements.apiKeyInput.value.trim();
    if (!apiKey) {
        addMessage('bot', 'Error: Please enter the auth key before sending a message.');
        elements.apiKeyInput.focus();
        return;
    }

    state.isLoading = true;
    elements.sendBtn.disabled = true;
    elements.messageInput.value = '';
    autoResizeTextarea();

    addMessage('user', text);
    const typingIndicator = showTypingIndicator();

    try {
        const response = await fetch('/ask', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-API-Key': apiKey,
                'X-User-Id': 'demo-user',
            },
            body: JSON.stringify({
                message: text,
                question: text,
                mode: state.currentMode,
                session_id: state.sessionId,
                user_id: 'demo-user',
            }),
        });

        if (!response.ok) {
            let errorMessage = 'Server error';
            try {
                const errorPayload = await response.json();
                errorMessage = errorPayload.error || errorMessage;
            } catch (_jsonError) {
                // Ignore parse failures and surface generic server error.
            }
            throw new Error(errorMessage);
        }

        const data = await response.json();
        typingIndicator.remove();
        addBotResponse(data);
        updateTracePanel(data);
    } catch (error) {
        typingIndicator.remove();
        addMessage('bot', `Error: ${error.message}`);
    } finally {
        state.isLoading = false;
        elements.sendBtn.disabled = false;
        elements.messageInput.focus();
    }
}

function addMessage(type, text) {
    const message = document.createElement('article');
    message.className = `message ${type === 'user' ? 'user-message' : 'bot-message'}`;

    const avatar = type === 'user' ? 'ME' : 'AD';
    message.innerHTML = `
        <div class="avatar">${avatar}</div>
        <div class="message-body">
            <div class="bubble ${type === 'user' ? 'user-bubble' : ''}">
                <div class="message-text">${formatAnswer(text)}</div>
            </div>
        </div>
    `;

    elements.messages.appendChild(message);
    scrollToBottom();
}

function addBotResponse(data) {
    const message = document.createElement('article');
    message.className = 'message bot-message';

    const steps = Array.isArray(data.steps) ? data.steps : [];
    const metrics = data.metrics || {};
    const answer = formatAnswer(data.answer || '');

    message.innerHTML = `
        <div class="avatar">AD</div>
        <div class="message-body">
            <div class="bubble bubble-answer">
                <div class="message-text">${answer}</div>
            </div>
            <div class="metric-row">
                <span class="metric-chip">${metrics.latency_ms || 0} ms</span>
                <span class="metric-chip">${metrics.total_tokens || 0} tokens</span>
                <span class="metric-chip">${metrics.steps_count || 0} steps</span>
                <span class="metric-chip">${escapeHtml(data.mode || state.currentMode)}</span>
            </div>
            ${steps.length ? `<button class="trace-toggle" type="button">Open reasoning panel</button>` : ''}
        </div>
    `;

    const toggleButton = message.querySelector('.trace-toggle');
    if (toggleButton) {
        toggleButton.addEventListener('click', () => {
            elements.rightPanel.classList.add('open');
            elements.sidebarOverlay.classList.add('active');
        });
    }

    elements.messages.appendChild(message);
    scrollToBottom();
}

function updateTracePanel(data) {
    const steps = Array.isArray(data.steps) ? data.steps : [];
    const metrics = data.metrics || {};

    if (!steps.length) {
        elements.panelContent.innerHTML = renderEmptyState('⌘', 'No trace available', 'This response did not include a reasoning trail.');
        return;
    }

    elements.panelContent.innerHTML = `
        <div class="metric-grid">
            <div class="metric-card">
                <span>Latency</span>
                <strong>${metrics.latency_ms || 0} ms</strong>
            </div>
            <div class="metric-card">
                <span>Tokens</span>
                <strong>${metrics.total_tokens || 0}</strong>
            </div>
            <div class="metric-card">
                <span>Steps</span>
                <strong>${metrics.steps_count || 0}</strong>
            </div>
        </div>
        <div class="trace-list">
            ${steps.map((step, index) => `
                <section class="trace-step">
                    <div class="trace-index">${index + 1}</div>
                    <div class="trace-copy">
                        <strong>${escapeHtml(getStepLabel(step.type))}</strong>
                        <p>${escapeHtml(step.content || '')}</p>
                    </div>
                </section>
            `).join('')}
        </div>
    `;
    elements.rightPanel.classList.add('open');
}

function getStepLabel(type) {
    const labels = {
        thought: 'Think',
        action: 'Action',
        observation: 'Observation',
        final_answer: 'Answer',
        error: 'Error',
        retry: 'Retry',
    };
    return labels[type] || type || 'Step';
}

function showTypingIndicator() {
    const indicator = document.createElement('article');
    indicator.className = 'message bot-message typing';
    indicator.innerHTML = `
        <div class="avatar">AD</div>
        <div class="message-body">
            <div class="bubble typing-bubble">
                <div class="dots"><span></span><span></span><span></span></div>
                <p>Agent is reasoning…</p>
            </div>
        </div>
    `;
    elements.messages.appendChild(indicator);
    scrollToBottom();
    return indicator;
}

function scrollToBottom() {
    elements.messagesContainer.scrollTop = elements.messagesContainer.scrollHeight;
}

function formatAnswer(text) {
    if (!text) return '';
    let html = escapeHtml(String(text));
    html = html.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\n/g, '<br>');
    return html;
}

function renderWelcomeMessage() {
    return `
        <article class="message bot-message welcome-message">
            <div class="avatar">AD</div>
            <div class="message-body">
                <div class="bubble">
                    <p class="eyebrow">Start here</p>
                    <h3>Mission deck online</h3>
                    <p>This new layout removes the old editorial look and keeps attention on the travel task itself.</p>
                    <ul>
                        <li>Weather windows for a destination</li>
                        <li>Hotels within a budget</li>
                        <li>Activities that match the forecast</li>
                    </ul>
                </div>
            </div>
        </article>
    `;
}

function renderEmptyState(icon, title, text) {
    return `
        <div class="empty-state">
            <div class="empty-icon">${icon}</div>
            <h3>${escapeHtml(title)}</h3>
            <p>${escapeHtml(text)}</p>
        </div>
    `;
}

function escapeHtml(text) {
    return String(text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}
