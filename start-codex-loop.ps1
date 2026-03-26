[CmdletBinding()]
param(
    [int]$MaxRounds = 100,
    [int]$DelaySeconds = 0,
    [string]$StopWhenMessageMatches,
    [string[]]$ExtraCodexArgs = @()
)

$PromptTemplate = "读取最近三次github提交记录,并以产品思维持续打磨 Telegram Bot 的交互体验，使其达到用户级产品而非 Demo 的标准，直到你判断已无需进一步优化为止。"

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function ConvertFrom-JsonLine {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Line
    )

    try {
        return $Line | ConvertFrom-Json -Depth 20
    }
    catch {
        return $null
    }
}

function Get-AgentMessageText {
    param(
        [Parameter()]
        [object]$Item
    )

    if ($null -eq $Item) {
        return ''
    }

    if ($Item.PSObject.Properties.Name -contains 'text' -and $null -ne $Item.text) {
        return [string]$Item.text
    }

    if (-not ($Item.PSObject.Properties.Name -contains 'content') -or $null -eq $Item.content) {
        return ''
    }

    $parts = foreach ($part in @($Item.content)) {
        if ($null -eq $part) {
            continue
        }

        if ($part.PSObject.Properties.Name -contains 'text' -and $null -ne $part.text) {
            [string]$part.text
            continue
        }

        if ($part.PSObject.Properties.Name -contains 'content' -and $null -ne $part.content) {
            [string]$part.content
        }
    }

    return ($parts -join "`n").Trim()
}

function Render-PromptTemplate {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Template,

        [Parameter(Mandatory = $true)]
        [int]$Round,

        [Parameter()]
        [string]$ThreadId,

        [Parameter()]
        [string]$LastMessage
    )

    $rendered = $Template
    $rendered = $rendered.Replace('{{round}}', [string]$Round)
    $rendered = $rendered.Replace('{{thread_id}}', [string]$ThreadId)
    $rendered = $rendered.Replace('{{last_message}}', [string]$LastMessage)
    return $rendered
}

function Invoke-CodexTurn {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Prompt,

        [Parameter(Mandatory = $true)]
        [string]$WorkingDirectory,

        [Parameter()]
        [string[]]$ExtraCodexArgs = @(),

        [Parameter(Mandatory = $true)]
        [int]$Round
    )

    $command = @('codex', 'exec', '--json', '--skip-git-repo-check')
    if (-not ($ExtraCodexArgs -contains '--yolo')) {
        $command += '--yolo'
    }

    if ($ExtraCodexArgs.Count -gt 0) {
        $command += $ExtraCodexArgs
    }

    $command += $Prompt

    $capturedThreadId = ''
    $lastMessage = ''

    Push-Location $WorkingDirectory
    try {
        Write-Host "========== Round $Round =========="
        Write-Host '[loop] start new session'

        & rtk @command 2>&1 | ForEach-Object {
            $line = $_.ToString()
            $event = ConvertFrom-JsonLine -Line $line

            if ($null -eq $event) {
                Write-Host $line
                return
            }

            switch ($event.type) {
                'thread.started' {
                    if ($event.PSObject.Properties.Name -contains 'thread_id') {
                        $capturedThreadId = [string]$event.thread_id
                        if ($capturedThreadId) {
                            Write-Host "[loop] thread $capturedThreadId"
                        }
                    }
                }
                'item.completed' {
                    if ($event.PSObject.Properties.Name -contains 'item' -and $null -ne $event.item) {
                        $item = $event.item
                        if ($item.PSObject.Properties.Name -contains 'type' -and $item.type -eq 'agent_message') {
                            $message = Get-AgentMessageText -Item $item
                            if ($message) {
                                $lastMessage = $message
                                Write-Host $message
                            }
                        }
                    }
                }
                'turn.completed' {
                    Write-Host '[loop] turn completed'
                }
            }
        }

        $exitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }

    [pscustomobject]@{
        ExitCode    = $exitCode
        ThreadId    = $capturedThreadId
        LastMessage = $lastMessage
    }
}

$workingDirectory = $PSScriptRoot
if (-not (Test-Path -LiteralPath $workingDirectory -PathType Container)) {
    throw "WorkingDirectory does not exist: $workingDirectory"
}

if ($MaxRounds -lt 0) {
    throw 'MaxRounds must be 0 or a positive integer.'
}

if ($DelaySeconds -lt 0) {
    throw 'DelaySeconds must be 0 or a positive integer.'
}

$round = 1
$lastThreadId = ''
$lastMessage = ''

while ($true) {
    $prompt = Render-PromptTemplate `
        -Template $PromptTemplate `
        -Round $round `
        -ThreadId $lastThreadId `
        -LastMessage $lastMessage

    $result = Invoke-CodexTurn `
        -Prompt $prompt `
        -WorkingDirectory $workingDirectory `
        -ExtraCodexArgs $ExtraCodexArgs `
        -Round $round

    if ($result.ExitCode -ne 0) {
        throw "Codex exited with code $($result.ExitCode) on round $round."
    }

    $lastThreadId = $result.ThreadId
    $lastMessage = $result.LastMessage

    if ($StopWhenMessageMatches -and $lastMessage -match $StopWhenMessageMatches) {
        Write-Host "[loop] stop condition matched: $StopWhenMessageMatches"
        break
    }

    if ($MaxRounds -gt 0 -and $round -ge $MaxRounds) {
        Write-Host "[loop] reached MaxRounds=$MaxRounds"
        break
    }

    if ($DelaySeconds -gt 0) {
        Write-Host "[loop] sleep $DelaySeconds second(s)"
        Start-Sleep -Seconds $DelaySeconds
    }

    $round++
}

Write-Host '[loop] finished'
