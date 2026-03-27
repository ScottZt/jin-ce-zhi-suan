param(
    [string]$PublicRemote = "public",
    [string]$PrivateRemote = "private",
    [string]$PublicBranch = "main",
    [string]$PrivateBranch = "private-main",
    [string]$PublicRepoUrl = "https://github.com/ScottZt/jin-ce-zhi-suan.git",
    [string]$PrivateRepoUrl = "https://gitee.com/SeniorAgentTeam/jin-ce-zhi-suan.git",
    [string]$PublicCommitMessage = "",
    [string]$PrivateCommitMessage = "chore: update private config",
    [string]$PublicSourceBranch = "",
    [switch]$UseLocalCommits,
    [switch]$SkipPublicPush,
    [switch]$SkipPrivatePush,
    [switch]$StrictPublicPush,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Invoke-Git {
    param(
        [Parameter(Mandatory = $true)][string[]]$Args,
        [switch]$AllowFailure,
        [switch]$ReadOnly
    )
    $display = $Args -join " "
    if ($DryRun -and -not $ReadOnly) {
        Write-Host "[DRY-RUN] git $display"
        return @{
            ExitCode = 0
            Output = @()
        }
    }
    $originalErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = & git @Args 2>&1
    }
    finally {
        $ErrorActionPreference = $originalErrorActionPreference
    }
    $exitCode = $LASTEXITCODE
    if (-not $AllowFailure -and $exitCode -ne 0) {
        throw "git $display 执行失败：`n$($output -join "`n")"
    }
    return @{
        ExitCode = $exitCode
        Output = @($output)
    }
}

function Ensure-Remote {
    param(
        [string]$Name,
        [string]$Url
    )
    $remoteOutput = Invoke-Git -Args @("remote") -ReadOnly
    $remoteSet = @($remoteOutput.Output | ForEach-Object { "$_".Trim() }) -contains $Name
    if ($remoteSet) {
        Invoke-Git -Args @("remote", "set-url", $Name, $Url) | Out-Null
        Invoke-Git -Args @("remote", "set-url", "--push", $Name, $Url) | Out-Null
    } else {
        Invoke-Git -Args @("remote", "add", $Name, $Url) | Out-Null
        Invoke-Git -Args @("remote", "set-url", $Name, $Url) | Out-Null
        Invoke-Git -Args @("remote", "set-url", "--push", $Name, $Url) | Out-Null
    }
}

function Ensure-LocalBranch {
    param(
        [string]$BranchName,
        [string]$FromBranch
    )
    $verify = Invoke-Git -Args @("rev-parse", "--verify", $BranchName) -AllowFailure -ReadOnly
    if ($verify.ExitCode -ne 0) {
        Invoke-Git -Args @("checkout", $FromBranch) | Out-Null
        Invoke-Git -Args @("branch", $BranchName, $FromBranch) | Out-Null
    }
}

function Ensure-BranchExists {
    param([string]$BranchName)
    $verify = Invoke-Git -Args @("rev-parse", "--verify", $BranchName) -AllowFailure -ReadOnly
    if ($verify.ExitCode -ne 0) {
        throw "本地分支不存在：$BranchName"
    }
}

function Get-CommitSubject {
    param([string]$Ref)
    $subjectResult = Invoke-Git -Args @("log", "-1", "--pretty=%s", $Ref) -AllowFailure -ReadOnly
    if ($subjectResult.ExitCode -ne 0 -or @($subjectResult.Output).Count -eq 0) {
        return ""
    }
    return "$($subjectResult.Output[0])".Trim()
}

function Sanitize-CommitMessage {
    param([string]$Message)
    $sanitized = "$Message"
    $replaceRules = @(
        @{ Pattern = "(?i)\b(api[\s\-_]*key|access[\s\-_]*key|secret|token|password|passwd|pwd)\b"; Replacement = "[REDACTED]" },
        @{ Pattern = "(?i)\b(private|internal|confidential|credential|credentials)\b"; Replacement = "[REDACTED]" },
        @{ Pattern = "(?i)(密钥|秘钥|口令|密码|令牌|凭证|私有|内部|内网)"; Replacement = "[REDACTED]" }
    )
    foreach ($rule in $replaceRules) {
        $sanitized = [regex]::Replace($sanitized, $rule.Pattern, $rule.Replacement)
    }
    $sanitized = [regex]::Replace($sanitized, "\s+", " ").Trim()
    if ([string]::IsNullOrWhiteSpace($sanitized)) {
        return "chore: sync public updates"
    }
    return $sanitized
}

function Has-WorkingChanges {
    $status = Invoke-Git -Args @("status", "--porcelain") -ReadOnly
    return ($status.Output | Measure-Object).Count -gt 0
}

function Get-AheadCount {
    param(
        [string]$LocalRef,
        [string]$RemoteRef
    )
    $result = Invoke-Git -Args @("rev-list", "--count", "$RemoteRef..$LocalRef") -AllowFailure -ReadOnly
    if ($result.ExitCode -ne 0 -or @($result.Output).Count -eq 0) {
        return -1
    }
    $parsed = 0
    if (-not [int]::TryParse("$($result.Output[0])".Trim(), [ref]$parsed)) {
        return -1
    }
    return $parsed
}

function Find-StashRefByMessage {
    param([string]$Message)
    $list = Invoke-Git -Args @("stash", "list") -ReadOnly
    foreach ($line in $list.Output) {
        $text = "$line"
        if ($text -match "^(stash@\{\d+\}):" -and $text.Contains($Message)) {
            return $Matches[1]
        }
    }
    return ""
}

function Resolve-RepoRoot {
    $candidate = Split-Path -Parent $PSCommandPath
    while (-not [string]::IsNullOrWhiteSpace($candidate)) {
        if (Test-Path (Join-Path $candidate ".git")) {
            return $candidate
        }
        $parent = Split-Path -Parent $candidate
        if ($parent -eq $candidate) {
            break
        }
        $candidate = $parent
    }
    throw "未找到 .git 仓库目录。请确认当前项目仍是 Git 仓库（.git 未被删除），或先在项目根目录执行 git init 并配置远端。"
}

$repoRoot = Resolve-RepoRoot
Set-Location $repoRoot
$startBranch = (Invoke-Git -Args @("rev-parse", "--abbrev-ref", "HEAD") -ReadOnly).Output[0].Trim()
$effectivePublicSourceBranch = $PublicSourceBranch
if ([string]::IsNullOrWhiteSpace($effectivePublicSourceBranch)) {
    $effectivePublicSourceBranch = $startBranch
}
$effectivePublicCommitMessage = $PublicCommitMessage
if ([string]::IsNullOrWhiteSpace($effectivePublicCommitMessage)) {
    $sourceSubject = Get-CommitSubject -Ref $effectivePublicSourceBranch
    $effectivePublicCommitMessage = Sanitize-CommitMessage -Message $sourceSubject
} else {
    $effectivePublicCommitMessage = Sanitize-CommitMessage -Message $effectivePublicCommitMessage
}
$privateStashCreated = $false
$entryStashCreated = $false
$entryStashTag = "dual-repo-entry-temp-" + [DateTime]::Now.ToString("yyyyMMddHHmmssfff")
$publicPushFailed = $false
$publicWorkBranch = "__dual_repo_public_work__"
$privateStrategyGuardPath = "data/strategies/custom_strategies.private.json"
$sourceCommitBeforePublic = ""
$sourcePrivateFileExistsBeforePublic = $false

try {
    if ($SkipPublicPush -and $SkipPrivatePush) {
        throw "参数冲突：-SkipPublicPush 与 -SkipPrivatePush 不能同时使用。"
    }
    $publicDenyList = @(
        "config.private.json",
        "data/strategies/custom_strategies.private.json",
        ".env",
        ".env.*",
        "双仓库推送指南.md",
        "数据接口系统的操作手册.md",
        "server.log",
        "USER_GUIDE.md"
    )
    $privateOnlyIncludeList = @(
        "config.private.json",
        "data/strategies/custom_strategies.private.json"
    )
    Ensure-Remote -Name $PublicRemote -Url $PublicRepoUrl
    Ensure-Remote -Name $PrivateRemote -Url $PrivateRepoUrl
    Ensure-LocalBranch -BranchName $PrivateBranch -FromBranch $effectivePublicSourceBranch
    Ensure-BranchExists -BranchName $effectivePublicSourceBranch
    $sourceCommitBeforePublic = (Invoke-Git -Args @("rev-parse", $effectivePublicSourceBranch) -ReadOnly).Output[0].Trim()
    $sourcePrivateSpecBefore = "{0}:{1}" -f $effectivePublicSourceBranch, $privateStrategyGuardPath
    $sourcePrivateFileProbeBefore = Invoke-Git -Args @("cat-file", "-e", $sourcePrivateSpecBefore) -AllowFailure -ReadOnly
    $sourcePrivateFileExistsBeforePublic = $sourcePrivateFileProbeBefore.ExitCode -eq 0

    $entryBefore = @((Invoke-Git -Args @("stash", "list") -ReadOnly).Output).Count
    Invoke-Git -Args @("stash", "push", "--include-untracked", "-m", $entryStashTag) -AllowFailure | Out-Null
    $entryAfter = @((Invoke-Git -Args @("stash", "list") -ReadOnly).Output).Count
    $entryStashCreated = $entryAfter -gt $entryBefore

    Invoke-Git -Args @("checkout", "-B", $publicWorkBranch, $effectivePublicSourceBranch) | Out-Null
    if ($UseLocalCommits) {
        if (Has-WorkingChanges) {
            throw "已启用 -UseLocalCommits：检测到未提交改动，请先手动提交后再执行脚本。"
        }
    } else {
        Invoke-Git -Args @("add", "-A") | Out-Null
        foreach ($path in $publicDenyList) {
            Invoke-Git -Args @("rm", "--cached", "--ignore-unmatch", $path) -AllowFailure | Out-Null
        }
        if (Has-WorkingChanges) {
            Invoke-Git -Args @("commit", "-m", $effectivePublicCommitMessage) | Out-Null
        } else {
            Write-Host "公共分支无可提交改动，跳过 commit。"
        }
    }
    if ($SkipPublicPush) {
        Write-Host "已指定 SkipPublicPush，跳过 public push。"
    } else {
        Invoke-Git -Args @("fetch", $PublicRemote, $PublicBranch) -AllowFailure | Out-Null
        $publicRemoteRef = "$PublicRemote/$PublicBranch"
        $aheadCount = Get-AheadCount -LocalRef "HEAD" -RemoteRef $publicRemoteRef
        if ($aheadCount -eq 0) {
            Write-Host "提示：$PublicBranch 相对 $publicRemoteRef 没有新增提交，push 可能显示 up-to-date。"
        } elseif ($aheadCount -gt 0) {
            Write-Host "提示：检测到 $aheadCount 个待推送提交到 $publicRemoteRef。"
        }
        $publicPushResult = Invoke-Git -Args @("push", $PublicRemote, "HEAD:$PublicBranch") -AllowFailure
        if ($publicPushResult.ExitCode -ne 0) {
            $publicPushFailed = $true
            if ($StrictPublicPush) {
                throw "public push 失败，已按 StrictPublicPush 中止：`n$($publicPushResult.Output -join "`n")"
            }
            Write-Host "警告：public push 失败，继续执行 private push。"
            Write-Host ($publicPushResult.Output -join "`n")
        }
    }
    $sourceCommitAfterPublic = (Invoke-Git -Args @("rev-parse", $effectivePublicSourceBranch) -ReadOnly).Output[0].Trim()
    if ($sourceCommitAfterPublic -ne $sourceCommitBeforePublic) {
        throw "保护触发：公共阶段意外改动了源分支 $effectivePublicSourceBranch（$sourceCommitBeforePublic -> $sourceCommitAfterPublic），已中止。"
    }
    if ($sourcePrivateFileExistsBeforePublic) {
        $sourcePrivateSpecAfter = "{0}:{1}" -f $effectivePublicSourceBranch, $privateStrategyGuardPath
        $sourcePrivateFileProbeAfter = Invoke-Git -Args @("cat-file", "-e", $sourcePrivateSpecAfter) -AllowFailure -ReadOnly
        if ($sourcePrivateFileProbeAfter.ExitCode -ne 0) {
            throw "保护触发：公共阶段后检测到 $privateStrategyGuardPath 在源分支中丢失，已中止。"
        }
    }

    if ($SkipPrivatePush) {
        Write-Host "已指定 SkipPrivatePush，跳过 private push。"
    } else {
        $stashBeforeCount = @((Invoke-Git -Args @("stash", "list") -ReadOnly).Output).Count
        Invoke-Git -Args @("stash", "push", "--include-untracked", "-m", "dual-repo-temp-private") -AllowFailure | Out-Null
        $stashAfterCount = @((Invoke-Git -Args @("stash", "list") -ReadOnly).Output).Count
        $privateStashCreated = $stashAfterCount -gt $stashBeforeCount

        Invoke-Git -Args @("checkout", $PrivateBranch) | Out-Null
        Invoke-Git -Args @("merge", $effectivePublicSourceBranch, "--no-edit") | Out-Null
        if ($privateStashCreated) {
            Invoke-Git -Args @("stash", "apply", "stash@{0}") | Out-Null
            Invoke-Git -Args @("stash", "drop", "stash@{0}") | Out-Null
            $privateStashCreated = $false
        }
        foreach ($path in $privateOnlyIncludeList) {
            if (Test-Path (Join-Path $repoRoot $path)) {
                Invoke-Git -Args @("add", "-f", $path) -AllowFailure | Out-Null
            }
        }
        if (Has-WorkingChanges) {
            Invoke-Git -Args @("commit", "-m", $PrivateCommitMessage) | Out-Null
        } else {
            Write-Host "私有分支无可提交改动，跳过 commit。"
        }
        Invoke-Git -Args @("push", $PrivateRemote, $PrivateBranch) | Out-Null
    }
    if ($publicPushFailed) {
        Write-Host "private push 已完成，但 public push 失败（见上方警告）。"
    } else {
        Write-Host "双仓库推送完成。"
    }
}
finally {
    if (-not $DryRun) {
        $currentBranch = (Invoke-Git -Args @("rev-parse", "--abbrev-ref", "HEAD") -ReadOnly).Output[0].Trim()
        if ($currentBranch -ne $startBranch) {
            Invoke-Git -Args @("checkout", $startBranch) -AllowFailure | Out-Null
        }
        $publicWorkBranchVerify = Invoke-Git -Args @("rev-parse", "--verify", $publicWorkBranch) -AllowFailure -ReadOnly
        if ($publicWorkBranchVerify.ExitCode -eq 0 -and $startBranch -ne $publicWorkBranch) {
            Invoke-Git -Args @("branch", "-D", $publicWorkBranch) -AllowFailure | Out-Null
        }
        if ($entryStashCreated) {
            $entryRef = Find-StashRefByMessage -Message $entryStashTag
            if (-not [string]::IsNullOrWhiteSpace($entryRef)) {
                Invoke-Git -Args @("stash", "apply", $entryRef) -AllowFailure | Out-Null
                Invoke-Git -Args @("stash", "drop", $entryRef) -AllowFailure | Out-Null
            } else {
                Write-Host "检测到入口临时 stash 未自动恢复，请手动执行：git stash list"
            }
        }
        if ($privateStashCreated) {
            Write-Host "检测到未恢复的临时 stash，请手动执行：git stash list / git stash apply stash@{0}"
        }
    }
}
