param(
    [string]$ParentDirectory = [Environment]::GetFolderPath("Desktop")
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Confirm-Step {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Purpose,

        [Parameter(Mandatory = $true)]
        [string]$Command
    )

    Write-Host ""
    Write-Host "Purpose: $Purpose" -ForegroundColor Cyan
    Write-Host "Command:" -ForegroundColor Yellow
    Write-Host "  $Command"

    $answer = Read-Host "Run this step? Type Y to continue"
    if ($answer -notmatch "(?i)^y(es)?$") {
        Write-Host "Stopped before running this command."
        exit 0
    }
}

function Assert-LastCommandSucceeded {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command
    )

    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $Command"
    }
}

function Invoke-Step {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Purpose,

        [Parameter(Mandatory = $true)]
        [string]$Command,

        [Parameter(Mandatory = $true)]
        [scriptblock]$Action
    )

    Confirm-Step -Purpose $Purpose -Command $Command
    & $Action
}

function Add-UvInstallPath {
    $userProfile = [Environment]::GetFolderPath("UserProfile")
    $candidateDirectories = @(
        (Join-Path $userProfile ".local\bin"),
        (Join-Path $userProfile ".cargo\bin")
    )

    foreach ($directory in $candidateDirectories) {
        $uvPath = Join-Path $directory "uv.exe"
        if ((Test-Path -LiteralPath $uvPath) -and ($env:Path -notlike "*$directory*")) {
            $env:Path = "$directory;$env:Path"
        }
    }
}

function Invoke-Uv {
    param(
        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$Arguments
    )

    Add-UvInstallPath
    $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
    if ($null -eq $uvCommand) {
        throw "uv was not found in PATH. Restart PowerShell or run: uv tool update-shell"
    }

    & $uvCommand.Source @Arguments
    Assert-LastCommandSucceeded -Command "uv $($Arguments -join ' ')"
}

if ([string]::IsNullOrWhiteSpace($ParentDirectory)) {
    throw "ParentDirectory cannot be empty."
}

$resolvedParentDirectory = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($ParentDirectory)
if (-not (Test-Path -LiteralPath $resolvedParentDirectory -PathType Container)) {
    throw "Parent directory does not exist: $resolvedParentDirectory"
}

$repoPath = Join-Path $resolvedParentDirectory "claude-template"
$repoFolderAlreadyExisted = Test-Path -LiteralPath $repoPath

Write-Host "Claude template Windows setup"
Write-Host "Target parent folder: $resolvedParentDirectory"
Write-Host "Repository folder: $repoPath"

Invoke-Step `
    -Purpose "Install uv, the Python package and tool manager used by this template." `
    -Command 'powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"' `
    -Action {
        powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
        Assert-LastCommandSucceeded -Command 'powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
        Add-UvInstallPath
    }

Invoke-Step `
    -Purpose "Move into the folder where the template will be cloned." `
    -Command "cd `"$resolvedParentDirectory`"" `
    -Action {
        Set-Location -LiteralPath $resolvedParentDirectory
    }

Invoke-Step `
    -Purpose "Clone claude-template into the selected folder, or reuse an existing clone from an earlier run." `
    -Command "git clone https://github.com/vela-wood/claude-template.git" `
    -Action {
        if (Test-Path -LiteralPath $repoPath) {
            $originUrl = git -C $repoPath remote get-url origin 2>$null
            if ($LASTEXITCODE -ne 0) {
                throw "Repository folder already exists but is not a usable Git clone: $repoPath"
            }

            $expectedOrigins = @(
                "https://github.com/vela-wood/claude-template.git",
                "git@github.com:vela-wood/claude-template.git"
            )
            if ($originUrl -notin $expectedOrigins) {
                throw "Repository folder already exists with unexpected origin '$originUrl': $repoPath"
            }

            Write-Host "Repository folder already exists; reusing it."
            return
        }

        git clone https://github.com/vela-wood/claude-template.git
        Assert-LastCommandSucceeded -Command "git clone https://github.com/vela-wood/claude-template.git"
    }

Invoke-Step `
    -Purpose "Enter the cloned claude-template repository." `
    -Command "cd claude-template" `
    -Action {
        Set-Location -LiteralPath $repoPath
    }

Invoke-Step `
    -Purpose "Switch the template to the Windows branch." `
    -Command "git checkout windows" `
    -Action {
        git checkout windows
        Assert-LastCommandSucceeded -Command "git checkout windows"
    }

if ($repoFolderAlreadyExisted) {
    Invoke-Step `
        -Purpose "Pull the latest Windows branch because this setup is reusing an existing clone." `
        -Command "git pull" `
        -Action {
            Set-Location -LiteralPath $repoPath
            git pull
            Assert-LastCommandSucceeded -Command "git pull"
        }
}

Invoke-Step `
    -Purpose "Install the template dependencies into its uv-managed environment." `
    -Command "uv sync" `
    -Action {
        Invoke-Uv sync
    }

Invoke-Step `
    -Purpose "Install the Caption CLI globally through uv using Python 3.13." `
    -Command 'uv tool install --force --python 3.13 "caption-cli @ git+https://github.com/sec-chair/caption-cli.git"' `
    -Action {
        Invoke-Uv tool install --force --python 3.13 "caption-cli @ git+https://github.com/sec-chair/caption-cli.git"
    }

Write-Host ""
Write-Host "Install completed. Please run uv run setup_claude.py for caption integration." -ForegroundColor Green
Write-Host "Repository folder: $repoPath"
