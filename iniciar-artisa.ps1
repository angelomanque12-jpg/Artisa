$ErrorActionPreference = "Stop"

$projectWindows = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "Capstone Artisa"))
if ($projectWindows -notmatch '^[A-Za-z]:\\') {
    throw "El proyecto debe estar en una unidad de Windows."
}
$drive = $projectWindows.Substring(0, 1).ToLowerInvariant()
$tail = $projectWindows.Substring(2).Replace('\', '/')
$projectWsl = "/mnt/$drive$tail"
$keeperFile = Join-Path $PSScriptRoot ".artisa-keepalive.pid"

# Docker se ejecuta dentro de Ubuntu. Este proceso mantiene WSL disponible después
# de terminar el comando, pero solo se crea cuando el usuario inicia Artisa a mano.
# Se abre oculto: no crea otra ventana de Windows Terminal.
$keeperRunning = $false
if (Test-Path -LiteralPath $keeperFile) {
    $savedProcessId = 0
    if ([int]::TryParse((Get-Content -LiteralPath $keeperFile -Raw).Trim(), [ref]$savedProcessId)) {
        $keeperRunning = $null -ne (Get-Process -Id $savedProcessId -ErrorAction SilentlyContinue)
    }
}
if (-not $keeperRunning) {
    $keeper = Start-Process -FilePath "$env:WINDIR\System32\wsl.exe" `
        -ArgumentList @("-d", "Ubuntu", "--exec", "sleep", "2147483647") `
        -WindowStyle Hidden -PassThru
    Set-Content -LiteralPath $keeperFile -Value $keeper.Id -Encoding ascii
    Start-Sleep -Seconds 1
}

& wsl.exe -d Ubuntu --cd $projectWsl python3 scripts/artisa.py iniciar
if ($LASTEXITCODE -ne 0) {
    if (Test-Path -LiteralPath $keeperFile) {
        $savedProcessId = [int](Get-Content -LiteralPath $keeperFile -Raw)
        Stop-Process -Id $savedProcessId -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $keeperFile -Force -ErrorAction SilentlyContinue
    }
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Artisa quedo activa en http://localhost:8080" -ForegroundColor Green
Write-Host "Para detenerla desde esta carpeta: .\detener-artisa.cmd"
