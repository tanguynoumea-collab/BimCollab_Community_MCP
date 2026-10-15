<#
.SYNOPSIS
  Installe BimCollab Community MCP : CLI BIMcollab Zoom + serveur MCP "bim-assistant" pour Claude Code.

.DESCRIPTION
  1. Crée un environnement Python (.venv) à la racine du dépôt.
  2. Installe les deux paquets : cli-anything-bczoom (CLI + pilotage live de Zoom) et bim-assistant (MCP).
  3. Enregistre le serveur MCP dans Claude Code :
       -Scope project (défaut) : écrit .mcp.json à la racine du dépôt (actif quand Claude Code est ouvert ici)
       -Scope user             : enregistre le serveur pour tous vos projets (via "claude mcp add")
  4. Installe le skill "cli-anything-bczoom" (déjà présent dans .claude\skills pour le projet ;
     copié dans %USERPROFILE%\.claude\skills avec -Scope user).
  5. Vérifie l'installation.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\install.ps1
  powershell -ExecutionPolicy Bypass -File .\install.ps1 -Scope user -Author "prenom.nom@entreprise.ch"
#>
param(
    [ValidateSet("project", "user")] [string] $Scope = "project",
    [string] $Author = "",
    [string] $Python = ""
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    OK  $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    !!  $m" -ForegroundColor Yellow }

if ($env:OS -ne "Windows_NT") { throw "BIMcollab Zoom et le pilotage live ne fonctionnent que sous Windows." }

# --- 1. Python ---------------------------------------------------------------------------------
Step "Recherche de Python >= 3.11"
$candidates = @()
if ($Python) { $candidates += ,@($Python) }
$candidates += ,@("py", "-3"); $candidates += ,@("python")
$py = $null
foreach ($c in $candidates) {
    try {
        $exe = $c[0]; $pre = @($c | Select-Object -Skip 1)
        $v = & $exe @pre -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and [version]$v -ge [version]"3.11") { $py = $c; Ok "$($c -join ' ') -> Python $v"; break }
    } catch { }
}
if (-not $py) { throw "Python 3.11+ introuvable. Installez-le depuis https://www.python.org/downloads/ (cochez 'Add to PATH')." }

# --- 2. venv + paquets -------------------------------------------------------------------------
Step "Création de l'environnement .venv"
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    $exe = $py[0]; $pre = @($py | Select-Object -Skip 1)
    & $exe @pre -m venv (Join-Path $Root ".venv")
}
Ok $venvPy

Step "Installation des paquets (quelques minutes la première fois : ifcopenshell, pywinauto...)"
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install --quiet -e "$Root\bimcollab-zoom\agent-harness[live,test]"
if ($LASTEXITCODE -ne 0) { throw "Échec d'installation de cli-anything-bczoom" }
& $venvPy -m pip install --quiet -e "$Root\bim-assistant"
if ($LASTEXITCODE -ne 0) { throw "Échec d'installation de bim-assistant" }
Ok "cli-anything-bczoom + bim-assistant installés"

# --- 3. Enregistrement MCP ---------------------------------------------------------------------
Step "Enregistrement du serveur MCP 'bim-assistant' (scope: $Scope)"
$envBlock = [ordered]@{ PYTHONIOENCODING = "utf-8" }
if ($Author) { $envBlock.BCZOOM_AUTHOR = $Author }
if ($Scope -eq "project") {
    $cfg = [ordered]@{ mcpServers = [ordered]@{ "bim-assistant" = [ordered]@{
        type = "stdio"; command = $venvPy; args = @("-m", "bim_assistant.server"); env = $envBlock } } }
    $json = $cfg | ConvertTo-Json -Depth 6
    [System.IO.File]::WriteAllText((Join-Path $Root ".mcp.json"), $json, (New-Object System.Text.UTF8Encoding($false)))
    Ok ".mcp.json écrit — ouvrez Claude Code dans ce dossier et acceptez le serveur 'bim-assistant'."
} else {
    $claude = Get-Command claude -ErrorAction SilentlyContinue
    if ($claude) {
        $envArgs = @(); foreach ($k in $envBlock.Keys) { $envArgs += @("--env", "$k=$($envBlock[$k])") }
        & claude mcp remove --scope user bim-assistant 2>$null | Out-Null
        & claude mcp add --scope user @envArgs bim-assistant -- $venvPy -m bim_assistant.server
        Ok "Serveur enregistré pour l'utilisateur (claude mcp add)."
    } else {
        Warn "CLI 'claude' introuvable : ajoutez ce bloc à la config MCP utilisateur de Claude Code :"
        Write-Host ("    command: $venvPy`n    args: -m bim_assistant.server")
    }
}

# --- 4. Skill ----------------------------------------------------------------------------------
Step "Skill cli-anything-bczoom"
$skillSrc = Join-Path $Root "bimcollab-zoom\agent-harness\cli_anything\bczoom\skills\SKILL.md"
$projSkill = Join-Path $Root ".claude\skills\cli-anything-bczoom"
New-Item -ItemType Directory -Force $projSkill | Out-Null
Copy-Item $skillSrc (Join-Path $projSkill "SKILL.md") -Force
Ok "Skill projet : .claude\skills\cli-anything-bczoom"
if ($Scope -eq "user") {
    $userSkill = Join-Path $env:USERPROFILE ".claude\skills\cli-anything-bczoom"
    New-Item -ItemType Directory -Force $userSkill | Out-Null
    $txt = (Get-Content $skillSrc -Raw -Encoding UTF8).Replace(
        '`.venv\Scripts\cli-anything-bczoom.exe` (repository root)', "``$Root\.venv\Scripts\cli-anything-bczoom.exe``")
    [System.IO.File]::WriteAllText((Join-Path $userSkill "SKILL.md"), $txt, (New-Object System.Text.UTF8Encoding($false)))
    Ok "Skill utilisateur : $userSkill"
}

# --- 5. Vérification ---------------------------------------------------------------------------
Step "Vérification"
& $venvPy -c "import bim_assistant.server, cli_anything.bczoom.utils.zoom_live; print('imports OK')"
$cli = Join-Path $Root ".venv\Scripts\cli-anything-bczoom.exe"
$info = & $cli --json app info 2>$null
if ($LASTEXITCODE -eq 0) { Ok "BIMcollab Zoom détecté"; Write-Host $info } else { Warn "BIMcollab Zoom non détecté (installez-le ou définissez BCZOOM_EXE). L'analyse IFC fonctionne quand même." }

Write-Host "`nInstallation terminée." -ForegroundColor Green
Write-Host "Ouvrez Claude Code dans ce dossier, puis par exemple :"
Write-Host '  « Ouvre mon IFC <chemin> et fais-moi une Smart View des murs par résistance au feu »'
Write-Host '  « Fais une liste Zoom du cubage béton par type d''objet, colorée par type »'
