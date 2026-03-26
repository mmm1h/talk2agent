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
继续开发这个项目，优化telegram bot的交互体验，提升用户的使用便捷性和满意度。通过改进消息处理逻辑、增加更多功能和优化界面设计，使得用户能够更高效地与Bot进行互动，并获得更好的使用体验。
'@

$continuePrompt = @'
继续开发这个项目，优化telegram bot的交互体验，提升用户的使用便捷性和满意度。通过改进消息处理逻辑、增加更多功能和优化界面设计，使得用户能够更高效地与Bot进行互动，并获得更好的使用体验。
'@

& $loopScript `
    -FirstPrompt $firstPrompt `
    -ContinuePrompt $continuePrompt `
    -WorkingDirectory $PSScriptRoot `
    -MaxRounds $MaxRounds `
    -DelaySeconds $DelaySeconds `
    -StopWhenMessageMatches $StopWhenMessageMatches `
    -ExtraCodexArgs $ExtraCodexArgs
