$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

python -m pip install --upgrade pip
python -m pip install -e ".[gui,build]"

python -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --name "Warhammer-Archive" `
    --paths "src" `
    --collect-all "pymupdf" `
    "packaging/windows_entry.py"

Copy-Item "README.md" "dist/Warhammer-Archive/README.md" -Force

$ArchivePath = "dist/Warhammer-Archive-Windows-x64.zip"
if (Test-Path $ArchivePath) {
    Remove-Item $ArchivePath -Force
}

Compress-Archive `
    -Path "dist/Warhammer-Archive/*" `
    -DestinationPath $ArchivePath `
    -CompressionLevel Optimal

Write-Host "Windows package created: $ArchivePath"

