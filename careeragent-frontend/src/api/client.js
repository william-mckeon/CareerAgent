const API_URL = '/api';
const API_KEY = import.meta.env.VITE_CAREERAGENT_API_KEY || '';

export const headers = {
  'Content-Type': 'application/json',
  'X-API-Key': API_KEY,
};

export async function healthCheck() {
  const res = await fetch(`${API_URL}/health`, {
    headers: { 'X-API-Key': API_KEY },
    signal: AbortSignal.timeout(5000),
  });
  if (!res.ok) return { status: 'unknown' };
  const data = await res.json();
  return { status: data.status || 'unknown' };
}

export async function chat(messages, reasoning_effort = null, mode = null, conversation_id = null) {
  const payload = { messages };
  if (reasoning_effort) payload.reasoning_effort = reasoning_effort;
  if (mode) payload.mode = mode;
  if (conversation_id) payload.conversation_id = conversation_id;

  const res = await fetch(`${API_URL}/chat`, {
    method: 'POST',
    headers,
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  return res;
}

export async function listConversations() {
  const res = await fetch(`${API_URL}/conversations`, { headers: { 'X-API-Key': API_KEY } });
  if (!res.ok) return [];
  return res.json();
}

export async function getConversation(cid) {
  const res = await fetch(`${API_URL}/conversations/${cid}`, { headers: { 'X-API-Key': API_KEY } });
  if (!res.ok) return null;
  return res.json();
}

export async function deleteConversation(cid) {
  const res = await fetch(`${API_URL}/conversations/${cid}`, {
    method: 'DELETE',
    headers: { 'X-API-Key': API_KEY },
  });
  return res.ok;
}
