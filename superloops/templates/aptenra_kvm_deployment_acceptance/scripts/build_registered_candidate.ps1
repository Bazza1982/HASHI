param(
    [Parameter(Mandatory = $true)][string]$TemplatePath,
    [Parameter(Mandatory = $true)][string]$CandidateId,
    [Parameter(Mandatory = $true)][string]$MsiVersion,
    [Parameter(Mandatory = $true)][string]$DisplayVersion,
    [Parameter(Mandatory = $true)][string]$BuildId,
    [Parameter(Mandatory = $true)][int]$BuildSequence,
    [Parameter(Mandatory = $true)][string]$ProductCode,
    [Parameter(Mandatory = $true)][string]$ProductRegistrationCommit,
    [Parameter(Mandatory = $true)][string]$ProductContentCommit,
    [Parameter(Mandatory = $true)][string]$PackagingCommit
)

$ErrorActionPreference = "Stop"
$source = Get-Content -LiteralPath $TemplatePath -Raw -Encoding UTF8
$replacements = [ordered]@{
    "apt-int-20260802-2243-094bc83a" = $CandidateId
    "0.2.74" = $MsiVersion
    "0.2.0-internal.5" = $DisplayVersion
    "apt-internal-0.2.0+20260802.094bc83a" = $BuildId
    "BuildSequence = 74" = "BuildSequence = $BuildSequence"
    "{024FB399-D019-46C7-80D4-BEA59E7BEF3E}" = $ProductCode
    "8e00a7e5961e68c32130d97ee4fc135043b64401" = $ProductRegistrationCommit
    "094bc83a89b2fcb67281922c97e39a9ae2a9ac5e" = $ProductContentCommit
    "854250e49aea54912d1de6510eeef36224451e3d" = $PackagingCommit
}
foreach ($entry in $replacements.GetEnumerator()) {
    $count = ([regex]::Matches($source, [regex]::Escape([string]$entry.Key))).Count
    if ($count -ne 1) {
        throw "Expected exactly one build-template match for: $($entry.Key); found $count"
    }
    $source = $source.Replace([string]$entry.Key, [string]$entry.Value)
}
$templateDirectory = Split-Path -Parent (Resolve-Path -LiteralPath $TemplatePath)
$source = $source.Replace('$PSScriptRoot', ("'" + $templateDirectory.Replace("'", "''") + "'"))
& ([scriptblock]::Create($source))
