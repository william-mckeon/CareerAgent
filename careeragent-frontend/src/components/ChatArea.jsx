import MessageBubble from './MessageBubble';

export default function ChatArea({ messages, sanitize }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem', padding: '0.5rem 0' }}>
      {messages.length === 0 ? (
        <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', flexDirection: 'column', gap: '1.25rem', opacity: 0.5, padding: '2rem 0' }}>
          <div style={{ fontSize: '2.5rem', opacity: 0.8 }}>⚡</div>
          <h2 style={{ margin: 0, fontWeight: 500, letterSpacing: '-0.02em', fontSize: '1.25rem', color: 'var(--text-dim)' }}>CareerAgent</h2>
          <p style={{ margin: 0, color: 'var(--text-dim)', fontSize: '0.85rem', maxWidth: '320px', textAlign: 'center', lineHeight: 1.5 }}>
            Start a conversation, upload a resume, or try <code style={{ fontFamily: 'var(--font-mono)', fontSize: '0.8rem', background: 'var(--surface)', padding: '0.1rem 0.3rem', borderRadius: '4px', color: 'var(--text)' }}>/plan</code>, <code style={{ fontFamily: 'var(--font-mono)', fontSize: '0.8rem', background: 'var(--surface)', padding: '0.1rem 0.3rem', borderRadius: '4px', color: 'var(--text)' }}>/tailor</code>, <code style={{ fontFamily: 'var(--font-mono)', fontSize: '0.8rem', background: 'var(--surface)', padding: '0.1rem 0.3rem', borderRadius: '4px', color: 'var(--text)' }}>/recommend-jobs</code>.
          </p>
        </div>
      ) : (
        messages.map((m, i) => <MessageBubble key={i} message={m} sanitize={sanitize} />)
      )}
      <div style={{ height: '1rem' }} />
    </div>
  );
}
