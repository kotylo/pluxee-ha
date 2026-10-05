#requires -Version 7.0

[CmdletBinding()]
param(
    [string] $EnvironmentFile = (Join-Path $PSScriptRoot ".env")
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Read-DotEnv {
    param([Parameter(Mandatory)] [string] $Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Configuration file not found: $Path. Copy .env.example to .env first."
    }

    $values = @{}
    foreach ($rawLine in Get-Content -LiteralPath $Path) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith("#")) {
            continue
        }

        $separator = $line.IndexOf("=")
        if ($separator -lt 1) {
            throw "Invalid configuration line in ${Path}"
        }

        $key = $line.Substring(0, $separator).Trim()
        $value = $line.Substring($separator + 1).Trim()
        if ($key -notmatch "^[A-Za-z_][A-Za-z0-9_]*$") {
            throw "Invalid configuration key: $key"
        }
        if (
            $value.Length -ge 2 -and
            (($value.StartsWith('"') -and $value.EndsWith('"')) -or
             ($value.StartsWith("'") -and $value.EndsWith("'")))
        ) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $values[$key] = $value
    }
    return $values
}

function Invoke-ExternalCommand {
    param(
        [Parameter(Mandatory)] [string] $Executable,
        [Parameter(Mandatory)] [string[]] $Arguments,
        [Parameter(Mandatory)] [string] $FailureMessage
    )

    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FailureMessage (exit code $LASTEXITCODE)"
    }
}

$settings = Read-DotEnv -Path $EnvironmentFile
$requiredKeys = @("HA_HOST", "HA_USER", "HA_CUSTOM_COMPONENTS_PATH")
foreach ($key in $requiredKeys) {
    if (-not $settings.ContainsKey($key) -or -not $settings[$key]) {
        throw "Missing required setting $key in $EnvironmentFile"
    }
}

$serverHost = $settings["HA_HOST"]
$serverUser = $settings["HA_USER"]
$customComponentsPath = $settings["HA_CUSTOM_COMPONENTS_PATH"].TrimEnd("/")

if ($serverHost -notmatch "^[A-Za-z0-9.-]+$") {
    throw "HA_HOST may contain only letters, numbers, dots, and hyphens."
}
if ($serverUser -notmatch "^[A-Za-z_][A-Za-z0-9._-]*$") {
    throw "HA_USER is not a valid SSH user name."
}
if (
    $customComponentsPath -notmatch "^/[A-Za-z0-9_./-]+$" -or
    -not $customComponentsPath.EndsWith("/custom_components")
) {
    throw "HA_CUSTOM_COMPONENTS_PATH must be an absolute path ending in /custom_components."
}

foreach ($tool in @("ssh", "scp")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "Required command is not available: $tool"
    }
}

$sourcePath = Join-Path $PSScriptRoot "custom_components\pluxee"
if (-not (Test-Path -LiteralPath $sourcePath -PathType Container)) {
    throw "Pluxee source directory not found: $sourcePath"
}

$deploymentId = "{0}-{1}" -f (Get-Date -Format "yyyyMMdd-HHmmss"), ([guid]::NewGuid().ToString("N").Substring(0, 8))
$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) "pluxee-ha-$deploymentId"
$stagedComponent = Join-Path $temporaryRoot "pluxee"
# Home Assistant scans hidden directories too; backups must live outside
# custom_components or their duplicate manifests can shadow the live component.
$remoteDeploymentRoot = "$customComponentsPath/../.pluxee-deployments"
# Keep staging beside the destination so installation uses a same-filesystem
# rename, including when Home Assistant's config is on a separate mount.
$remoteStagingRoot = "$customComponentsPath/.pluxee-staging-$deploymentId"
$remoteBackupPath = "$remoteDeploymentRoot/backup-$deploymentId"
$remoteTarget = "$serverUser@$serverHost"
$deployed = $false

