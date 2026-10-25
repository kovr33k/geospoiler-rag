$ErrorActionPreference = "Stop"

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

$resultsPath = Join-Path $PSScriptRoot "test_results.txt"

"=== ТЕСТ 1 ===" | Set-Content -Path $resultsPath -Encoding UTF8
.\run_pipeline.ps1 query "Откуда в базе тезис про ультралевых и ультраправых? Дай ссылку." |
    Tee-Object -FilePath $resultsPath -Append

"`n=== ТЕСТ 2 ===" | Add-Content -Path $resultsPath -Encoding UTF8
.\run_pipeline.ps1 query "Что в базе говорится о сходстве ультралевых и ультраправых?" |
    Tee-Object -FilePath $resultsPath -Append

"`n=== ТЕСТ 3 ===" | Add-Content -Path $resultsPath -Encoding UTF8
.\run_pipeline.ps1 query "Что в базе говорится про связи европейских ультраправых с Трампом?" |
    Tee-Object -FilePath $resultsPath -Append

"`n=== ТЕСТ 4 ===" | Add-Content -Path $resultsPath -Encoding UTF8
.\run_pipeline.ps1 query "Что в базе говорится о Кубе и переговорах с США?" |
    Tee-Object -FilePath $resultsPath -Append
