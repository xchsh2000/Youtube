$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

Write-Host ""
Write-Host "这个上传方式会使用 GitHub 官方 API，不走本机 Git HTTPS/SSH 推送组件。"
Write-Host "请粘贴 GitHub token。输入时不会显示。"
Write-Host ""

$secureToken = Read-Host "GitHub token" -AsSecureString
$tokenPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)

try {
    $env:GITHUB_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPtr)
    python ".\github_api_upload.py"
}
finally {
    $env:GITHUB_TOKEN = ""
    if ($tokenPtr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPtr)
    }
}

Write-Host ""
Read-Host "按回车退出"
