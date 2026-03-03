param(
    [string]$ShortcutName = "Clocktower Bot"
)

$projectRoot = $PSScriptRoot
if (-not $projectRoot) {
    $projectRoot = (Get-Location).Path
}

$startMenuPrograms = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$shortcutPath = Join-Path $startMenuPrograms ("$ShortcutName.lnk")
$targetPath = Join-Path $projectRoot "clocktower.bat"

$wsh = New-Object -ComObject WScript.Shell
$shortcut = $wsh.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $targetPath
$shortcut.WorkingDirectory = $projectRoot
$shortcut.Description = "Start Clocktower Bot"
$shortcut.Save()

Write-Host "Created shortcut: $shortcutPath"
