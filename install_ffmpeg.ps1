# FFmpeg Installation Script for Windows
# Downloads and extracts FFmpeg to C:\ffmpeg

param(
    [string]$InstallPath = "C:\ffmpeg"
)

Write-Host "Installing FFmpeg to $InstallPath..." -ForegroundColor Green

# Create installation directory
if (-not (Test-Path $InstallPath)) {
    New-Item -ItemType Directory -Path $InstallPath -Force | Out-Null
    Write-Host "Created directory: $InstallPath" -ForegroundColor Green
}

# Download FFmpeg
$downloadUrl = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
$zipPath = "$env:TEMP\ffmpeg.zip"

Write-Host "Downloading FFmpeg from $downloadUrl..." -ForegroundColor Yellow
try {
    Invoke-WebRequest -Uri $downloadUrl -OutFile $zipPath -UseBasicParsing
    Write-Host "Downloaded successfully" -ForegroundColor Green
} catch {
    Write-Host "Failed to download FFmpeg: $_" -ForegroundColor Red
    exit 1
}

# Extract FFmpeg
Write-Host "Extracting FFmpeg..." -ForegroundColor Yellow
try {
    Expand-Archive -Path $zipPath -DestinationPath $env:TEMP -Force
    Write-Host "Extracted successfully" -ForegroundColor Green
} catch {
    Write-Host "Failed to extract FFmpeg: $_" -ForegroundColor Red
    exit 1
}

# Find the extracted folder and copy bin to target
$extractedPath = Get-ChildItem -Path $env:TEMP -Filter "ffmpeg-*" -Directory | Select-Object -First 1
if ($extractedPath) {
    $binPath = Join-Path $extractedPath.FullName "bin"
    if (Test-Path $binPath) {
        Copy-Item -Path "$binPath\*" -Destination $InstallPath -Force -Recurse
        Write-Host "Copied FFmpeg binaries to $InstallPath" -ForegroundColor Green
    }
}

# Verify installation
$ffmpegExe = Join-Path $InstallPath "ffmpeg.exe"
if (Test-Path $ffmpegExe) {
    Write-Host "FFmpeg installed successfully!" -ForegroundColor Green
    & $ffmpegExe -version | Select-Object -First 3
} else {
    Write-Host "FFmpeg binary not found at expected location" -ForegroundColor Red
    exit 1
}

# Clean up
Remove-Item $zipPath -Force -ErrorAction SilentlyContinue

Write-Host "`nUpdate your .env file with:" -ForegroundColor Yellow
Write-Host "FFMPEG_PATH=$ffmpegExe" -ForegroundColor Cyan