try {
    New-Item -ItemType Directory -Path $stagedComponent | Out-Null
    foreach ($file in Get-ChildItem -LiteralPath $sourcePath -File -Recurse) {
        if ($file.FullName -match "[\\/]__pycache__[\\/]" -or $file.Extension -eq ".pyc") {
            continue
        }

        $relativePath = [IO.Path]::GetRelativePath($sourcePath, $file.FullName)
        $destination = Join-Path $stagedComponent $relativePath
        $destinationDirectory = Split-Path -Parent $destination
        if (-not (Test-Path -LiteralPath $destinationDirectory)) {
            New-Item -ItemType Directory -Path $destinationDirectory | Out-Null
        }
        Copy-Item -LiteralPath $file.FullName -Destination $destination
    }

    Invoke-ExternalCommand -Executable "ssh" -Arguments @(
        "-o", "BatchMode=yes",
        $remoteTarget,
        "test -d '$customComponentsPath' && mkdir -p -- '$remoteStagingRoot'"
    ) -FailureMessage "Unable to validate the remote custom_components directory"

    Invoke-ExternalCommand -Executable "scp" -Arguments @(
        "-r",
        $stagedComponent,
        "${remoteTarget}:$remoteStagingRoot/"
    ) -FailureMessage "Unable to upload the Pluxee integration"

    $installScript = @'
set -eu
custom_components=$1
staging_root=$2
backup_path=$3
destination="$custom_components/pluxee"
staged_component="$staging_root/pluxee"

test -d "$custom_components"
test -f "$staged_component/manifest.json"
test -f "$staged_component/__init__.py"
python3 -m json.tool "$staged_component/manifest.json" >/dev/null
mkdir -p -- "$(dirname -- "$backup_path")"

# Preserve backups made by earlier versions without exposing duplicate
# integration manifests to Home Assistant's component discovery.
for legacy_backup in "$custom_components"/.pluxee-backup-*; do
    [ -d "$legacy_backup" ] || continue
    test -f "$legacy_backup/manifest.json"
    legacy_target="$(dirname -- "$backup_path")/$(basename -- "$legacy_backup")"
    if [ -e "$legacy_target" ]; then
        printf 'Backup already exists at %s; refusing to overwrite it\n' "$legacy_target" >&2
        exit 1
    fi
    mv -- "$legacy_backup" "$legacy_target"
done

had_previous=false
if [ -e "$destination" ]; then
    mv -- "$destination" "$backup_path"
    had_previous=true
fi

if mv -- "$staged_component" "$destination"; then
    rmdir -- "$staging_root"
else
    if [ "$had_previous" = true ]; then
        mv -- "$backup_path" "$destination"
    fi
    exit 1
fi

printf 'Installed Pluxee at %s\n' "$destination"
if [ "$had_previous" = true ]; then
    printf 'Previous installation backed up at %s\n' "$backup_path"
fi
'@

    # Encode the script so PowerShell cannot convert its LF endings to CRLF while
    # streaming it to the Linux shell. A trailing CR made the final `fi` invalid.
    $installScriptLf = $installScript -replace "`r`n", "`n"
    $encodedInstallScript = [Convert]::ToBase64String(
        [Text.Encoding]::UTF8.GetBytes($installScriptLf)
    )
    $remoteInstallCommand = (
        "printf '%s' '$encodedInstallScript' | base64 -d | " +
        "sh -s -- '$customComponentsPath' '$remoteStagingRoot' '$remoteBackupPath'"
    )
    Invoke-ExternalCommand -Executable "ssh" -Arguments @(
        "-o", "BatchMode=yes",
        $remoteTarget,
        $remoteInstallCommand
    ) -FailureMessage "Unable to install the staged Pluxee integration"

    $deployed = $true
    Write-Host "Copy complete. Restart Home Assistant to load the new integration."
}
finally {
    $resolvedTemporaryRoot = [IO.Path]::GetFullPath($temporaryRoot)
    $resolvedSystemTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if ($resolvedTemporaryRoot.StartsWith($resolvedSystemTemp, [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $resolvedTemporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
    }

    if (-not $deployed) {
        & ssh -o BatchMode=yes $remoteTarget "rm -rf -- '$remoteStagingRoot'" 2>$null
    }
}
