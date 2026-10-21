const assert = require('node:assert/strict')
const test = require('node:test')
const path = require('node:path')
const vm = require('node:vm')
const { spawnSync } = require('node:child_process')
const { existsSync } = require('node:fs')
const esbuild = require('esbuild')

const source = esbuild.buildSync({
  entryPoints: [path.resolve(__dirname, '../src/lib/codexSetupCommand.ts')],
  bundle: true, platform: 'node', format: 'cjs', write: false,
}).outputFiles[0].text
const moduleData = { exports: {} }
vm.runInNewContext(source, { module: moduleData, exports: moduleData.exports })
const { defaultSetupShell, codexSetupCommand } = moduleData.exports

test('根据客户端系统选择终端，Windows 默认 PowerShell', () => {
  for (const platform of ['Win32', 'Win64', 'Windows', 'Mozilla/5.0 (Windows NT 10.0)']) {
    assert.equal(defaultSetupShell(platform), 'powershell')
  }
  for (const platform of ['MacIntel', 'Linux x86_64', 'Darwin', '']) {
    assert.equal(defaultSetupShell(platform), 'bash')
  }
})

test('Bash 命令成功后隐藏输入凭据，命令不接收 Key', () => {
  const command = codexSetupCommand('bash', 'https://example.invalid/api')
  assert.ok(command.startsWith("python -m pip install -e '.[mcp]' && \\\n  "))
  assert.ok(command.includes('--prompt-api-key'))
  assert.ok(!command.includes('synthetic'))
  assert.ok(command.includes('--python "$(command -v python)"'))
  assert.ok(command.endsWith('--install-codex --doctor'))
})

test('PowerShell 命令兼容 5.1，并解析当前 Python 的完整路径', () => {
  const command = codexSetupCommand('powershell', 'https://example.invalid/api')
  assert.ok(command.includes('\nif ($LASTEXITCODE -eq 0) {\n'))
  assert.ok(command.includes('--prompt-api-key'))
  assert.ok(!command.includes('synthetic'))
  assert.ok(command.includes("--python (python -c 'import sys; print(sys.executable)')"))
  assert.ok(!command.includes('&&'))
  assert.ok(!command.includes('command -v'))
})

test('在 Windows PowerShell 中校验执行顺序、失败中止及参数完整性', { skip: process.platform !== 'win32' }, () => {
  const apiUrl = "https://example.invalid/api?q='; throw 'must-stay-data'; # $()"
  for (const installExit of [0, 1]) {
    // 仅调用内存中的 python 替身，不安装包、不修改 Codex 配置。
    const script = `
      $script:calls = @()
      function python {
        $script:calls += ,@($args)
        if ($args[0] -eq '-c') {
          'C:\\Synthetic Python\\python.exe'
          $global:LASTEXITCODE = 0
        } elseif ($args[1] -eq 'pip') {
          $global:LASTEXITCODE = ${installExit}
        } else { $global:LASTEXITCODE = 0 }
      }
      ${codexSetupCommand('powershell', apiUrl)}
      ConvertTo-Json -InputObject @($script:calls) -Depth 5 -Compress
    `
    const result = spawnSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], { encoding: 'utf8' })
    assert.equal(result.status, 0, result.stderr)
    const calls = JSON.parse(result.stdout.trim())
    assert.equal(calls.length, installExit === 0 ? 3 : 1)
    assert.deepEqual(calls[0], ['-m', 'pip', 'install', '-e', '.[mcp]'])
    if (installExit === 0) {
      const setup = calls[2]
      assert.equal(setup[1], 'mnemotrace.agent_setup')
      assert.equal(setup[setup.indexOf('--api-url') + 1], apiUrl)
      assert.ok(setup.includes('--prompt-api-key'))
      assert.ok(!setup.includes('--api-key'))
      assert.equal(setup[setup.indexOf('--python') + 1], 'C:\\Synthetic Python\\python.exe')
      assert.deepEqual(setup.slice(-2), ['--install-codex', '--doctor'])
    }
  }
})

const bash = process.platform === 'win32' ? 'C:/Program Files/Git/bin/bash.exe' : 'bash'
test('实际 Bash 保留 URL 参数，安装失败时不进入隐藏输入步骤', { skip: process.platform === 'win32' && !existsSync(bash) }, () => {
  const apiUrl = "https://example.invalid/api?q='; printf must-stay-data; # $(printf injected)"
  for (const installExit of [0, 1]) {
    const script = `
      python() {
        if [ "$2" = pip ]; then
          printf 'INSTALL\\0'
          return ${installExit}
        else
          printf 'SETUP\\0'
          printf '%s\\0' "$@"
        fi
      }
      ${codexSetupCommand('bash', apiUrl)}
    `
    const result = spawnSync(bash, ['--noprofile', '--norc'], { input: script, encoding: 'utf8' })
    assert.equal(result.status, installExit, result.stderr)
    const args = result.stdout.split('\0').filter(Boolean)
    assert.equal(args[0], 'INSTALL')
    if (installExit) { assert.equal(args.length, 1); continue }
    assert.equal(args[1], 'SETUP')
    assert.equal(args[args.indexOf('--api-url') + 1], apiUrl)
    assert.ok(args.includes('--prompt-api-key'))
    assert.ok(!args.includes('--api-key'))
  }
})
