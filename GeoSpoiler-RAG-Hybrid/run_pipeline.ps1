# GeoSpoiler-RAG Pipeline Runner
# Sets UTF-8 encoding before running Python to avoid cp1252 crashes on Windows

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

function Import-DotEnvFile {
    param(
        [string]$Path
    )

    if (-not (Test-Path $Path)) {
        return
    }

    foreach ($line in Get-Content $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }

        $separatorIndex = $trimmed.IndexOf("=")
        if ($separatorIndex -lt 1) {
            continue
        }

        $name = $trimmed.Substring(0, $separatorIndex).Trim()
        $value = $trimmed.Substring($separatorIndex + 1).Trim()
        # Match python-dotenv: an explicitly supplied process setting wins.
        if ($null -eq [Environment]::GetEnvironmentVariable($name, "Process")) {
            [Environment]::SetEnvironmentVariable($name, $value.Trim('"').Trim("'"), "Process")
        }
    }
}

function Test-PythonExe {
    param(
        [string]$Candidate
    )

    if (-not $Candidate -or -not (Test-Path -LiteralPath $Candidate)) {
        return $false
    }

    & $Candidate -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" *> $null
    return $LASTEXITCODE -eq 0
}

function Resolve-PythonExe {
    if ($env:VIRTUAL_ENV) {
        $venvPython = Join-Path $env:VIRTUAL_ENV "Scripts\python.exe"
        if (Test-PythonExe $venvPython) {
            return $venvPython
        }
    }

    $projectVenvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
    if (Test-PythonExe $projectVenvPython) {
        return $projectVenvPython
    }

    try {
        $pythonCmd = Get-Command python -ErrorAction Stop
        if (
            $pythonCmd.Source -and
            $pythonCmd.Source -notlike "*WindowsApps\python.exe" -and
            (Test-PythonExe $pythonCmd.Source)
        ) {
            return $pythonCmd.Source
        }
    } catch {
    }

    $pythonCore = Get-ChildItem `
        -Path (Join-Path $env:LOCALAPPDATA "Python\pythoncore-*\python.exe") `
        -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending |
        Select-Object -First 1
    if ($pythonCore -and (Test-PythonExe $pythonCore.FullName)) {
        return $pythonCore.FullName
    }

    $localBinPython = Join-Path $env:LOCALAPPDATA "Python\bin\python.exe"
    if (Test-PythonExe $localBinPython) {
        return $localBinPython
    }

    return "python"
}

$pythonExe = Resolve-PythonExe
Write-Host "Using Python: $pythonExe" -ForegroundColor DarkGray

