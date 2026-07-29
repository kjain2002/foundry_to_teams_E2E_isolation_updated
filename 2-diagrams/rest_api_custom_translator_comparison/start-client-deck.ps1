param(
    [int] $Port = 8765
)

$ErrorActionPreference = 'Stop'
$diagramDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "Starting the client deck in PowerShell at http://localhost:$Port/publishing_approaches_client_deck.html"
python -m http.server $Port --directory $diagramDirectory