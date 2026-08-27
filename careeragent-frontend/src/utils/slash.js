const COMMANDS = {
  tailor: { expand: "Tailor my resume to this job. Use your `tailor` skill.", mode: null },
  'ats-check': { expand: "Run an ATS keyword-coverage check. Use your `ats-check` skill.", mode: null },
  'quantify-bullets': { expand: "Quantify and strengthen these resume bullets. Use your `quantify-bullets` skill.", mode: null },
  'cover-letter': { expand: "Draft a cover letter. Use your `cover-letter` skill.", mode: null },
  'linkedin-review': { expand: "Review my LinkedIn profile. Use your `linkedin-review` skill.", mode: null },
  'recommend-jobs': { expand: "Find and score roles that fit me. Use your `recommend-jobs` skill.", mode: null },
  'deep-review': { expand: "Do a deep line-level review of my code. Use your `deep-code-review` skill.", mode: null },
  'content-ideas': { expand: "Turn my projects into LinkedIn post ideas. Use your `code-content-ideas` skill.", mode: null },
  'review-repos': { expand: "Refresh my projects library from GitHub repos.", mode: null },
  reminders: { expand: "Check my application tracker: which are due for follow-up?", mode: null },
  fetch: { expand: "Fetch this job posting from its URL and summarize it.", mode: null },
  plan: { expand: "Plan this task: investigate, then propose a step-by-step plan.", mode: 'plan' },
};

export function expandSlash(text) {
  const s = (text || '').trim();
  if (!s.startsWith('/')) return { text, mode: null };
  const [name, ...restArr] = s.slice(1).split(' ');
  const cmdName = (name || '').trim().toLowerCase();
  const rest = restArr.join(' ').trim();
  const cmd = COMMANDS[cmdName];
  if (!cmd) return { text, mode: null };
  return { text: cmd.expand + (rest ? `\n\n${rest}` : ''), mode: cmd.mode || null };
}
