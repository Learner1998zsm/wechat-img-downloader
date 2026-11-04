# 一键打包成单文件 exe
# 用法：在 PowerShell 中执行 .\build_exe.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "未找到 python，请先安装 Python 3.9+" -ForegroundColor Red
    exit 1
}

python -m pip install --upgrade pyinstaller
python -m PyInstaller `
    --noconfirm --clean --onefile --windowed `
    --name "公众号原图下载器" `
    --icon "icon.ico" `
    --distpath ".\dist" `
    --workpath ".\build" `
    --specpath ".\build" `
    "wx_img_downloader.py"

Write-Host ""
Write-Host "打包完成：dist\公众号原图下载器.exe" -ForegroundColor Green
