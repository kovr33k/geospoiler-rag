@echo off
echo ==============================================
echo 1. Continuing Enrichment...
echo ==============================================
call "%~dp0run_pipeline.cmd" enrich
if errorlevel 1 exit /b %errorlevel%

echo ==============================================
echo 2. Rebuilding LightRAG Graph...
echo ==============================================
call "%~dp0run_pipeline.cmd" rebuild
if errorlevel 1 exit /b %errorlevel%

echo ==============================================
echo 3. Running Golden Set Verification...
echo ==============================================
call "%~dp0run_pipeline.cmd" golden
if errorlevel 1 exit /b %errorlevel%

echo ==============================================
echo ALL DONE! Check artifacts/golden_set_results.md
echo ==============================================
