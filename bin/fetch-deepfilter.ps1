# Fetch DeepFilterNet's deep-filter CLI and the DeepFilterNet3 model into bin/ on Windows
# (optional: enables the denoised playback copies). The Windows counterpart of fetch-deepfilter.sh:
#   powershell -ExecutionPolicy Bypass -File bin\fetch-deepfilter.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$Ver = "0.5.6"
$Asset = "deep-filter-$Ver-x86_64-pc-windows-msvc.exe"
$ModelSha = "c94d91f70911001c946e0fabb4aa9adc37045f45a03b56008cb0c8244cb63616"

Invoke-WebRequest -UseBasicParsing -OutFile deep-filter.exe "https://github.com/Rikorose/DeepFilterNet/releases/download/v$Ver/$Asset"
# No pinned checksum for the Windows build yet: print it, to pin here once checked.
Write-Host "deep-filter.exe SHA-256: $((Get-FileHash deep-filter.exe -Algorithm SHA256).Hash.ToLower())"

Invoke-WebRequest -UseBasicParsing -OutFile DeepFilterNet3_onnx.tar.gz "https://raw.githubusercontent.com/Rikorose/DeepFilterNet/main/models/DeepFilterNet3_onnx.tar.gz"
$got = (Get-FileHash DeepFilterNet3_onnx.tar.gz -Algorithm SHA256).Hash.ToLower()
if ($got -ne $ModelSha) {
    Remove-Item DeepFilterNet3_onnx.tar.gz
    throw "DeepFilterNet3_onnx.tar.gz checksum mismatch: $got"
}
Write-Host "deep-filter.exe and DeepFilterNet3 model ready in bin\"
