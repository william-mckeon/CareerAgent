export default function MessageBubble({ message, sanitize }) {
  const isUser = message.role === 'user';
  return (
    <div style={{
      width: '100%',
      padding: '0.75rem 0',
      borderBottom: '1px solid var(--border)',
      display: 'flex',
      flexDirection: 'column',
      gap: '0.25rem',
    }}>
      <div style={{
        display: 'flex',
        alignItems: 'flex-start',
        gap: '0.75rem',
        maxWidth: '720px',
        margin: '0 auto',
        padding: '0 1rem',
      }}>
        <div style={{
          flex: 1,
          fontSize: '0.92rem', lineHeight: 1.6,
          color: 'var(--text)', wordBreak: 'break-word',
        }}>
          {!isUser && message.reasoning && (
            <details style={{ marginBottom: '0.75rem' }}>
              <summary style={{
                fontSize: '0.76rem', color: 'var(--text-dim)',
                cursor: 'pointer', userSelect: 'none', fontWeight: 500,
                letterSpacing: '0.01em',
              }}>Show thinking</summary>
              <div style={{
                marginTop: '0.5rem', padding: '0.6rem 0.75rem',
                background: 'var(--bg)', borderRadius: '8px',
                border: '1px solid var(--border)',
                color: 'var(--text-dim)', fontSize: '0.85rem',
                lineHeight: 1.5, whiteSpace: 'pre-wrap',
              }}>
                {sanitize(message.reasoning)}
              </div>
            </details>
          )}
          <div style={{ whiteSpace: 'pre-wrap', fontWeight: isUser ? 500 : 400 }}>
            {isUser ? message.content : sanitize(message.content)}
          </div>
        </div>
      </div>
    </div>
  );
}
