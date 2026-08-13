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
$CleanupFailed = $false
$ComposeReady = $false
$Compose = @()
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
if (-not (Get-Command ssh-keygen -ErrorAction SilentlyContinue)) {
    throw 'OpenSSH ssh-keygen is required for the disposable SSH target'
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
$BackendImageName = 'orangeserver-s2-smoke:' + $Suffix
$SshImageName = 'orangeserver-s2-ssh:' + $Suffix
$SmokeTempParent = Join-Path $RepoRoot '.tmp/s2-smoke'
$TempRoot = Join-Path $SmokeTempParent $Suffix
$SourceRoot = Join-Path $TempRoot 'source'
$ArchivePath = Join-Path $TempRoot 'source.tar'
$FixtureRoot = Join-Path $SourceRoot '.s2-smoke'
$V104Schema = Join-Path $FixtureRoot 'v1.0.4-orange.sql'
$SshPrivateKey = Join-Path $FixtureRoot 's2-client-key'
$SshPublicKey = $SshPrivateKey + '.pub'
try {
    New-Item -ItemType Directory -Force -Path $TempRoot | Out-Null
    New-Item -ItemType Directory -Force -Path $SourceRoot | Out-Null

    # Reject Git links before extraction.  A reviewed exact HEAD must not be
    # able to make tar follow a path outside the disposable source root.
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
        'deploy/s2-smoke/Dockerfile.ssh-target',
        'deploy/s2-smoke/ssh-target-entrypoint.sh',
        'deploy/s2-smoke/sshd_config',
        'deploy/s2-smoke/uptime-wrapper.sh',
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

    # The client credential exists only under the disposable extracted tree.
    $SshKeyArguments = @(
        '-q', '-t', 'rsa', '-b', '3072', '-N', '',
        '-C', 'orangeserver-s2-disposable', '-f', $SshPrivateKey
    )
    Invoke-Native ssh-keygen @SshKeyArguments
    if (
        -not (Test-Path -LiteralPath $SshPrivateKey -PathType Leaf) -or
        -not (Test-Path -LiteralPath $SshPublicKey -PathType Leaf)
    ) {
        throw 'ssh-keygen did not produce the disposable client key pair'
    }

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

    $Compose = @('compose', '-f', $ComposeFile)
    $ComposeReady = $true
    Write-Host "[S2 smoke] HEAD=$Head archive_sha256=$ArchiveSha256 project=$ProjectName"
    Invoke-Native docker @Compose config --quiet
    Invoke-Native docker @Compose build smoke-runner autonomy-worker ssh-target
    Invoke-Native docker @Compose run --rm ssh-key-init
    Invoke-Native docker @Compose up -d --wait --wait-timeout $WaitTimeoutSeconds `
        mysql-fresh mysql-upgrade business-redis autonomy-redis ssh-target

    Invoke-Native docker @Compose run --rm smoke-runner migrate-and-prime
    Invoke-Native docker @Compose run --rm smoke-runner langgraph-pause-first
    Invoke-Native docker @Compose run --rm smoke-runner langgraph-resume-to-second

    # Persistence is only evidence after a real process restart. The named
    # volume remains attached; a new saver/graph instance must recover the
    # second interrupt and complete it, in addition to the DB 0 marker check.
    Invoke-Native docker @Compose restart autonomy-redis
    Invoke-Native docker @Compose up -d --wait --wait-timeout $WaitTimeoutSeconds autonomy-redis
    Invoke-Native docker @Compose run --rm smoke-runner verify-persistence
    Invoke-Native docker @Compose run --rm smoke-runner langgraph-resume-after-restart
    Invoke-Native docker @Compose run --rm smoke-runner ssh-prime-target

    Invoke-Native docker @Compose up -d --wait --wait-timeout $WaitTimeoutSeconds autonomy-worker
    # Feature-off is an application-isolation contract. Both autonomy services
    # are truly unavailable during this probe.
    Invoke-Native docker @Compose stop autonomy-worker autonomy-redis
    # The same exact-head Flask app exercises legacy Flask route/application
    # boundaries without creating Run or command-audit state. This does not
    # claim a Gunicorn/listener/network-path smoke.
    Invoke-Native docker @Compose run --rm --no-deps `
        --env 'OGS_AI_AUTONOMY_ENABLED=false' `
        --env 'OGS_AI_AUTONOMY_REDIS_HOST=autonomy-redis-unavailable' `
        smoke-runner feature-off-isolation
    Invoke-Native docker @Compose up -d --wait --wait-timeout $WaitTimeoutSeconds autonomy-redis
    Invoke-Native docker @Compose up -d --wait --wait-timeout $WaitTimeoutSeconds autonomy-worker
    Invoke-Native docker @Compose run --rm smoke-runner production-checkpoint-loss-boundary
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
    $LeaseOutput = @(& docker @Compose run --rm smoke-runner wait-worker-lease)
    if ($LASTEXITCODE -ne 0) {
        throw 'cannot observe the real Worker lease before SIGKILL'
    }
    $LeaseEvidenceLines = @(
        $LeaseOutput | Where-Object { $_.StartsWith('S2_WORKER_LEASE_EVIDENCE=') }
    )
    if ($LeaseEvidenceLines.Count -ne 1) {
        throw 'Worker lease probe did not return exactly one evidence record'
    }
    $LeaseEvidence = $LeaseEvidenceLines[0].Substring(
        'S2_WORKER_LEASE_EVIDENCE='.Length
    ) | ConvertFrom-Json
    if (
        [string]::IsNullOrWhiteSpace([string]$LeaseEvidence.lease_owner) -or
        [string]::IsNullOrWhiteSpace([string]$LeaseEvidence.lease_expires_at)
    ) {
        throw 'Worker lease evidence omitted its owner or expiry'
    }
    Invoke-Native docker @Compose kill --signal SIGKILL autonomy-worker
    Invoke-Native docker rm -f $BlockerName
    $BlockerName = $null
    # Restart immediately, while the persisted old lease is still live.  The
    # duplicate broker delivery must retry itself across expiry; startup scan
    # cannot claim credit because the next probe requires the old lease alive.
    Invoke-Native docker @Compose up -d --wait --wait-timeout $WaitTimeoutSeconds autonomy-worker
    Invoke-Native docker @Compose run --rm `
        --env "OGS_S2_EXPECTED_LEASE_OWNER=$($LeaseEvidence.lease_owner)" `
        --env "OGS_S2_EXPECTED_LEASE_EXPIRES_AT=$($LeaseEvidence.lease_expires_at)" `
        smoke-runner verify-restart-before-expiry
    Invoke-Native docker @Compose run --rm smoke-runner verify-worker-kill-recovery

    # Crash before the Executor can commit execution_started/write_intent.
    # The locked approved Step proves that no remote side effect can begin;
    # after lease expiry, production recovery must execute it exactly once.
    Invoke-Native docker @Compose pause autonomy-worker
    $BlockerName = "$ProjectName-ssh-pre-intent-lock"
    & docker @Compose run -d --name $BlockerName smoke-runner hold-ssh-pre-intent-lock | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'cannot start the pre-intent SSH row-lock fixture'
    }
    $BlockerRunning = (& docker inspect --format '{{.State.Running}}' $BlockerName).Trim()
    if ($LASTEXITCODE -ne 0 -or $BlockerRunning -ne 'true') {
        throw 'pre-intent SSH row-lock fixture exited before readiness'
    }
    Invoke-Native docker @Compose run --rm smoke-runner wait-ssh-pre-intent-lock
    Invoke-Native docker @Compose unpause autonomy-worker
    Invoke-Native docker @Compose run --rm smoke-runner wait-ssh-pre-intent-lease
    Invoke-Native docker @Compose kill --signal SIGKILL autonomy-worker
    Invoke-Native docker rm -f $BlockerName
    $BlockerName = $null
    Invoke-Native docker @Compose up -d --wait --wait-timeout $WaitTimeoutSeconds autonomy-worker
    Invoke-Native docker @Compose run --rm smoke-runner verify-ssh-pre-intent-recovery

    # No broker publish occurs in this phase. The already-running Worker's
    # real 30-second timer must discover and dispatch the queued Run.
    Invoke-Native docker @Compose run --rm smoke-runner worker-timer-recovery

    # End-to-end production side-effect path: Celery task -> Driver ->
    # Executor -> default runner -> cancellable SSH channel. The target is an
    # internal-only OpenSSH daemon and all commands run as uid 10001.
    Invoke-Native docker @Compose run --rm smoke-runner ssh-exit-and-streams
    Invoke-Native docker @Compose run --rm smoke-runner ssh-cancel-process-group
    Invoke-Native docker @Compose run --rm smoke-runner ssh-runtime-environment-revocation
    Invoke-Native docker @Compose run --rm smoke-runner ssh-file-patch-restore

    # A read-only probe is safe to retry, but the first remote process must be
    # gone before recovery starts and only one terminal outcome may persist.
    Invoke-Native docker @Compose run --rm smoke-runner ssh-start-readonly
    Invoke-Native docker @Compose run --rm smoke-runner wait-ssh-readonly-started
    Invoke-Native docker @Compose kill --signal SIGKILL autonomy-worker
    Invoke-Native docker @Compose run --rm smoke-runner release-ssh-readonly-first-attempt
    Invoke-Native docker @Compose up -d --wait --wait-timeout $WaitTimeoutSeconds autonomy-worker
    Invoke-Native docker @Compose run --rm smoke-runner verify-ssh-readonly-recovery

    # This remains a real production execution. The target path is inside the
    # approved /opt root but resolves through a symlink to /tmp; no marker may
    # be returned to the caller. Run it before the uncertain-write gate, whose
    # required needs_attention row deliberately retains this host reservation.
    Invoke-Native docker @Compose run --rm smoke-runner ssh-symlink-boundary

    # Kill the real Worker only after the remote marker proves a write began
    # after durable write_intent. A duplicate delivery waits behind the live
    # fenced lease, then recovery must mark outcome_unknown without replay.
    Invoke-Native docker @Compose run --rm smoke-runner ssh-start-write
    Invoke-Native docker @Compose run --rm smoke-runner wait-ssh-write-started
    Invoke-Native docker @Compose kill --signal SIGKILL autonomy-worker
    Invoke-Native docker @Compose up -d --wait --wait-timeout $WaitTimeoutSeconds autonomy-worker
    Invoke-Native docker @Compose run --rm smoke-runner verify-ssh-write-recovery

    $BackendImageId = (& docker image inspect --format '{{.Id}}' $env:OGS_S2_SMOKE_BACKEND_IMAGE).Trim()
    if ($LASTEXITCODE -ne 0 -or $BackendImageId -notmatch '^sha256:[0-9a-f]{64}$') {
        throw 'cannot resolve the exact-head backend image ID'
    }
    $ImageIds = [ordered]@{ backend = $BackendImageId }
    foreach ($Service in @(
        'mysql-fresh', 'mysql-upgrade', 'business-redis',
        'autonomy-redis', 'ssh-target', 'autonomy-worker'
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

    $Succeeded = $true
}
finally {
    if ($null -ne $BlockerName) {
        & docker rm -f $BlockerName | Out-Null
        if ($LASTEXITCODE -ne 0) {
            $CleanupFailed = $true
        }
        $BlockerName = $null
    }
    if ($Keep) {
        Write-Warning "S2 smoke resources kept for inspection: project=$ProjectName"
        Write-Warning 'Cleanup with the same COMPOSE_PROJECT_NAME and archived Compose file shown above.'
    }
    else {
        if ($ComposeReady) {
            & docker @Compose down --volumes --remove-orphans --timeout 10
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
    throw 'S2 smoke did not reach its acceptance gate'
}
if ($CleanupFailed) {
    throw "S2 smoke reached acceptance but cleanup failed; retry source retained at $TempRoot"
}

Write-Host (
    '[S2 smoke] DISPOSABLE_S2_PASS: fresh/upgrade MySQL, real ' +
    'ShallowRedisSaver double-interrupt AOF recovery, duplicate delivery, ' +
    'lease expiry, Worker SIGKILL recovery, exact SSH exit/dual streams, ' +
    'confirmed process-group cancellation, uncertain-write non-replay, ' +
    'and bounded-root symlink refusal'
)
