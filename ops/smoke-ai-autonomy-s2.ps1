[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[0-9a-fA-F]{40}$')]
    [string]$ExpectedHead,
    [switch]$Keep,
    [ValidateRange(60, 1800)]
    [int]$WaitTimeoutSeconds = 420
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ExpectedV104Commit = 'a4ef2c43efaea7b50cdc7f4fc6a7334a8966f0a8'
$TempRoot = $null
$SourceRoot = $null
$ComposeFile = $null
$Succeeded = $false
$BlockerName = $null

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
    throw 'git is required for the exact-checkout S2 smoke'
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Docker CLI with Compose v2 is required for the S2 smoke'
}
if (-not (Get-Command tar -ErrorAction SilentlyContinue)) {
    throw 'tar is required to extract the exact Git archive for the S2 smoke'
}

$Head = (& git -C $RepoRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $Head -notmatch '^[0-9a-f]{40}$') {
    throw 'cannot resolve the reviewed Git HEAD'
}
if ($Head -ne $ExpectedHead.ToLowerInvariant()) {
    throw "HEAD $Head does not match -ExpectedHead $ExpectedHead"
}

# An exact-head smoke must never consume staged, unstaged, or untracked input.
# Refuse the run rather than silently proving a dirty developer checkout.
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

$V104Commit = (& git -C $RepoRoot rev-list -n 1 v1.0.4).Trim()
if ($LASTEXITCODE -ne 0 -or $V104Commit -ne $ExpectedV104Commit) {
    throw "v1.0.4 must resolve to the pinned commit $ExpectedV104Commit"
}

