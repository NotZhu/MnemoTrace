const assert = require('node:assert/strict')
const test = require('node:test')
const path = require('node:path')
const vm = require('node:vm')
const { spawnSync } = require('node:child_process')
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

test('Bash 命令保留成功后执行，并按 Bash 规则转义参数', () => {
  const command = codexSetupCommand('bash', 'https://example.invalid/api', "synthetic'key")
  assert.ok(command.startsWith("python -m pip install -e '.[mcp]' && \\\n  "))
  assert.ok(command.includes("--api-key 'synthetic'\\''key'"))
  assert.ok(command.includes('--python "$(command -v python)"'))
  assert.ok(command.endsWith('--install-codex --doctor'))
})

test('PowerShell 命令兼容 5.1，并解析当前 Python 的完整路径', () => {
  const command = codexSetupCommand('powershell', 'https://example.invalid/api', "synthetic'key")
  assert.ok(command.includes('\nif ($LASTEXITCODE -eq 0) {\n'))
  assert.ok(command.includes("--api-key 'synthetic''key'"))
  assert.ok(command.includes("--python (python -c 'import sys; print(sys.executable)')"))
  assert.ok(!command.includes('&&'))
  assert.ok(!command.includes('command -v'))
})

test('在 Windows PowerShell 中校验执行顺序、失败中止及参数完整性', { skip: process.platform !== 'win32' }, () => {
  const apiUrl = 'https://example.invalid/api'
  const apiKey = "synthetic'; throw 'must-stay-data'; # $()"
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
      ${codexSetupCommand('powershell', apiUrl, apiKey)}
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
      assert.equal(setup[setup.indexOf('--api-key') + 1], apiKey)
      assert.equal(setup[setup.indexOf('--python') + 1], 'C:\\Synthetic Python\\python.exe')
      assert.deepEqual(setup.slice(-2), ['--install-codex', '--doctor'])
    }
  }
})
