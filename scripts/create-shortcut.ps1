# Creates a desktop shortcut for Manufex (IOA desktop agent).
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\create-shortcut.ps1
$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$desktop = [Environment]::GetFolderPath('Desktop')

$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut((Join-Path $desktop 'Manufex (IOA Workbench).lnk'))
$sc.TargetPath = Join-Path $repoRoot 'start-ioa-workbench.cmd'
$sc.WorkingDirectory = $repoRoot
$sc.Description = 'Manufex - IOA desktop agent, based on Enikk v0.11.2 (github.com/gtt116/enikk)'
$sc.Save()

Write-Host "Shortcut created: $desktop\Manufex (IOA Workbench).lnk"
Write-Host "Repo root: $repoRoot"
