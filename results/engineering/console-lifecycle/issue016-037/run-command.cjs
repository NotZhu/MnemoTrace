// Reproducible engineering commands; stdout is redacted before it becomes evidence.
const { spawnSync } = require('node:child_process')
const fs = require('node:fs')
const path = require('node:path')
const root = path.resolve(__dirname, '../..')
const [label, cwd, command, ...args] = process.argv.slice(2)
const start = Date.now()
const result = spawnSync(command, args, { cwd: path.resolve(root, cwd), encoding: 'utf8', env: { ...process.env, PYTHONUTF8: '1' }, shell: false })
const redact = value => String(value ?? '').split(root).join('<REPO>').split(process.env.USERPROFILE || '<unused>').join('<HOME>')
const record = { label, command: [command, ...args], cwd, date: new Date().toISOString(), elapsed_ms: Date.now() - start,
  exit_code: result.status, error: result.error?.message, stdout: redact(result.stdout), stderr: redact(result.stderr) }
fs.appendFileSync(path.join(__dirname, 'validation.jsonl'), JSON.stringify(record) + '\n')
process.stdout.write(record.stdout)
process.stderr.write(record.stderr)
if (record.error) console.error(record.error)
process.exit(result.status ?? 1)
