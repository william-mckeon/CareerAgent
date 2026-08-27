export default function Sidebar({ conversations, activeId, onNew, onSelect, onDelete }) {
  return (
    <aside style={{ width: '260px', borderRight: '1px solid var(--border)', background: 'var(--surface)', display: 'flex', flexDirection: 'column', flexShrink: 0 }}>
      <div style={{ padding: '1.25rem 1rem' }}>
        <button
          onClick={onNew}
          style={{
            width: '100%', padding: '0.65rem 0.75rem', borderRadius: 'var(--radius)',
            border: '1px solid var(--border)', background: 'var(--accent)', color: '#fff',
            fontWeight: 600, fontSize: '0.85rem', cursor: 'pointer', transition: 'opacity 0.15s',
          }}
          onMouseOver={(e) => e.target.style.opacity = '0.9'}
          onMouseOut={(e) => e.target.style.opacity = '1'}
        >
          ➕ New conversation
        </button>
      </div>
      <div style={{ flex: 1, overflowY: 'auto', padding: '0 0.75rem' }}>
        {conversations.length === 0 ? (
          <p style={{ color: 'var(--text-dim)', fontSize: '0.8rem', padding: '0.5rem', textAlign: 'center' }}>
            No conversations yet — send a message to start one.
          </p>
        ) : (
          conversations.map((c) => {
            const cid = c.conversation_id || c.id;
            const title = (c.title || 'Untitled').trim() || 'Untitled';
            const isActive = cid === activeId;
            return (
              <div key={cid} style={{ display: 'flex', gap: '0.25rem', marginBottom: '0.25rem' }}>
                <button
                  onClick={() => onSelect(cid)}
                  style={{
                    flex: 1, textAlign: 'left', padding: '0.5rem 0.6rem',
                    borderRadius: '10px', border: 'none', background: isActive ? 'var(--surface-2)' : 'transparent',
                    color: isActive ? 'var(--text)' : 'var(--text-dim)', fontSize: '0.8rem',
                    fontWeight: isActive ? 600 : 400, cursor: 'pointer', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  }}
                  title={title}
                >
                  {title.slice(0, 28)}{title.length > 28 ? '…' : ''}
                </button>
                <button
                  onClick={() => onDelete(cid)}
                  title="Delete"
                  style={{
                    background: 'transparent', border: 'none', color: 'var(--text-dim)',
                    cursor: 'pointer', fontSize: '0.9rem', padding: '0.25rem', borderRadius: '6px',
                  }}
                  onMouseOver={(e) => { e.target.style.color = 'var(--text)'; e.target.style.background = 'var(--surface-2)'; }}
                  onMouseOut={(e) => { e.target.style.color = 'var(--text-dim)'; e.target.style.background = 'transparent'; }}
                >
                  🗑
                </button>
              </div>
            );
          })
        )}
      </div>
      <div style={{ padding: '0.75rem 1rem', borderTop: '1px solid var(--border)', fontSize: '0.7rem', color: 'var(--text-dim)' }}>
        CareerAgent v2.0 — React
      </div>
    </aside>
  );
}
