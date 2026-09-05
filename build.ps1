# build.ps1 —— 一键构建 Wscrcpy.exe（Windows）
# 用法: .\build.ps1 [-HdcBin <hdc.exe路径>] [-FfmpegBin <ffmpeg.exe路径>]
# hdc.exe 从 DevEco Command Line Tools for Windows 取；ffmpeg.exe 用静态构建
# （gyan.dev essentials 或 BtbN）。目标机无需任何环境。
param(
    [string]$HdcBin = "",
    [string]$FfmpegBin = ""
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "== [1/4] 备齐 vendor 资源 =="
New-Item -ItemType Directory -Force -Path vendor\bin, vendor\data | Out-Null

# --- hdc.exe ---
if (-not $HdcBin) {
    $candidates = @(
        "$env:LOCALAPPDATA\HUAWEI\Sdk\hmscore",
        "$env:USERPROFILE\AppData\Local\HUAWEI\Sdk\hmscore"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) {
            $hit = Get-ChildItem -Path $c -Recurse -Filter hdc.exe -ErrorAction SilentlyContinue |
                   Select-Object -First 1
            if ($hit) { $HdcBin = $hit.FullName; break }
        }
    }
}
if (-not $HdcBin -or -not (Test-Path $HdcBin)) {
    # CI / 绿色构建路径：直接用随仓库提交的 hdc.exe
    if (Test-Path "vendor\bin\hdc.exe") { $HdcBin = "vendor\bin\hdc.exe" }
}
if (-not $HdcBin -or -not (Test-Path $HdcBin)) {
    Write-Error "未找到 hdc.exe。请用 -HdcBin 指定，或将 hdc.exe 放到 vendor\bin\hdc.exe"
}
# 同文件不拷（-HdcBin vendor\bin\hdc.exe 时 Copy-Item 自拷会报错终止脚本）
$hdcSrcPath = (Resolve-Path $HdcBin).Path
$hdcDstPath = Join-Path (Resolve-Path "vendor\bin").Path "hdc.exe"
if ($hdcSrcPath -ne $hdcDstPath) {
    Copy-Item -Force $HdcBin $hdcDstPath
}
# hdc.exe 如带伴随 DLL 一并复制（Windows DLL 搜索含 exe 所在目录）
$tcDir = (Resolve-Path (Split-Path $HdcBin -Parent)).Path
$vendorBin = (Resolve-Path "vendor\bin").Path
if ($tcDir -ne $vendorBin) {
    Get-ChildItem -Path $tcDir -Filter *.dll -ErrorAction SilentlyContinue |
        ForEach-Object { Copy-Item -Force $_.FullName vendor\bin\ }
}

# --- ffmpeg.exe（静态 GPL 构建，含 libx264；无 DLL 依赖） ---
if (-not $FfmpegBin -or -not (Test-Path $FfmpegBin)) {
    # 主源 BtbN GitHub Releases（云机访问稳定）；备源 gyan.dev
    $sources = @(
        @{ Url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip" },
        @{ Url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" }
    )
    foreach ($s in $sources) {
        try {
            $zip = "$env:TEMP\ffmpeg-static.zip"
            $ext = "$env:TEMP\ffmpeg-static-ext"
            Write-Host "下载 ffmpeg: $($s.Url)"
            Invoke-WebRequest -Uri $s.Url -OutFile $zip -UserAgent "Mozilla/5.0" -TimeoutSec 900
            Expand-Archive -Force -Path $zip -DestinationPath $ext
            $hit = Get-ChildItem $ext -Recurse -Filter ffmpeg.exe | Select-Object -First 1
            if (-not $hit) { throw "包内未找到 ffmpeg.exe" }
            $FfmpegBin = $hit.FullName
            break
        } catch {
            Write-Host "源失败: $($_.Exception.Message) —— 尝试下一个"
        }
    }
}
if (-not $FfmpegBin -or -not (Test-Path $FfmpegBin)) {
    Write-Error "ffmpeg.exe 获取失败。请手动下载后 -FfmpegBin 指定"
}
Copy-Item -Force $FfmpegBin vendor\bin\ffmpeg.exe

Copy-Item -Force scripts\caploop.sh vendor\data\caploop.sh

Write-Host "== [2/4] PyInstaller 构建 =="
python -m PyInstaller wscrcpy.spec --noconfirm 2>&1 | Tee-Object -FilePath pyinstaller-log.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "PyInstaller 失败，日志尾部："
    Get-Content pyinstaller-log.txt -Tail 40
    Write-Error "PyInstaller 构建失败（详见 pyinstaller-log.txt）"
}

Write-Host "== [3/4] 整理产物 =="
if (Test-Path dist\Wscrcpy\Wscrcpy.exe) {
    Write-Host "完成: dist\Wscrcpy\Wscrcpy.exe（分发整个 dist\Wscrcpy\ 目录即为绿色包）"
} else {
    Write-Error "构建产物未生成，请检查上方 PyInstaller 日志"
}
Write-Host "== [4/4] 冒烟建议 =="
Write-Host "dist\Wscrcpy\Wscrcpy.exe --probe   # 先跑验证清单（需连接设备）"
