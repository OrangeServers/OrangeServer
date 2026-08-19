[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[0-9a-fA-F]{40}$')]
    [string]$ExpectedHead,
    [switch]$Keep,
    [ValidateRange(60, 1800)]
    [int]$WaitTimeoutSeconds = 420
)

# M1/S3 切片 7 thin-layer smoke：复用 S2 的 exact-head archive/Compose 基础
# 设施（同一 archive 安全检查、同一 compose 服务与后端镜像），只运行 S3
# 单阶段探针 chat-draft-only，证明聊天侧只能创建自治草稿/引用卡。
# 不 fork S2 脚本；S2 完整执行链路由 ops/smoke-ai-autonomy-s2.ps1 负责。

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$TempRoot = $null
$SourceRoot = $null
$ComposeArguments = @()
$Succeeded = $false
$CleanupFailed = $false
$ComposeReady = $false
$BackendImageName = $null

function Invoke-Native {
    param(
        [Parameter(Mandatory)]
        [string]$FilePath,
        [Parameter(ValueFromRemainingArguments)]
        [string[]]$Arguments
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath failed with exit code $LASTEXITCODE"
    }
}

function New-UrlSafeSecret {
    param(
        [ValidateRange(16, 128)]
        [int]$ByteCount,
        [switch]$KeepPadding
    )
    $bytes = [byte[]]::new($ByteCount)
    [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $encoded = [Convert]::ToBase64String($bytes).Replace('+', '-').Replace('/', '_')
    if (-not $KeepPadding) {
        $encoded = $encoded.TrimEnd('=')
    }
    return $encoded
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw 'git is required for the exact-checkout S3 smoke'
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Docker CLI with Compose v2 is required for the S3 smoke'
}
if (-not (Get-Command tar -ErrorAction SilentlyContinue)) {
    throw 'tar is required to extract the exact Git archive for the S3 smoke'
}

$Head = (& git -C $RepoRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $Head -notmatch '^[0-9a-f]{40}$') {
    throw 'cannot resolve the reviewed Git HEAD'
}
if ($Head -ne $ExpectedHead.ToLowerInvariant()) {
    throw "HEAD $Head does not match -ExpectedHead $ExpectedHead"
}

# An exact-head smoke must never consume staged, unstaged, or untracked input.
& git -C $RepoRoot diff --quiet
if ($LASTEXITCODE -ne 0) {
    throw 'working tree has unstaged changes; exact-head smoke refused'
}
& git -C $RepoRoot diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    throw 'index has staged changes; exact-head smoke refused'
}
$Porcelain = @(& git -C $RepoRoot status --porcelain=v1 --untracked-files=all)
if ($LASTEXITCODE -ne 0) {
    throw 'cannot inspect repository cleanliness'
}
if ($Porcelain.Count -ne 0) {
    throw 'working tree contains untracked or modified files; exact-head smoke refused'
}

