import { useState, useEffect, useRef, useCallback } from 'react';
import { healthCheck, chat, listConversations, getConversation, deleteConversation } from './api/client';
import Sidebar from './components/Sidebar';
import ChatArea from './components/ChatArea';
import ChatInput from './components/ChatInput';

// Emoji error taxonomy preserved from careeragent-frontend review
const ERROR_MAP = {
  connection: '🔌 Connection error — backend unreachable.',
  timeout: '⏳ Timeout — model may be loading.',
  auth: '🔐 Auth failure — API key mismatch.',
  validation: '⚠️ Request validation error.',
  unexpected: '❌ Unexpected error.',
};

function sanitizeMarkdown(text) {
  if (!text) return text;
  // Defang images: ![alt](url) -> inert text
  let safe = text.replace(/!\[([^\]]*)\]\(([^)]*)\)/g, (m, alt, url) => {
    const defanged = url.replace(/http/g, 'hxxp');
    return `🖼️ ${(alt || 'image').trim()} (${defanged})`;
  });
  // Defang links: [text](url) -> text (hxxp://...)
  safe = safe.replace(/\[([^\]]*)\]\(([^)]*)\)/g, (m, label, url) => {
    const defanged = url.replace(/http/g, 'hxxp');
    return `${label || ''} (${defanged})`;
  });
  // Defang bare URLs
  safe = safe.replace(/https?:\/\//gi, (m) => m.replace('http', 'hxxp'));
  return safe;
}

import { expandSlash } from './utils/slash';

