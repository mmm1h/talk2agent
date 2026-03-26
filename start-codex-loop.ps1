[CmdletBinding()]
param(
    [int]$MaxRounds = 100,
    [int]$DelaySeconds = 0,
    [string]$StopWhenMessageMatches,
    [string[]]$ExtraCodexArgs = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$loopScript = 'C:\Users\12562\codex-loop.ps1'
if (-not (Test-Path -LiteralPath $loopScript -PathType Leaf)) {
    throw "Loop script not found: $loopScript"
}

$firstPrompt = @'
请继续开发，并以产品思维持续打磨 Telegram Bot 的交互体验，使其达到用户级产品而非 Demo 的标准，直到你判断已无需进一步优化为止。
'@

$continuePrompt = @'
请继续开发，并以产品思维持续打磨 Telegram Bot 的交互体验，使其达到用户级产品而非 Demo 的标准，直到你判断已无需进一步优化为止。
'@

& $loopScript `
    -FirstPrompt $firstPrompt `
    -ContinuePrompt $continuePrompt `
    -WorkingDirectory $PSScriptRoot `
    -MaxRounds $MaxRounds `
    -DelaySeconds $DelaySeconds `
    -StopWhenMessageMatches $StopWhenMessageMatches `
    -ExtraCodexArgs $ExtraCodexArgs
