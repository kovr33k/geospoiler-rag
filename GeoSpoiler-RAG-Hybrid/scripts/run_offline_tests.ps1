param(
    [string]$RunRoot = "",
    [string]$Python = ""
)

$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $Python) {
    $Python = Join-Path $projectRoot $(if ($IsWindows) { ".venv/Scripts/python.exe" } else { ".venv/bin/python" })
    if (-not (Test-Path -LiteralPath $Python)) {
        throw "Create .venv and install development dependencies, or pass -Python, before running offline tests."
    }
}

if (-not $RunRoot) {
    $RunRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("geospoiler-tests-" + [guid]::NewGuid().ToString("N"))
}
$RunRoot = [System.IO.Path]::GetFullPath($RunRoot)
New-Item -ItemType Directory -Force -Path $RunRoot | Out-Null

$env:GEOSPOILER_NO_NETWORK = "1"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:WIKI_ENABLED = "false"
$env:HYBRID_QUERY_WIKI_ENABLED = "false"
$env:PYTHONPATH = "$projectRoot$([IO.Path]::PathSeparator)$(Join-Path $projectRoot 'src')$([IO.Path]::PathSeparator)$env:PYTHONPATH"
if (-not $env:TIKTOKEN_CACHE_DIR) {
    $cacheRoot = if ($IsWindows) { $env:LOCALAPPDATA } else { [IO.Path]::GetTempPath() }
    $env:TIKTOKEN_CACHE_DIR = Join-Path $cacheRoot "GeoSpoiler/tiktoken"
}

$paths = @{
    OUTPUT_DIR = "data"
    YOUTUBE_NORMALIZED_DIR = "data\normalized_youtube"
    YOUTUBE_SEGMENTS_DIR = "data\enriched_segments"
    RAG_STORAGE_DIR = "data\indexes\lightrag"
    CARD_FTS_DB_PATH = "data\indexes\card_fts.sqlite"
    SOURCE_REGISTRY_DB_PATH = "data\indexes\source_registry.sqlite"
    WIKI_STATE_DB_PATH = "data\wiki\state.sqlite"
    WIKI_OUTPUT_DIR = "data\wiki\projections"
    WIKI_SIDECAR_DIR = "data\wiki\sidecars"
    STATE_DIR = "data\state"
    CODEX_RUNTIME_DIR = "data\state\codex_runtime"
    MEDIA_CACHE_DIR = "data\cache\media"
    INSTAGRAM_CACHE_DIR = "data\cache\instagram"
    LOG_DIR = "logs"
}
foreach ($entry in $paths.GetEnumerator()) {
    [Environment]::SetEnvironmentVariable($entry.Key, (Join-Path $RunRoot $entry.Value), "Process")
}

Write-Host "Offline test artifacts: $RunRoot"
& $Python -m pytest (Join-Path $projectRoot "tests") -q -rs --basetemp (Join-Path $RunRoot "pytest")
exit $LASTEXITCODE
