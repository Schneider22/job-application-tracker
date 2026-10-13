param(
  [Parameter(Mandatory = $true)]
  [string]$Message
)

$ErrorActionPreference = "Stop"
$pythonCommand = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }
& $pythonCommand -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw "Tests failed; change was not committed or pushed." }

git add --all
if ($LASTEXITCODE -ne 0) { throw "Could not stage the change." }

& $pythonCommand scripts/security_gate.py
if ($LASTEXITCODE -ne 0) { throw "Security gate failed; change was not pushed." }

git diff --cached --check
if ($LASTEXITCODE -ne 0) { throw "Whitespace validation failed; change was not pushed." }

$staged = git diff --cached --name-only
if (-not $staged) { Write-Host "No changes to publish."; exit 0 }

git commit -m $Message
if ($LASTEXITCODE -ne 0) { throw "Commit failed." }
git push origin main
if ($LASTEXITCODE -ne 0) { throw "Push failed. The local commit is preserved for retry." }
