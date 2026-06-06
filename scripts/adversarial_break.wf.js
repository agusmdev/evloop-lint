export const meta = {
  name: 'evloop-adversarial-break',
  description: 'Breaker agents generate realistic FastAPI evasion cases; judge labels true escapes against the running linter',
  phases: [
    { title: 'Break', detail: 'breakers generate realistic cases across axes' },
    { title: 'Judge', detail: 'label realism + run linter + classify escapes' },
  ],
}

const ROOT = '/Users/agusmarchi/Desktop/spikes/evloop-linter-claude-api'

const RUN_INSTRUCTIONS = `HOW TO RUN THE LINTER on a case directory CASE_DIR:
  cd ${ROOT} && PYTHONPATH=src python3 -m evloop_lint.cli CASE_DIR --no-color --format json --confidence possible
It prints a JSON array of findings (each has code, confidence, path, line, blocker, chain). Empty array [] means NOTHING was flagged.`

const TOOL_SUMMARY = `evloop-lint detects sync event-loop-BLOCKING calls reachable from async code in FastAPI, interprocedurally across files, optimistic policy (unresolved = safe). Known blockers (data registry): time.sleep, requests.get/post/..., urllib urlopen, open(), socket recv/send, subprocess run/call/Popen.wait, psycopg2.connect, sqlite3 execute, pymysql.connect, bcrypt.hashpw/checkpw, hashlib.pbkdf2_hmac/scrypt, zlib/gzip.compress. Offload escape hatches (safe): asyncio.to_thread, loop.run_in_executor, anyio.to_thread.run_sync, starlette run_in_threadpool. Schedulers analyzed on-loop: loop.call_soon/call_later/call_at, asyncio.create_task/ensure_future. Entry points: async def (on-loop); plain def endpoints are threadpooled (off-loop) but a plain def reached DIRECTLY from async is analyzed on-loop. Default max-depth 4.`

const MANDATE = `MANDATE: cases MUST be REALISTIC, idiomatic FastAPI/async Python a real dev could plausibly write. FORBIDDEN: eval/exec, base64/__import__ obfuscation, deliberately absurd constructs. The goal is GENERIC weaknesses, never gimmicks.`

const axes = [
  'cross-file nesting just under and just over max-depth (router->service->repo->driver chains)',
  'instance method calls on dependency-injected services (self.repo.find() where the blocker is in the repo class), and methods passed as callbacks',
  'offload edge cases: blocker in the receiver/constructor that builds the offloaded callable; functools.partial bound-arg blockers; lambda wrappers; anyio.from_thread.run re-entry',
  'implicit-call channels: blocking call behind a @property or descriptor; iterating a sync generator/cursor that blocks; async context managers (async with) whose underlying code blocks',
  'false-positive bait: correct idiomatic code that should NOT be flagged (plain def endpoints with blockers, properly offloaded work, fully async stacks, sync helpers only called from sync def)',
]

const CASE_SCHEMA = {
  type: 'object', additionalProperties: false,
  properties: {
    cases: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        properties: {
          name: { type: 'string' },
          mechanism: { type: 'string' },
          intent: { type: 'string', enum: ['false_negative', 'false_positive'] },
          files: { type: 'array', items: { type: 'object', additionalProperties: false, properties: { path: { type: 'string' }, content: { type: 'string' } }, required: ['path', 'content'] } },
          expected: { type: 'string' },
          realism_note: { type: 'string' },
        },
        required: ['name', 'mechanism', 'intent', 'files', 'expected', 'realism_note'],
      },
    },
  },
  required: ['cases'],
}

phase('Break')
const batches = await parallel(axes.map((axis, i) => () =>
  agent(
    `You are a BREAKER attacking a static linter. ${TOOL_SUMMARY}\n\n${MANDATE}\n\nGenerate 2 distinct realistic CASES for this axis: ${axis}\n\nEach case is a small multi-file FastAPI-ish project (relative paths like app/main.py, app/services/x.py). Include genuine BLOCKERS the tool might MISS (intent=false_negative) and/or correct code that might be wrongly flagged (intent=false_positive). Real imports, real call chains.`,
    { label: `break:axis${i}`, phase: 'Break', schema: CASE_SCHEMA, agentType: 'general-purpose' },
  ),
))
const allCases = batches.filter(Boolean).flatMap((b) => b.cases || [])
log(`generated ${allCases.length} candidate cases`)

phase('Judge')
const VERDICT_SCHEMA = {
  type: 'object', additionalProperties: false,
  properties: {
    name: { type: 'string' },
    is_realistic: { type: 'boolean' },
    true_label: { type: 'string', enum: ['should_flag', 'should_not_flag'] },
    linter_flagged: { type: 'boolean' },
    is_escape: { type: 'boolean' },
    escape_kind: { type: 'string', enum: ['false_negative', 'false_positive', 'none'] },
    analysis: { type: 'string' },
    linter_output: { type: 'string' },
  },
  required: ['name', 'is_realistic', 'true_label', 'linter_flagged', 'is_escape', 'escape_kind', 'analysis', 'linter_output'],
}

const verdicts = await parallel(allCases.map((c) => () =>
  agent(
    `You are the JUDGE/LABELER for the evloop-lint adversarial loop. ${TOOL_SUMMARY}\n\n${RUN_INSTRUCTIONS}\n\nCandidate case (JSON):\n${JSON.stringify(c)}\n\nSteps:\n1. D=$(mktemp -d); write each file under D (mkdir -p parents).\n2. Run the linter on D; capture JSON.\n3. GROUND TRUTH via asyncio semantics: does the target actually block the loop on an async path? (plain def endpoints=threadpooled=not blocking; properly offloaded=not blocking; sync helper only called from sync code=irrelevant.)\n4. is_escape = linter disagrees with ground truth. false_negative=missed real blocker; false_positive=flagged correct code.\n5. Judge realism strictly.\nReturn the verdict with raw linter JSON in linter_output.`,
    { label: `judge:${c.name}`, phase: 'Judge', schema: VERDICT_SCHEMA, agentType: 'general-purpose' },
  ),
))

const v = verdicts.filter(Boolean)
const escapes = v.filter((x) => x.is_realistic && x.is_escape)
return {
  total_cases: allCases.length,
  judged: v.length,
  realistic: v.filter((x) => x.is_realistic).length,
  escapes: escapes.length,
  false_negatives: escapes.filter((x) => x.escape_kind === 'false_negative').map((x) => ({ name: x.name, analysis: x.analysis })),
  false_positives: escapes.filter((x) => x.escape_kind === 'false_positive').map((x) => ({ name: x.name, analysis: x.analysis })),
  escape_detail: escapes.map((x) => ({ name: x.name, kind: x.escape_kind, analysis: x.analysis, output: x.linter_output })),
  all_cases: allCases,
}