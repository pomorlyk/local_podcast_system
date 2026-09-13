param([Parameter(Mandatory=$true)][string]$RequestPath)
$ErrorActionPreference = 'Stop'
$requestData = Get-Content -LiteralPath $RequestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $requestData.url.StartsWith('https://')) { throw 'HTTPS is required' }
$extraHeaders = @{}
if ($requestData.audio) { $extraHeaders['Range'] = 'bytes=0-' }
$response = Invoke-WebRequest -Uri $requestData.url -Headers $extraHeaders -OutFile $requestData.output -PassThru -SslProtocol Tls12 -TimeoutSec 180 -MaximumRedirection 8
$result = @{
    status = [int]$response.StatusCode
    url = $response.BaseResponse.RequestMessage.RequestUri.AbsoluteUri
    headers = @{}
}
foreach ($key in $response.Headers.Keys) { $result.headers[$key] = ($response.Headers[$key] -join ', ') }
$result | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $requestData.info -Encoding utf8
