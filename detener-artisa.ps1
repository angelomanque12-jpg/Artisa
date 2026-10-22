$ErrorActionPreference = "Stop"

$projectWindows = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "Capstone Artisa"))
if ($projectWindows -notmatch '^[A-Za-z]:\\') {
    throw "El proyecto debe estar en una unidad de Windows."
}
$drive = $projectWindows.Substring(0, 1).ToLowerInvariant()
$tail = $projectWindows.Substring(2).Replace('\', '/')
$projectWsl = "/mnt/$drive$tail"
$keeperFile = Join-Path $PSScriptRoot ".artisa-keepalive.pid"

& wsl.exe -d Ubuntu --cd $projectWsl python3 scripts/artisa.py detener
$stopCode = $LASTEXITCODE
if (Test-Path -LiteralPath $keeperFile) {
    $savedProcessId = [int](Get-Content -LiteralPath $keeperFile -Raw)
    Stop-Process -Id $savedProcessId -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $keeperFile -Force -ErrorAction SilentlyContinue
}

if ($stopCode -eq 0) {
    Write-Host "Artisa se detuvo sin borrar datos." -ForegroundColor Green
}
exit $stopCode
