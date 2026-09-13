# Windows HTTPS fallback for the exact Turbo revision used by faster-whisper.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$podcastRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$modelDirectory = Join-Path $podcastRoot '99_系统文件/依赖/播客模型/whisper-large-v3-turbo'
$modelRevision = '0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf'
$modelRepository = 'mobiuslabsgmbh/faster-whisper-large-v3-turbo'
New-Item -ItemType Directory -Force -Path $modelDirectory | Out-Null
$modelInfo = Invoke-RestMethod -Uri "https://huggingface.co/api/models/$modelRepository/revision/${modelRevision}?blobs=true" -SslProtocol Tls12 -TimeoutSec 60 -MaximumRetryCount 3
if ($modelInfo.sha -ne $modelRevision) { throw 'Model revision does not match' }
$modelFiles = @{}
foreach ($modelName in @('config.json','model.bin','preprocessor_config.json','tokenizer.json','vocabulary.json')) {
    $modelTarget = Join-Path $modelDirectory $modelName
    $expected = $modelInfo.siblings | Where-Object rfilename -eq $modelName
    if (-not $expected) { throw "Missing source metadata: $modelName" }
    $needsDownload = -not (Test-Path -LiteralPath $modelTarget)
    if (-not $needsDownload) { $needsDownload = (Get-Item -LiteralPath $modelTarget).Length -ne $expected.size }
    if ($needsDownload) {
        Write-Output "Downloading $modelName ($($expected.size) bytes)"
        $modelPartial = $modelTarget + '.part'
        Invoke-WebRequest -Uri "https://huggingface.co/$modelRepository/resolve/$modelRevision/$modelName" -OutFile $modelPartial -Resume -SslProtocol Tls12 -TimeoutSec 1800 -MaximumRetryCount 3 -RetryIntervalSec 2
        if ((Get-Item -LiteralPath $modelPartial).Length -ne $expected.size) { throw "Incomplete download: $modelName" }
        if ($expected.lfs -and (Get-FileHash -LiteralPath $modelPartial -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected.lfs.sha256) { throw "Checksum mismatch: $modelName" }
        Move-Item -LiteralPath $modelPartial -Destination $modelTarget -Force
    }
    $digest = (Get-FileHash -LiteralPath $modelTarget -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($expected.lfs -and $digest -ne $expected.lfs.sha256) { throw "Checksum mismatch: $modelName" }
    if (-not $expected.lfs) {
        $fileBytes = [IO.File]::ReadAllBytes($modelTarget)
        $prefixBytes = [Text.Encoding]::UTF8.GetBytes("blob $($fileBytes.Length)`0")
        $blobBytes = [byte[]]::new($prefixBytes.Length + $fileBytes.Length)
        [Array]::Copy($prefixBytes, 0, $blobBytes, 0, $prefixBytes.Length)
        [Array]::Copy($fileBytes, 0, $blobBytes, $prefixBytes.Length, $fileBytes.Length)
        $blobDigest = [Convert]::ToHexString([Security.Cryptography.SHA1]::HashData($blobBytes)).ToLowerInvariant()
        if ($blobDigest -ne $expected.blobId) { throw "Git blob checksum mismatch: $modelName" }
    }
    $modelFiles[$modelName] = @{bytes=$expected.size;sha256=$digest}
    Write-Output "Verified $modelName"
}
$modelReceipt = @{repository=$modelRepository;resolved_repository=$modelInfo.id;revision=$modelRevision;files=$modelFiles}
$receiptTarget = Join-Path $modelDirectory 'download-receipt.json'
$modelReceipt | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath ($receiptTarget + '.part') -Encoding utf8
Move-Item -LiteralPath ($receiptTarget + '.part') -Destination $receiptTarget -Force
Write-Output 'Model download and verification complete.'