$Suffix = '{0}-{1}' -f $Head.Substring(0, 10), ([Guid]::NewGuid().ToString('N').Substring(0, 8))
$ProjectName = ('ogs-s3-smoke-' + $Suffix).ToLowerInvariant()
$BackendImageName = 'orangeserver-s3-smoke:' + $Suffix
$SshImageName = 'orangeserver-s3-ssh:' + $Suffix
$SmokeTempParent = Join-Path $RepoRoot '.tmp/s3-smoke'
$TempRoot = Join-Path $SmokeTempParent $Suffix
$SourceRoot = Join-Path $TempRoot 'source'
$ArchivePath = Join-Path $TempRoot 'source.tar'
try {
    New-Item -ItemType Directory -Force -Path $TempRoot | Out-Null
    New-Item -ItemType Directory -Force -Path $SourceRoot | Out-Null

    # Same archive safety checks as the S2 wrapper: reject links and unsafe
    # paths before extracting the reviewed HEAD.
    $GitTreeEntries = @(& git -C $RepoRoot ls-tree -r $Head)
    if ($LASTEXITCODE -ne 0 -or $GitTreeEntries.Count -eq 0) {
        throw 'cannot inspect the exact-head Git tree'
    }
    foreach ($Entry in $GitTreeEntries) {
        if ($Entry.StartsWith('120000 ')) {
            throw "exact-head Git tree contains a forbidden symlink: $Entry"
        }
    }

    Invoke-Native git -C $RepoRoot archive --format=tar --output=$ArchivePath $Head
    $ArchiveEntries = @(& tar -tf $ArchivePath)
    if ($LASTEXITCODE -ne 0 -or $ArchiveEntries.Count -eq 0) {
        throw 'cannot inspect the exact-head source archive'
    }
    foreach ($Entry in $ArchiveEntries) {
        if (
            [string]::IsNullOrWhiteSpace($Entry) -or
            $Entry.StartsWith('/') -or
            $Entry -match '^[A-Za-z]:' -or
            $Entry -match '(^|/)\.\.(/|$)' -or
            $Entry.Contains('\')
        ) {
            throw "unsafe path in Git archive: $Entry"
        }
    }
    $ArchiveDetails = @(& tar -tvf $ArchivePath)
    if ($LASTEXITCODE -ne 0 -or $ArchiveDetails.Count -eq 0) {
        throw 'cannot inspect exact-head tar entry types'
    }
    foreach ($RawEntry in $ArchiveDetails) {
        $Entry = $RawEntry.TrimStart()
        if ($Entry.StartsWith('l') -or $Entry.StartsWith('h')) {
            throw "exact-head archive contains a forbidden link: $RawEntry"
        }
    }
    Invoke-Native tar -xf $ArchivePath -C $SourceRoot
    foreach ($RequiredPath in @(
        'AGENTS.md',
        'backend/Dockerfile',
        'backend/mysqldir/orange.sql',
        'deploy/docker-compose.s2-smoke.yml',
        'deploy/docker-compose.s3-smoke.yml',
        'ops/smoke-ai-autonomy-s3.py'
    )) {
        if (-not (Test-Path -LiteralPath (Join-Path $SourceRoot $RequiredPath) -PathType Leaf)) {
            throw "exact-head archive is missing $RequiredPath"
        }
    }
    $ArchiveSha256 = (Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $S2ComposeFile = Join-Path $SourceRoot 'deploy/docker-compose.s2-smoke.yml'
    $S3ComposeFile = Join-Path $SourceRoot 'deploy/docker-compose.s3-smoke.yml'

    $env:COMPOSE_PROJECT_NAME = $ProjectName
    $env:OGS_S2_SMOKE_GIT_HEAD = $Head
    $env:OGS_S2_SMOKE_SOURCE_ROOT = $SourceRoot
    $env:OGS_S2_SMOKE_BACKEND_IMAGE = $BackendImageName
    $env:OGS_S2_SMOKE_SSH_IMAGE = $SshImageName
    $env:OGS_S2_SMOKE_MYSQL_ROOT_PASSWORD = New-UrlSafeSecret -ByteCount 30
    $env:OGS_S2_SMOKE_MYSQL_PASSWORD = New-UrlSafeSecret -ByteCount 30
    $env:OGS_S2_SMOKE_BUSINESS_REDIS_PASSWORD = New-UrlSafeSecret -ByteCount 30
    $env:OGS_S2_SMOKE_AUTONOMY_REDIS_PASSWORD = New-UrlSafeSecret -ByteCount 30
    $env:OGS_S2_SMOKE_FLASK_SECRET = New-UrlSafeSecret -ByteCount 48
    $env:OGS_S2_SMOKE_FERNET_KEY = New-UrlSafeSecret -ByteCount 32 -KeepPadding

    $ComposeArguments = @('compose', '-f', $S2ComposeFile, '-f', $S3ComposeFile)
    $ComposeReady = $true
    Write-Host "[S3 smoke] HEAD=$Head archive_sha256=$ArchiveSha256 project=$ProjectName"
    Invoke-Native docker @ComposeArguments config --quiet
    Invoke-Native docker @ComposeArguments build smoke-runner

    # The chat-draft probe only needs fresh MySQL and business Redis; S2's
    # SSH target / upgrade DB / autonomy worker are not part of this gate.
    Invoke-Native docker @ComposeArguments up -d --wait --wait-timeout $WaitTimeoutSeconds `
        mysql-fresh business-redis
    Invoke-Native docker @ComposeArguments run --rm --no-deps smoke-runner chat-draft-only

    $Succeeded = $true
}
finally {
    if ($Keep) {
        Write-Warning "S3 smoke resources kept for inspection: project=$ProjectName"
        Write-Warning 'Cleanup with the same COMPOSE_PROJECT_NAME and archived Compose files shown above.'
    }
    else {
        if ($ComposeReady) {
            & docker @ComposeArguments down --volumes --remove-orphans --timeout 10
            if ($LASTEXITCODE -ne 0) {
                $CleanupFailed = $true
                Write-Warning "automatic Compose cleanup failed for project $ProjectName"
            }
        }
        foreach ($ImageName in @($BackendImageName, $SshImageName)) {
            $ExistingImageIds = @(& docker image ls --quiet --no-trunc $ImageName)
            if ($LASTEXITCODE -ne 0) {
                $CleanupFailed = $true
                Write-Warning "cannot inspect disposable image during cleanup: $ImageName"
                continue
            }
            if (-not [string]::IsNullOrWhiteSpace(($ExistingImageIds -join ''))) {
                & docker image rm $ImageName | Out-Null
                if ($LASTEXITCODE -ne 0) {
                    $CleanupFailed = $true
                    Write-Warning "automatic image cleanup failed: $ImageName"
                }
            }
        }
        if (
            -not $CleanupFailed -and
            $null -ne $TempRoot -and
            (Test-Path -LiteralPath $TempRoot)
        ) {
            $ResolvedTemp = (Resolve-Path -LiteralPath $TempRoot).Path
            $ExpectedPrefix = [IO.Path]::GetFullPath($SmokeTempParent) + [IO.Path]::DirectorySeparatorChar
            if (-not $ResolvedTemp.StartsWith($ExpectedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
                $CleanupFailed = $true
                Write-Warning "refusing to remove unexpected smoke path: $ResolvedTemp"
            }
            if (-not $CleanupFailed) {
                try {
                    Remove-Item -LiteralPath $ResolvedTemp -Recurse -Force
                }
                catch {
                    $CleanupFailed = $true
                    Write-Warning "automatic source cleanup failed: $ResolvedTemp"
                }
            }
        }
    }
}

if (-not $Succeeded) {
    throw 'S3 smoke did not reach its acceptance gate'
}
if ($CleanupFailed) {
    throw "S3 smoke reached acceptance but cleanup failed; retry source retained at $TempRoot"
}

Write-Host (
    '[S3 smoke] DISPOSABLE_S3_PASS: chat created only an autonomy ' +
    'draft/reference card; no Run was started, approved, or mutated, ' +
    'non-admin chat was refused, and the detail projection restores the card'
)
