import { useState } from 'react';

export default function ChatInput({ onSubmit, disabled, reasoningEffort, setReasoningEffort }) {
  const [text, setText] = useState('');

  const handleSubmit = (e) => {
    e.preventDefault();
    const trimmed = text.trim();
    if (!trimmed || disabled) return;
    setText('');
    onSubmit(trimmed);
  };

  return (
    <div style={{
      border: '1.5px solid var(--border)',
      borderRadius: 'var(--radius)',
      background: 'var(--surface)',
      padding: '0.75rem',
      display: 'flex',
      flexDirection: 'column',
      gap: '0.5rem',
      boxShadow: '0 4px 20px rgba(0,0,0,0.25)',
    }}>
      <form onSubmit={handleSubmit} style={{ display: 'flex', gap: '0.5rem', alignItems: 'flex-end' }}>
      <div style={{ flex: 1, position: 'relative' }}>
        <textarea
          rows={1}
          value={text}
          onChange={(e) => setText(e.target.value)}
          disabled={disabled}
          placeholder="Message CareerAgent..."
          style={{
            width: '100%', resize: 'none', padding: '0.85rem 1rem',
            borderRadius: 'var(--radius)', border: 'none',
            background: 'var(--surface)', color: 'var(--text)',
            fontFamily: 'var(--font-sans)', fontSize: '0.9rem', outline: 'none',
            minHeight: '44px', maxHeight: '140px', lineHeight: 1.4,
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              handleSubmit(e);
            }
          }}
          onInput={(e) => { e.target.style.height = 'auto'; e.target.style.height = Math.min(e.target.scrollHeight, 140) + 'px'; }}
        />
      </div>
      <button
        type="submit"
        disabled={disabled || !text.trim()}
        style={{
          padding: '0.85rem 1.25rem', borderRadius: 'var(--radius)',
          border: 'none', background: disabled ? 'var(--surface-2)' : 'var(--accent)',
          color: disabled ? 'var(--text-dim)' : '#fff', fontWeight: 600,
          cursor: disabled ? 'not-allowed' : 'pointer', transition: 'opacity 0.15s',
        }}
      >
        Send
      </button>
    </form>
    </div>
  );
}
