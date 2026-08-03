param(
    [Parameter(Mandatory = $true)][string]$TemplatePath,
    [Parameter(Mandatory = $true)][string]$ProductBundleName,
    [Parameter(Mandatory = $true)][string]$ProductCommit
)

$ErrorActionPreference = "Stop"
$source = Get-Content -LiteralPath $TemplatePath -Raw -Encoding UTF8
$replacements = [ordered]@{
    "aptenra-main-1ca5d7cf.bundle" = $ProductBundleName
    "1ca5d7cf73fff08ba825e1e872256c978ede5d1b" = $ProductCommit
}
foreach ($entry in $replacements.GetEnumerator()) {
    $count = ([regex]::Matches($source, [regex]::Escape([string]$entry.Key))).Count
    if ($count -ne 1) {
        throw "Expected exactly one preparation-template match for: $($entry.Key); found $count"
    }
    $source = $source.Replace([string]$entry.Key, [string]$entry.Value)
}
& ([scriptblock]::Create($source))
