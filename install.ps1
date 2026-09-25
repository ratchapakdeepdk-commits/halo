# HALO installer for Windows (run via install.bat).
#   -CiSmoke   CI only: skip Ollama + model download, do not open the panel.
param([switch]$CiSmoke)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

function Say($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}
function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function Find-Python {
    # Resolve a real python.exe once, then always call it by full path.
    foreach ($c in @(@("py", "-3"), @("python"))) {
        if (-not (Have $c[0])) { continue }
        try {
            $args2 = @($c | Select-Object -Skip 1) + @("-c", "import sys; print(sys.executable if sys.version_info >= (3, 10) else '')")
            $exe = (& $c[0] @args2 2>$null | Select-Object -Last 1)
            if ($exe -and (Test-Path $exe)) { return $exe.Trim() }
        } catch {}
    }
    return $null
}
function Ollama-Up {
    try { Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 3 | Out-Null; return $true } catch { return $false }
}

# 1. Python >= 3.10
$py = Find-Python
if (-not $py) {
    if (-not (Have winget)) { throw "Python 3.10+ is required: install it from https://www.python.org/downloads/ and re-run." }
    Say "Installing Python 3.12 (winget)..."
    winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
    Refresh-Path
    $py = Find-Python
    if (-not $py) { throw "Python was installed but not found; open a new window and re-run install.bat." }
}
Say "Python: $py"

# 2. Ollama (the local model server)
if (-not $CiSmoke -and -not (Ollama-Up)) {
    if (-not (Have ollama)) {
        if (-not (Have winget)) { throw "Install Ollama from https://ollama.com/download and re-run." }
        Say "Installing Ollama (winget)..."
        winget install -e --id Ollama.Ollama --accept-package-agreements --accept-source-agreements
        Refresh-Path
    }
    Say "Starting Ollama..."
    $app = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama app.exe"
    if (Test-Path $app) { Start-Process $app } else { Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden }
    for ($i = 0; $i -lt 30 -and -not (Ollama-Up); $i++) { Start-Sleep 1 }
    if (-not (Ollama-Up)) { throw "Ollama did not start. Open the Ollama app once, then re-run install.bat." }
    Say "Ollama is running."
}

# 3. HALO itself
Say "Installing HALO..."
& $py -m pip install --user --upgrade --quiet "$here"
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
$scripts = (& $py -c "import sysconfig; print(sysconfig.get_path('scripts', 'nt_user'))").Trim()
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$scripts*") {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$scripts", "User")
    Refresh-Path
    Say "Added $scripts to your PATH (new terminals will see the 'halo' command)."
}

# 4. Pick + pull models for this GPU, then choose which agents (Claude Code / Codex / Gemini)
#    use HALO from the ones installed
if (-not $CiSmoke) {
    # Interactive: shows the models that fit this PC and the agents found, lets you pick
    # (Enter = recommended models / all agents).
    $setupArgs = @("-m", "halo", "setup")
    if (-not ((Have claude) -or (Have codex) -or (Have gemini))) {
        Write-Host "   (No Claude Code / Codex / Gemini CLI found: HALO works from the terminal; run 'halo agents add <name>' after installing one.)"
    }
    & $py @setupArgs
    if ($LASTEXITCODE -ne 0) { throw "halo setup failed" }
}

# 5. Desktop shortcut to the control panel
$pyw = Join-Path (Split-Path -Parent $py) "pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = $py }
$desktop = [Environment]::GetFolderPath("Desktop")
if ($desktop) {
    $sh = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desktop "HALO.lnk"))
    $sh.TargetPath = $pyw
    $sh.Arguments = "-m halo gui"
    $sh.Description = "HALO control panel (hybrid / frontier only)"
    $sh.Save()
    Say "Desktop shortcut created: HALO"
}

& $py -m halo --version
if ($CiSmoke) { Say "CI smoke install OK"; exit 0 }
Say "Done. Optional: run 'halo tune' (or 'Auto-pick' in the panel) to find the best local models for this PC."
Start-Process $pyw -ArgumentList "-m", "halo", "gui"