function Invoke-Python {
    param(
        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$Args
    )

    if ($Args.Count -gt 0 -and $Args[0].EndsWith(".py") -and -not [IO.Path]::IsPathRooted($Args[0])) {
        $Args[0] = Join-Path $PSScriptRoot $Args[0]
    }
    & $pythonExe @Args
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

function Start-LightRAGUi {
    Import-DotEnvFile (Join-Path $PSScriptRoot ".env")

    $env:LLM_BINDING = "openai"
    $env:EMBEDDING_BINDING = "openai"
    $env:LLM_BINDING_HOST = $env:LLM_BASE_URL
    $env:LLM_BINDING_API_KEY = $env:LLM_API_KEY
    $env:EMBEDDING_BINDING_HOST = $env:EMBEDDING_BASE_URL
    $env:EMBEDDING_BINDING_API_KEY = $env:EMBEDDING_API_KEY
    $runtimePaths = & $pythonExe -c "import json; from geospoiler import config; print(json.dumps({'storage': str(config.RAG_STORAGE_DIR), 'input': str(config.ENRICHED_DIR)}))"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $resolvedPaths = $runtimePaths | ConvertFrom-Json
    $env:WORKING_DIR = $resolvedPaths.storage
    $env:INPUT_DIR = $resolvedPaths.input
    $env:HOST = "127.0.0.1"
    $env:PORT = "9621"

    if (-not $env:LLM_BINDING_API_KEY -or -not $env:EMBEDDING_BINDING_API_KEY) {
        Write-Host "LightRAG UI needs LLM and embedding credentials in .env." -ForegroundColor Red
        Write-Host "Expected vars: LLM_API_KEY, LLM_BASE_URL, EMBEDDING_API_KEY, EMBEDDING_BASE_URL." -ForegroundColor Yellow
        exit 1
    }

    Write-Host "=== LightRAG Web UI ===" -ForegroundColor Cyan
    Write-Host "Open: http://127.0.0.1:9621/" -ForegroundColor Green
    Write-Host "Docs: http://127.0.0.1:9621/docs" -ForegroundColor DarkGray
    Invoke-Python -m lightrag.api.lightrag_server
}

$step = $args[0]

switch ($step) {
    "auth" {
        Write-Host "=== Step 0: Telegram Authorization ===" -ForegroundColor Cyan
        Invoke-Python auth.py
    }
    "fetch" {
        $limit = if ($args[1]) { $args[1] } else { "" }
        Write-Host "=== Step 1: Fetching from Telegram ===" -ForegroundColor Cyan
        if ($limit) {
            Invoke-Python main.py fetch $limit
        } else {
            Invoke-Python main.py fetch
        }
    }
    "normalize" {
        $limit = if ($args[1]) { $args[1] } else { "" }
        Write-Host "=== Step 2: Fetch + Normalize (no LightRAG load) ===" -ForegroundColor Cyan
        if ($limit) {
            Invoke-Python main.py normalize $limit
        } else {
            Invoke-Python main.py normalize
        }
    }
    "load" {
        Write-Host "=== Step 3: Loading into LightRAG ===" -ForegroundColor Cyan
        $extraArgs = @($args | Select-Object -Skip 1)
        Invoke-Python main.py load @extraArgs
    }
    "enrich" {
        $extraArgs = @($args | Select-Object -Skip 1)
        Invoke-Python main.py enrich @extraArgs
    }
    "rebuild" {
        Write-Host "=== Rebuilding LightRAG storage from enriched graph text ===" -ForegroundColor Yellow
        $extraArgs = @($args | Select-Object -Skip 1)
        Invoke-Python main.py rebuild @extraArgs
    }
    "run" {
        $limit = if ($args[1]) { $args[1] } else { "" }
        Write-Host "=== Pipeline: fetch + normalize + enrich + local indexes ===" -ForegroundColor Cyan
        if ($limit) {
            Invoke-Python main.py run $limit
        } else {
            Invoke-Python main.py run
        }
    }
    "query" {
        $queryArgs = @($args | Select-Object -Skip 1)
        $knownModes = @("local", "global", "hybrid", "naive", "mix", "bypass")
        $mode = "mix"
        if ($queryArgs.Count -gt 0 -and $knownModes -contains $queryArgs[-1].ToLower()) {
            $mode = $queryArgs[-1].ToLower()
            if ($queryArgs.Count -gt 1) {
                $question = ($queryArgs[0..($queryArgs.Count - 2)] -join " ").Trim()
            } else {
                $question = ""
            }
        } else {
            $question = ($queryArgs -join " ").Trim()
        }
        if (-not $question) {
            Write-Host "Usage: .\run_pipeline.ps1 query `"Your question`" [mode]"
            exit 1
        }
        Invoke-Python main.py query $question $mode
    }
    "search" {
        $extraArgs = @($args | Select-Object -Skip 1)
        Invoke-Python main.py search @extraArgs
    }
    "quality" {
        Invoke-Python main.py quality
    }
    "golden" {
        Invoke-Python test_golden_set.py
    }
    "ui" {
        Start-LightRAGUi
    }
    "review" {
        Invoke-Python main.py review --web
    }
    "status" {
        Invoke-Python main.py status
    }
    default {
        Write-Host "Usage: .\run_pipeline.ps1 [auth|fetch|normalize|enrich|load|rebuild|run|query|search|quality|golden|ui|review|status]"
        Write-Host ""
        Write-Host "  auth           - First-time Telegram login (saves session)"
        Write-Host "  fetch [N]      - Fetch last N messages per channel (default: all new)"
        Write-Host "  normalize [N]  - Fetch + normalize only (no LightRAG load)"
        Write-Host "  enrich [flags] - Build enriched cards and local indexes"
        Write-Host "  load [flags]   - Load enriched graph text into LightRAG"
        Write-Host "  rebuild [flags]- Backup LightRAG storage and rebuild from enriched graph text"
        Write-Host "  run [N]        - Fetch + normalize + enrich + local indexes"
        Write-Host "  query `"?`" [m]  - Query the knowledge graph (default: mix; modes: hybrid/mix/local/global)"
        Write-Host "  search [args]  - Search the active local retrieval layers"
        Write-Host "  quality        - Show graph quality diagnostics"
        Write-Host "  golden         - Run golden set verification"
        Write-Host "  ui             - Start the LightRAG Web UI on http://127.0.0.1:9621/"
        Write-Host "  review         - Show pending AI chat review items"
        Write-Host "  status         - Show pipeline status"
    }
}
