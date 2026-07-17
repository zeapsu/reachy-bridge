export const meta = {
  name: 'app-sweep',
  description: 'Parallel per-app test sweep across apps/*, one consolidated report',
  whenToUse: 'Post-merge or pre-deploy health check. args: array of app dirs, or omit to auto-discover apps/*/.',
  phases: [
    { title: 'Discover', detail: 'list app dirs (skipped if args given)', model: 'haiku' },
    { title: 'Test', detail: 'one low-effort agent per app runs its tests', model: 'haiku' },
  ],
}

const RESULT = {
  type: 'object',
  properties: {
    app: { type: 'string' },
    command: { type: 'string' },
    passed: { type: 'integer' },
    failed: { type: 'integer' },
    errors: { type: 'array', items: { type: 'string' }, description: 'failing test names + one-line cause each' },
  },
  required: ['app', 'command', 'passed', 'failed', 'errors'],
}

let apps = Array.isArray(args) ? args : null
if (!apps) {
  const DISCOVER = { type: 'object', properties: { dirs: { type: 'array', items: { type: 'string' } } }, required: ['dirs'] }
  const d = await agent(
    'List the testable app directories in this repo: `ls -d apps/*/`, keeping only dirs that contain a tests/ directory or test_*.py files. Return relative paths only.',
    { phase: 'Discover', label: 'discover', model: 'haiku', effort: 'low', schema: DISCOVER },
  )
  apps = d ? d.dirs : []
}
if (!apps.length) return { error: 'no app dirs found' }
log(`sweeping ${apps.length} apps: ${apps.join(', ')}`)

const results = (await parallel(apps.map(app => () =>
  agent(
    `From the repository root, run \`uv run pytest ${app}\` (per AGENTS.md: shared uv workspace, never activate the venv). Tests only, no fixes. Report pass/fail counts and, for each failure, the test name plus a one-line root-cause guess.`,
    { phase: 'Test', label: app, model: 'haiku', effort: 'low', schema: RESULT },
  )
))).filter(Boolean)

const failing = results.filter(r => r.failed > 0)
return {
  summary: `${results.length - failing.length}/${results.length} apps green`,
  failing,
  all: results,
}
