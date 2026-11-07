export type SetupShell = 'powershell' | 'bash'

export function defaultSetupShell(platform: string): SetupShell {
  return /^win|windows/i.test(platform) ? 'powershell' : 'bash'
}

export function codexSetupCommand(shell: SetupShell, apiUrl: string, spaceId = '<SPACE_ID>'): string {
  const quote = (value: string) => shell === 'powershell'
    ? `'${value.replace(/'/g, "''")}'`
    : `'${value.replace(/'/g, "'\\''")}'`
  const install = "python -m pip install -e '.[mcp]'"
  const setup = [
    'python -m mnemotrace.agent_setup',
    '--client codex',
    '--api-url', quote(apiUrl),
    '--space-id', quote(spaceId),
    '--prompt-api-key',
    shell === 'powershell'
      ? "--python (python -c 'import sys; print(sys.executable)')"
      : '--python "$(command -v python)"',
    '--install-codex',
    '--doctor',
  ].join(' ')

  // Windows PowerShell 5.1 不支持 &&；安装成功后才运行配置步骤。
  return shell === 'powershell'
    ? `${install}\nif ($LASTEXITCODE -eq 0) {\n  ${setup}\n}`
    : `${install} && \\\n  ${setup}`
}
