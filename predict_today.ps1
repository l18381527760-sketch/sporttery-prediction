param(
    [string]$Date = (Get-Date -Format "yyyy-MM-dd"),
    [switch]$NoFiles
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$DataDir = Join-Path $ProjectRoot "data"
$OutputDir = Join-Path $ProjectRoot "output"

function To-Double($Value, [double]$Default = 0) {
    if ($null -eq $Value -or [string]::IsNullOrWhiteSpace([string]$Value)) {
        return $Default
    }
    return [double]$Value
}

function To-OptionalDouble($Value) {
    if ($null -eq $Value -or [string]::IsNullOrWhiteSpace([string]$Value)) {
        return $null
    }
    return [double]$Value
}

function Clamp([double]$Value, [double]$Low, [double]$High) {
    return [Math]::Max($Low, [Math]::Min($High, $Value))
}

function Factorial([int]$N) {
    $Result = 1.0
    for ($I = 2; $I -le $N; $I++) {
        $Result *= $I
    }
    return $Result
}

function Get-PoissonPmf([double]$Lambda, [int]$MaxGoals) {
    $Values = @()
    $Total = 0.0
    for ($K = 0; $K -le $MaxGoals; $K++) {
        $Value = [Math]::Exp(-$Lambda) * [Math]::Pow($Lambda, $K) / (Factorial $K)
        $Values += $Value
        $Total += $Value
    }
    return $Values | ForEach-Object { $_ / $Total }
}

function Get-ExpectedGoals($A, $B, $Fixture, $Config) {
    $EloTerm = (($A.elo - $B.elo) / 400.0) * $Config.elo_goal_weight
    $RestTerm = (Clamp ($A.rest_days - $B.rest_days) -3.0 3.0) * $Config.rest_weight
    $HomeA = 0.0
    $HomeB = 0.0
    if (-not $Fixture.neutral) {
        $HomeA = $A.home_adv * $Config.home_adv_weight
        $HomeB = $B.home_adv * $Config.home_adv_weight
    }

    $ALog = [Math]::Log($Config.base_goals) +
        $EloTerm +
        $A.attack * $Config.attack_weight -
        $B.defense * $Config.defense_weight +
        ($A.form - $B.form) * $Config.form_weight +
        $A.injury * $Config.injury_weight +
        $RestTerm +
        $HomeA

    $BLog = [Math]::Log($Config.base_goals) -
        $EloTerm +
        $B.attack * $Config.attack_weight -
        $A.defense * $Config.defense_weight +
        ($B.form - $A.form) * $Config.form_weight +
        $B.injury * $Config.injury_weight -
        $RestTerm +
        $HomeB

    return @{
        a = Clamp ([Math]::Exp($ALog)) 0.15 4.5
        b = Clamp ([Math]::Exp($BLog)) 0.15 4.5
    }
}

function Get-ScoreDistribution([double]$LambdaA, [double]$LambdaB, [int]$MaxGoals) {
    $DistA = @(Get-PoissonPmf $LambdaA $MaxGoals)
    $DistB = @(Get-PoissonPmf $LambdaB $MaxGoals)
    $PA = 0.0
    $PDraw = 0.0
    $PB = 0.0
    $Scores = @()

    for ($GA = 0; $GA -le $MaxGoals; $GA++) {
        for ($GB = 0; $GB -le $MaxGoals; $GB++) {
            $P = $DistA[$GA] * $DistB[$GB]
            if ($GA -gt $GB) {
                $PA += $P
            } elseif ($GA -eq $GB) {
                $PDraw += $P
            } else {
                $PB += $P
            }
            $Scores += [pscustomobject]@{ a = $GA; b = $GB; p = $P }
        }
    }

    return @{
        p_a = $PA
        p_draw = $PDraw
        p_b = $PB
        top_scores = @($Scores | Sort-Object p -Descending | Select-Object -First 5)
    }
}

function Get-MarketProbabilities($Fixture) {
    if ($null -eq $Fixture.odds_a -or $null -eq $Fixture.odds_draw -or $null -eq $Fixture.odds_b) {
        return $null
    }
    $A = 1.0 / $Fixture.odds_a
    $D = 1.0 / $Fixture.odds_draw
    $B = 1.0 / $Fixture.odds_b
    $Total = $A + $D + $B
    return @{ a = $A / $Total; draw = $D / $Total; b = $B / $Total }
}

function Blend-Probabilities($Model, $Market, [double]$Weight) {
    if ($null -eq $Market) {
        return $Model
    }
    $A = (1 - $Weight) * $Model.a + $Weight * $Market.a
    $D = (1 - $Weight) * $Model.draw + $Weight * $Market.draw
    $B = (1 - $Weight) * $Model.b + $Weight * $Market.b
    $Total = $A + $D + $B
    return @{ a = $A / $Total; draw = $D / $Total; b = $B / $Total }
}

function Get-Advancement($PA, $PDraw, $PB, $A, $B) {
    $StrengthA = 1.0 / (1.0 + [Math]::Pow(10, -(($A.elo - $B.elo) / 400.0)))
    $TieShareA = 0.58 * $StrengthA + 0.42 * 0.5
    return @{ a = $PA + $PDraw * $TieShareA; b = $PB + $PDraw * (1 - $TieShareA) }
}

function Format-Percent([double]$Value) {
    return "{0:N1}%" -f ($Value * 100)
}

function Get-Confidence([double]$BestProbability, $Config) {
    if ($BestProbability -ge $Config.confidence_thresholds.high) {
        return "High"
    }
    if ($BestProbability -ge $Config.confidence_thresholds.medium) {
        return "Medium"
    }
    return "Low"
}

$TargetDate = [datetime]::ParseExact($Date, "yyyy-MM-dd", $null).Date
$Config = Get-Content (Join-Path $ProjectRoot "config.json") -Raw -Encoding UTF8 | ConvertFrom-Json

$Ratings = @{}
Import-Csv (Join-Path $DataDir "team_ratings.csv") | ForEach-Object {
    $Ratings[$_.team.Trim()] = [pscustomobject]@{
        team = $_.team.Trim()
        elo = To-Double $_.elo
        attack = To-Double $_.attack
        defense = To-Double $_.defense
        form = To-Double $_.form
        injury = To-Double $_.injury
        rest_days = To-Double $_.rest_days 3.0
        home_adv = To-Double $_.home_adv
    }
}

$Fixtures = Import-Csv (Join-Path $DataDir "fixtures.csv") | ForEach-Object {
    [pscustomobject]@{
        match_date = [datetime]::ParseExact($_.date.Trim(), "yyyy-MM-dd", $null).Date
        kickoff = $_.kickoff_local.Trim()
        stage = $_.stage.Trim().ToLowerInvariant()
        team_a = $_.team_a.Trim()
        team_b = $_.team_b.Trim()
        neutral = $_.neutral.Trim().ToLowerInvariant() -in @("true", "1", "yes", "y")
        venue = $_.venue.Trim()
        odds_a = To-OptionalDouble $_.odds_a
        odds_draw = To-OptionalDouble $_.odds_draw
        odds_b = To-OptionalDouble $_.odds_b
    }
} | Where-Object { $_.match_date -eq $TargetDate }

$KnockoutStages = @($Config.knockout_stages)
$Predictions = @()

foreach ($Fixture in $Fixtures) {
    if (-not $Ratings.ContainsKey($Fixture.team_a) -or -not $Ratings.ContainsKey($Fixture.team_b)) {
        throw "Missing team rating: $($Fixture.team_a) or $($Fixture.team_b)"
    }

    $A = $Ratings[$Fixture.team_a]
    $B = $Ratings[$Fixture.team_b]
    $Xg = Get-ExpectedGoals $A $B $Fixture $Config
    $Dist = Get-ScoreDistribution $Xg.a $Xg.b ([int]$Config.max_goals)
    $Model = @{ a = $Dist.p_a; draw = $Dist.p_draw; b = $Dist.p_b }
    $Market = Get-MarketProbabilities $Fixture
    $Prob = Blend-Probabilities $Model $Market ([double]$Config.market_blend_weight)

    $AdvA = $null
    $AdvB = $null
    $Pick = $Fixture.team_a
    $BestProbability = $Prob.a

    if ($Prob.draw -gt $BestProbability) {
        $Pick = "Draw"
        $BestProbability = $Prob.draw
    }
    if ($Prob.b -gt $BestProbability) {
        $Pick = $Fixture.team_b
        $BestProbability = $Prob.b
    }

    if ($KnockoutStages -contains $Fixture.stage) {
        $Adv = Get-Advancement $Prob.a $Prob.draw $Prob.b $A $B
        $AdvA = $Adv.a
        $AdvB = $Adv.b
        if ($AdvA -ge $AdvB) {
            $Pick = $Fixture.team_a
            $BestProbability = $AdvA
        } else {
            $Pick = $Fixture.team_b
            $BestProbability = $AdvB
        }
    }

    $Predictions += [pscustomobject]@{
        date = $TargetDate.ToString("yyyy-MM-dd")
        kickoff = $Fixture.kickoff
        stage = $Fixture.stage
        venue = $Fixture.venue
        team_a = $Fixture.team_a
        team_b = $Fixture.team_b
        xg_a = $Xg.a
        xg_b = $Xg.b
        p_a = $Prob.a
        p_draw = $Prob.draw
        p_b = $Prob.b
        adv_a = $AdvA
        adv_b = $AdvB
        pick = $Pick
        confidence = Get-Confidence $BestProbability $Config
        top_scores = $Dist.top_scores
    }
}

$Lines = @("# $($TargetDate.ToString("yyyy-MM-dd")) World Cup Predictions", "")
if ($Predictions.Count -eq 0) {
    $Lines += "No matches found for this date in data/fixtures.csv."
} else {
    foreach ($Item in $Predictions) {
        $Lines += "## $($Item.kickoff) $($Item.team_a) vs $($Item.team_b)"
        $Lines += "- Stage: $($Item.stage); Venue: $($Item.venue)"
        $Lines += "- Expected goals: $($Item.team_a) $("{0:N2}" -f $Item.xg_a), $($Item.team_b) $("{0:N2}" -f $Item.xg_b)"
        $Lines += "- 90-minute probabilities: $($Item.team_a) win $(Format-Percent $Item.p_a), draw $(Format-Percent $Item.p_draw), $($Item.team_b) win $(Format-Percent $Item.p_b)"
        if ($null -ne $Item.adv_a) {
            $Lines += "- Advancement probabilities: $($Item.team_a) $(Format-Percent $Item.adv_a), $($Item.team_b) $(Format-Percent $Item.adv_b)"
        }
        $ScoreText = (($Item.top_scores | ForEach-Object { "$($_.a)-$($_.b) $(Format-Percent $_.p)" }) -join "; ")
        $Lines += "- Most likely scores: $ScoreText"
        $Lines += "- Pick: $($Item.pick); Confidence: $($Item.confidence)"
        $Lines += ""
    }
}

$Report = $Lines -join [Environment]::NewLine
Write-Output $Report

if (-not $NoFiles) {
    if (-not (Test-Path $OutputDir)) {
        New-Item -ItemType Directory -Path $OutputDir | Out-Null
    }
    $MdPath = Join-Path $OutputDir "predictions_$($TargetDate.ToString("yyyy-MM-dd")).md"
    $CsvPath = Join-Path $OutputDir "predictions_$($TargetDate.ToString("yyyy-MM-dd")).csv"
    Set-Content -Path $MdPath -Value $Report -Encoding UTF8
    $Predictions |
        Select-Object date, kickoff, stage, team_a, team_b, xg_a, xg_b, p_a, p_draw, p_b, adv_a, adv_b, pick, confidence |
        Export-Csv -Path $CsvPath -NoTypeInformation -Encoding UTF8
    Write-Output "Generated: $MdPath"
    Write-Output "Generated: $CsvPath"
}
