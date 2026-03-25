# Wrapper called by the scheduled task. Runs the scraper and appends output to a log file.

$ScriptDir = "C:\myprojects\UpperValleyEventScraper"
$LogFile   = "$ScriptDir\output\scraper.log"

Set-Location $ScriptDir
$env:PYTHONUTF8 = "1"

"" | Add-Content $LogFile -Encoding UTF8
"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Add-Content $LogFile -Encoding UTF8
python scraper.py --sources=all 2>&1 | Add-Content $LogFile -Encoding UTF8
