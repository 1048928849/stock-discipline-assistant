param([switch]$FromClipboard)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$EnvFile = Join-Path $ProjectRoot ".env"
$ExampleFile = Join-Path $ProjectRoot ".env.example"
$BaseUrl = "https://generativelanguage.googleapis.com/v1beta/openai"
$Model = "gemini-3.6-flash"
$Bstr = [IntPtr]::Zero

Write-Host "Gemini API Key 只会写入本机 .env，不会显示在终端或写入数据库。"
if ($FromClipboard) {
    $ApiKey = (Get-Clipboard -Raw).Trim()
} else {
    $SecureKey = Read-Host "请粘贴 Google AI Studio API Key" -AsSecureString
    $Bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureKey)
    $ApiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Bstr)
}
try {
    if ([string]::IsNullOrWhiteSpace($ApiKey)) {
        throw "API Key 不能为空"
    }
    Write-Host "正在验证 Gemini 模型和鉴权……"
    $Headers = @{
        Authorization = "Bearer $ApiKey"
        "x-goog-api-client" = "stock-discipline-assistant-oai/0.1.0"
    }
    $null = Invoke-RestMethod -Method Get -Uri "$BaseUrl/models/$Model" -Headers $Headers -TimeoutSec 30

    $Lines = if (Test-Path -LiteralPath $EnvFile) {
        @(Get-Content -LiteralPath $EnvFile -Encoding UTF8)
    } else {
        @(Get-Content -LiteralPath $ExampleFile -Encoding UTF8)
    }
    function Set-EnvValue([string[]]$Current, [string]$Name, [string]$Value) {
        $Found = $false
        $Updated = @()
        for ($Index = 0; $Index -lt $Current.Count; $Index++) {
            if ($Current[$Index] -match "^$([regex]::Escape($Name))=") {
                $Updated += "$Name=$Value"
                $Found = $true
            } else {
                $Updated += $Current[$Index]
            }
        }
        if (-not $Found) { $Updated += "$Name=$Value" }
        return $Updated
    }
    $Lines = @(Set-EnvValue $Lines "LLM_PROVIDER" "gemini")
    $Lines = @(Set-EnvValue $Lines "LLM_BASE_URL" $BaseUrl)
    $Lines = @(Set-EnvValue $Lines "LLM_MODEL" $Model)
    $Lines = @(Set-EnvValue $Lines "LLM_ENABLED" "true")
    $Lines = @(Set-EnvValue $Lines "LLM_API_KEY" $ApiKey)
    [IO.File]::WriteAllLines($EnvFile, $Lines, [Text.UTF8Encoding]::new($false))
    Write-Host "配置成功。请重启项目，再点击设置页的“测试大模型连接”。"
} finally {
    if ($Bstr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Bstr)
    }
    $ApiKey = $null
    if ($FromClipboard) { Set-Clipboard -Value " " }
}



