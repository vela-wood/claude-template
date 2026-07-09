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

Write-Host "Claude template Windows setup"

Invoke-Step `
    -Purpose "Pull the latest changes from the remote repository." `
    -Command "git pull" `
    -Action {
        git pull
        Assert-LastCommandSucceeded -Command "git pull"
    }

Invoke-Step `
    -Purpose "Install uv, the Python package and tool manager used by this template." `
    -Command 'powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"' `
    -Action {
        powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
        Assert-LastCommandSucceeded -Command 'powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
        Add-UvInstallPath
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