export default function App() {
  const [ready, setReady] = useState(false);
  const [statusMsg, setStatusMsg] = useState('Checking upstream...');
  const [messages, setMessages] = useState([]);
  const [conversationId, setConversationId] = useState(null);
  const [pending, setPending] = useState(null);
  const [mode, setMode] = useState('acceptEdits');
  const [reasoningEffort, setReasoningEffort] = useState('Default');
  const [streaming, setStreaming] = useState(false);
  const [errorBanner, setErrorBanner] = useState(null);
  const [convList, setConvList] = useState([]);
  const [uploadSeed, setUploadSeed] = useState(null);
  const streamRef = useRef(null);

  // Health gate
  useEffect(() => {
    let timer;
    let attempts = 0;
    const max = 40;
    const poll = async () => {
      attempts++;
      try {
        const health = await healthCheck();
        if (health.status === 'ok') {
          setStatusMsg('🟢 careeragent-api ready');
          setReady(true);
          return;
        } else if (health.status === 'loading') {
          setStatusMsg(`⏳ Model starting up (poll #${attempts})`);
        } else if (health.status === 'unreachable') {
          setStatusMsg('🔌 careeragent-api unreachable — upstream model down');
        } else if (health.status === 'unauthorized') {
          setStatusMsg('🔐 Key rejected — check CAREERAGENT_API_KEY');
        } else {
          setStatusMsg('⚠️ Unknown health status');
        }
      } catch (e) {
        setStatusMsg('🔌 Cannot reach careeragent-api');
      }
      if (attempts < max) {
        timer = setTimeout(poll, 3000);
      } else {
        setStatusMsg('⛔ Health gate capped — retry manually');
      }
    };
    poll();
    return () => clearTimeout(timer);
  }, []);

  const refreshConvList = useCallback(async () => {
    if (!ready) return;
    const list = await listConversations();
    setConvList(list);
  }, [ready]);

  // Load conversation list for sidebar
  useEffect(() => {
    refreshConvList();
  }, [ready, refreshConvList]);

  // Restore from URL ?c=
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const cid = params.get('c');
    if (cid && ready) {
      getConversation(cid).then((conv) => {
        if (conv) {
          setMessages(conv.messages || []);
          setConversationId(cid);
        }
      });
    }
  }, [ready]);

  const handleNewConv = () => {
    setMessages([]);
    setConversationId(null);
    setPending(null);
    window.history.pushState({}, '', window.location.pathname);
    refreshConvList();
  };

  const handleSelectConv = async (cid) => {
    const conv = await getConversation(cid);
    if (conv) {
      setMessages(conv.messages || []);
      setConversationId(cid);
      setPending(null);
      window.history.pushState({}, '', `?c=${cid}`);
    }
  };

  const handleDeleteConv = async (cid) => {
    if (!await deleteConversation(cid)) return;
    if (conversationId === cid) handleNewConv();
    refreshConvList();
  };

  const submitTurn = useCallback(async (text, isResumeSeed = false) => {
    setErrorBanner(null);
    setStreaming(true);
    const userMsg = { role: 'user', content: text };
    setMessages((prev) => [...prev, userMsg]);

    const payload = {
      messages: [...messages, userMsg].map((m) => ({ role: m.role, content: m.content })),
      reasoning_effort: reasoningEffort === 'Default' ? null : (reasoningEffort === 'Quick' ? 'low' : reasoningEffort === 'Standard' ? 'medium' : 'high'),
      mode,
      conversation_id: conversationId,
    };

    try {
      const response = await chat(payload.messages, payload.reasoning_effort, payload.mode, payload.conversation_id);
      // Stream handling simplified for reliability: read chunks
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let reasoningText = '';
      let answerText = '';
      let finishReason = '';
      let streamDone = false;
      let streamError = null;

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();
        for (const line of lines) {
          const clean = line.replace(/^data: /, '').trim();
          if (!clean || clean === ': heartbeat') continue;
          if (clean === '[DONE]') {
            streamDone = true;
            break;
          }
          if (clean.startsWith('[ERROR')) {
            streamError = clean;
            break;
          }
          try {
            const chunk = JSON.parse(clean);
            const delta = chunk.choices?.[0]?.delta || {};
            const finish = chunk.choices?.[0]?.finish_reason;
            if (delta.reasoning) {
              reasoningText += delta.reasoning;
            }
            if (delta.content) {
              answerText += delta.content;
            }
            if (finish) finishReason = finish;
          } catch (e) {
            // Skip malformed chunks silently (same behavior as sse_decoder.py)
          }
        }
        if (streamDone || streamError) break;
      }
      reader.releaseLock();

      if (streamError) {
        setErrorBanner('❌ Stream error: ' + streamError);
        setStreaming(false);
        return;
      }

      // Apply turn result
      if (pending && !isResumeSeed) {
        // Not used in basic flow; kept for future pause/resume
      }
      setMessages((prev) => {
        const assistantMsg = { role: 'assistant', content: answerText || '(no answer produced)' };
        if (reasoningText) assistantMsg.reasoning = reasoningText;
        return [...prev, assistantMsg];
      });
      // Capture conversation id from header if present
      const cid = response.headers.get('X-Conversation-Id');
      if (cid) {
        setConversationId(cid);
        window.history.pushState({}, '', `?c=${cid}`);
      }
      refreshConvList();
      setStreaming(false);
    } catch (err) {
      const msg = err.message || 'Unknown error';
      let banner = '❌ ' + msg;
      if (msg.includes('401') || msg.includes('key')) banner = '🔐 ' + msg;
      else if (msg.includes('503') || msg.includes('504')) banner = '⏳ ' + msg;
      else if (msg.includes('400') || msg.includes('422')) banner = '⚠️ ' + msg;
      else if (msg.includes('connect') || msg.includes('502')) banner = '🔌 ' + msg;
      setErrorBanner(banner);
      setStreaming(false);
    }
  }, [messages, conversationId, reasoningEffort, mode, pending]);

  const handleInput = (text) => {
    if (pending) {
      setPending(null);
    }
    const expanded = expandSlash(text);
    if (expanded.mode) setMode(expanded.mode);
    submitTurn(expanded.text);
  };

  if (!ready) {
    return (
      <div style={{
        height: '100vh', display: 'flex', flexDirection: 'column',
        alignItems: 'center', justifyContent: 'center',
        gap: '1.5rem', fontFamily: 'var(--font-sans)',
        background: 'var(--bg)', color: 'var(--text)',
      }}>
        <div style={{ fontSize: '2.5rem', opacity: 0.9 }}>⚡</div>
        <h1 style={{ margin: 0, fontSize: '1.75rem', fontWeight: 600, letterSpacing: '-0.03em' }}>CareerAgent</h1>
        <p style={{ color: 'var(--text-dim)', margin: 0, fontSize: '0.95rem' }}>{statusMsg}</p>
        <div style={{ width: '200px', height: '3px', background: 'var(--surface-2)', borderRadius: '2px', overflow: 'hidden', marginTop: '0.5rem' }}>
          <div style={{ width: '100%', height: '100%', background: 'var(--accent)', animation: 'pulse 2s infinite' }} />
        </div>
        <style>{`
          @keyframes pulse { 0%, 100% { opacity: 0.5; } 50% { opacity: 1; } }
        `}</style>
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', height: '100vh', overflow: 'hidden' }}>
      <Sidebar
        conversations={convList}
        activeId={conversationId}
        onNew={handleNewConv}
        onSelect={handleSelectConv}
        onDelete={handleDeleteConv}
      />
      <main style={{ flex: 1, display: 'flex', flexDirection: 'column', background: 'var(--bg)', position: 'relative' }}>
        {/* Header */}
        <header style={{ padding: '1rem 1.5rem', borderBottom: '1px solid var(--border)' }}>
          <div style={{ maxWidth: '768px', margin: '0 auto', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <h1 style={{ margin: 0, fontSize: '1.1rem', fontWeight: 600, letterSpacing: '-0.03em', color: 'var(--text)' }}>CareerAgent</h1>
          </div>
        </header>

        {/* Error banner */}
        {errorBanner && (
          <div style={{ padding: '0.75rem 1.5rem', background: 'rgba(248,113,113,0.08)', borderBottom: '1px solid rgba(248,113,113,0.15)', color: 'var(--error)', fontSize: '0.85rem', fontWeight: 500 }}>
            {errorBanner}
          </div>
        )}

        {/* Chat stream — centered modern layout */}
        <div style={{ flex: 1, overflowY: 'auto', padding: '1.5rem', display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
          <div style={{ width: '100%', maxWidth: '768px' }}>
            <ChatArea messages={messages} sanitize={sanitizeMarkdown} />
          </div>
        </div>

        {/* Input */}
        <div style={{ padding: '1rem 1.5rem', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '0.5rem' }}>
          <div style={{ width: '100%', maxWidth: '768px' }}>
            <ChatInput onSubmit={handleInput} disabled={streaming} reasoningEffort={reasoningEffort} setReasoningEffort={setReasoningEffort} />
          </div>
        </div>
      </main>
    </div>
  );
}