$Suffix = '{0}-{1}' -f $Head.Substring(0, 10), ([Guid]::NewGuid().ToString('N').Substring(0, 8))
$ProjectName = ('ogs-s2-smoke-' + $Suffix).ToLowerInvariant()
$SmokeTempParent = Join-Path $RepoRoot '.tmp/s2-smoke'
$TempRoot = Join-Path $SmokeTempParent $Suffix
$SourceRoot = Join-Path $TempRoot 'source'
$ArchivePath = Join-Path $TempRoot 'source.tar'
$FixtureRoot = Join-Path $SourceRoot '.s2-smoke'
$V104Schema = Join-Path $FixtureRoot 'v1.0.4-orange.sql'
New-Item -ItemType Directory -Force -Path $TempRoot | Out-Null
New-Item -ItemType Directory -Force -Path $SourceRoot | Out-Null

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
Invoke-Native tar -xf $ArchivePath -C $SourceRoot
foreach ($RequiredPath in @(
    'AGENTS.md',
    'backend/Dockerfile',
    'backend/mysqldir/orange.sql',
    'deploy/docker-compose.s2-smoke.yml',
    'ops/smoke-ai-autonomy-s2.py'
)) {
    if (-not (Test-Path -LiteralPath (Join-Path $SourceRoot $RequiredPath) -PathType Leaf)) {
        throw "exact-head archive is missing $RequiredPath"
    }
}
$ArchiveSha256 = (Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash.ToLowerInvariant()
$ComposeFile = Join-Path $SourceRoot 'deploy/docker-compose.s2-smoke.yml'
New-Item -ItemType Directory -Force -Path $FixtureRoot | Out-Null

# `git show` is the only upgrade fixture source. This prevents a copied SQL
# file from silently drifting away from the immutable v1.0.4 release tag.
$schemaLines = & git -C $RepoRoot show 'v1.0.4:backend/mysqldir/orange.sql'
if ($LASTEXITCODE -ne 0) {
    throw 'cannot extract backend/mysqldir/orange.sql from v1.0.4'
}
$schemaLines | Set-Content -LiteralPath $V104Schema -Encoding utf8NoBOM

$env:COMPOSE_PROJECT_NAME = $ProjectName
$env:OGS_S2_SMOKE_GIT_HEAD = $Head
$env:OGS_S2_SMOKE_SOURCE_ROOT = $SourceRoot
$env:OGS_S2_SMOKE_BACKEND_IMAGE = 'orangeserver-s2-smoke:' + $Head.Substring(0, 12)
$env:OGS_S2_SMOKE_MYSQL_ROOT_PASSWORD = New-UrlSafeSecret -ByteCount 30
$env:OGS_S2_SMOKE_MYSQL_PASSWORD = New-UrlSafeSecret -ByteCount 30
$env:OGS_S2_SMOKE_BUSINESS_REDIS_PASSWORD = New-UrlSafeSecret -ByteCount 30
$env:OGS_S2_SMOKE_AUTONOMY_REDIS_PASSWORD = New-UrlSafeSecret -ByteCount 30
$env:OGS_S2_SMOKE_FLASK_SECRET = New-UrlSafeSecret -ByteCount 48
$env:OGS_S2_SMOKE_FERNET_KEY = New-UrlSafeSecret -ByteCount 32 -KeepPadding

$Compose = @('compose', '-f', $ComposeFile)

try {
    Write-Host "[S2 smoke] HEAD=$Head archive_sha256=$ArchiveSha256 project=$ProjectName"
    Invoke-Native docker @Compose config --quiet
    Invoke-Native docker @Compose build smoke-runner autonomy-worker
    Invoke-Native docker @Compose up -d --wait --wait-timeout $WaitTimeoutSeconds `
        mysql-fresh mysql-upgrade business-redis autonomy-redis

    Invoke-Native docker @Compose run --rm smoke-runner migrate-and-prime

    # Persistence is only evidence after a real process restart. The named
    # volume remains attached; the second probe must recover the DB 0 marker.
    Invoke-Native docker @Compose restart autonomy-redis
    Invoke-Native docker @Compose up -d --wait --wait-timeout $WaitTimeoutSeconds autonomy-redis
    Invoke-Native docker @Compose run --rm smoke-runner verify-persistence

    Invoke-Native docker @Compose up -d --wait --wait-timeout $WaitTimeoutSeconds autonomy-worker
    Invoke-Native docker @Compose run --rm smoke-runner worker-and-duplicate
    Invoke-Native docker @Compose run --rm smoke-runner lease-and-boundary
    Invoke-Native docker @Compose run --rm smoke-runner checkpoint-and-cancel

    # Deterministic real Worker crash: freeze the already-ready Worker, then
    # start a one-off probe which locks the interrupted Step row and queues a
    # production drive_run task.  The Worker can consume and commit its MySQL
    # lease but blocks while persisting the recovery boundary.  Only after the
    # committed lease is observed do we send real SIGKILL.
    Invoke-Native docker @Compose pause autonomy-worker
    $BlockerName = "$ProjectName-worker-kill-lock"
    & docker @Compose run -d --name $BlockerName smoke-runner hold-worker-lock | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'cannot start the worker-kill row-lock fixture'
    }
    $BlockerRunning = (& docker inspect --format '{{.State.Running}}' $BlockerName).Trim()
    if ($LASTEXITCODE -ne 0 -or $BlockerRunning -ne 'true') {
        throw 'worker-kill row-lock fixture exited before readiness'
    }
    Invoke-Native docker @Compose run --rm smoke-runner wait-worker-lock-ready
    Invoke-Native docker @Compose unpause autonomy-worker
    Invoke-Native docker @Compose run --rm smoke-runner wait-worker-lease
    Invoke-Native docker @Compose kill --signal SIGKILL autonomy-worker
    Invoke-Native docker rm -f $BlockerName
    $BlockerName = $null
    # Restart immediately, while the persisted old lease is still live.  The
    # duplicate broker delivery must retry itself across expiry; startup scan
    # cannot claim credit because the next probe requires the old lease alive.
    Invoke-Native docker @Compose up -d --wait --wait-timeout $WaitTimeoutSeconds autonomy-worker
    Invoke-Native docker @Compose run --rm smoke-runner verify-restart-before-expiry
    Invoke-Native docker @Compose run --rm smoke-runner verify-worker-kill-recovery

    $BackendImageId = (& docker image inspect --format '{{.Id}}' $env:OGS_S2_SMOKE_BACKEND_IMAGE).Trim()
    if ($LASTEXITCODE -ne 0 -or $BackendImageId -notmatch '^sha256:[0-9a-f]{64}$') {
        throw 'cannot resolve the exact-head backend image ID'
    }
    $ImageIds = [ordered]@{ backend = $BackendImageId }
    foreach ($Service in @(
        'mysql-fresh', 'mysql-upgrade', 'business-redis',
        'autonomy-redis', 'autonomy-worker'
    )) {
        $ContainerId = (& docker @Compose ps -q $Service).Trim()
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($ContainerId)) {
            throw "cannot resolve container for $Service"
        }
        $ImageId = (& docker inspect --format '{{.Image}}' $ContainerId).Trim()
        if ($LASTEXITCODE -ne 0 -or $ImageId -notmatch '^sha256:[0-9a-f]{64}$') {
            throw "cannot resolve image ID for $Service"
        }
        $ImageIds[$Service] = $ImageId
    }
    foreach ($Entry in $ImageIds.GetEnumerator()) {
        Write-Host ("[S2 smoke] image {0}={1}" -f $Entry.Key, $Entry.Value)
    }

    Write-Warning (
        '[S2 smoke] NOT_RUN_SSH_GATE: in-flight SSH cancellation and ' +
        'approved-step execution recovery still require a disposable real ' +
        'SSH target plus trustworthy remote process-group stop confirmation.'
    )

    $Succeeded = $true
    Write-Host (
        '[S2 smoke] INFRASTRUCTURE_SUBSET_PASS: fresh/upgrade MySQL, Redis ' +
        'persistence, lease expiry, duplicate retry, immediate Worker ' +
        'SIGKILL/restart, ' +
        'checkpoint safety boundaries and pre-side-effect cancellation'
    )
}
finally {
    if ($null -ne $BlockerName) {
        & docker rm -f $BlockerName | Out-Null
        $BlockerName = $null
    }
    if ($Keep) {
        Write-Warning "S2 smoke resources kept for inspection: project=$ProjectName"
        Write-Warning 'Cleanup with the same COMPOSE_PROJECT_NAME and archived Compose file shown above.'
    }
    else {
        & docker @Compose down --volumes --remove-orphans --timeout 10
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "automatic Compose cleanup failed for project $ProjectName"
        }
        if ($null -ne $TempRoot -and (Test-Path -LiteralPath $TempRoot)) {
            $ResolvedTemp = (Resolve-Path -LiteralPath $TempRoot).Path
            $ExpectedPrefix = [IO.Path]::GetFullPath($SmokeTempParent) + [IO.Path]::DirectorySeparatorChar
            if (-not $ResolvedTemp.StartsWith($ExpectedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
                throw "refusing to remove unexpected smoke path: $ResolvedTemp"
            }
            Remove-Item -LiteralPath $ResolvedTemp -Recurse -Force
        }
    }
}

if (-not $Succeeded) {
    throw 'S2 smoke did not reach its acceptance gate'
}
