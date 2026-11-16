# Offline TTS helper. Engines: sapi (classic SpVoice, 1 Chinese voice) / winrt (OneCore: Huihui|Yaoyao|Kangkang).
#
# HARD RULE: this file must stay PURE ASCII (zero bytes above 0x7F).
# PowerShell 5.1 on this machine reads BOM-less UTF-8 source as GBK (codepage 936). Non-ASCII
# bytes then decode to mojibake that can *swallow the following line break*, merging the next
# line into the comment. Measured 2026-10: 3 lines vanished, the "$esc = ..." assignment became
# part of a comment, and the script kept running while silently synthesizing EMPTY audio
# (a valid 46-byte WAV header). No error was raised. make_audiobook.py now asserts ASCII purity
# before invoking this file.
param(
  [Parameter(Mandatory = $true)][string]$Manifest
)
$ErrorActionPreference = 'Stop'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$MIN_WAV = 4096   # a real synthesis result is far larger; smaller means an empty/header-only WAV

$m = [System.IO.File]::ReadAllText($Manifest, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
$results = New-Object System.Collections.ArrayList

function Add-Result($id, $ok, $chars, $bytes, $err) {
  $o = [ordered]@{ id = $id; ok = $ok; chars = $chars; bytes = $bytes }
  if ($err) { $o.error = $err }
  [void]$results.Add([pscustomobject]$o)
}

function Describe-Err($e) {
  # AggregateException hides the real cause in InnerException; unwrap it or the report says
  # only "One or more errors occurred".
  $msgs = @()
  $cur = $e
  for ($i = 0; $i -lt 4 -and $cur; $i++) {
    $msgs += ('[' + $cur.GetType().Name + '] ' + $cur.Message)
    if ($cur.InnerException) { $cur = $cur.InnerException } else { break }
  }
  return ($msgs -join ' <- ')
}

if ($m.engine -eq 'sapi') {
  Add-Type -AssemblyName System.Speech
  $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
  try { $synth.SelectVoice($m.voice) } catch { throw ('SelectVoice failed: ' + $_.Exception.Message) }
  $synth.Rate = [int]$m.rate
  if ($null -ne $m.volume) { $synth.Volume = [int]$m.volume }
  $fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(22050,
    [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
    [System.Speech.AudioFormat.AudioChannel]::Mono)
  foreach ($it in $m.items) {
    $text = [System.IO.File]::ReadAllText($it.text_file, [System.Text.Encoding]::UTF8)
    try {
      $synth.SetOutputToWaveFile($it.out_wav, $fmt)
      $synth.Speak($text)
      $synth.SetOutputToNull()
      $written = (Get-Item $it.out_wav).Length
      if ($written -lt $MIN_WAV) {
        Add-Result $it.id $false $text.Length $written ('empty synthesis: written=' + $written)
      }
      else {
        Add-Result $it.id $true $text.Length $written $null
      }
    }
    catch {
      try { $synth.SetOutputToNull() } catch { }
      Add-Result $it.id $false $text.Length 0 (Describe-Err $_.Exception)
    }
  }
  $synth.Dispose()
}
elseif ($m.engine -eq 'winrt') {
  [void][Windows.Media.SpeechSynthesis.SpeechSynthesizer, Windows.Media, ContentType = WindowsRuntime]
  [void][Windows.Media.SpeechSynthesis.SpeechSynthesisStream, Windows.Media, ContentType = WindowsRuntime]
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  $asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
      $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
      $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
  $voice = [Windows.Media.SpeechSynthesis.SpeechSynthesizer]::AllVoices |
    Where-Object { $_.Id -like ('*' + $m.voice + '*') } | Select-Object -First 1
  if (-not $voice) { throw ('winrt voice not found: ' + $m.voice) }
  foreach ($it in $m.items) {
    $text = [System.IO.File]::ReadAllText($it.text_file, [System.Text.Encoding]::UTF8)
    try {
      $s = New-Object Windows.Media.SpeechSynthesis.SpeechSynthesizer
      $s.Voice = $voice
      if ($m.rate_ssml) {
        # Measured: an attribute-less <prosody> tag makes OneCore throw FormatException, so the
        # rate must always be present when SSML is used; otherwise use the plain-text API.
        $esc = [System.Security.SecurityElement]::Escape($text)
        $ssml = '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="zh-CN"><prosody rate="' + $m.rate_ssml + '">' + $esc + '</prosody></speak>'
        $op = $s.SynthesizeSsmlToStreamAsync($ssml)
      }
      else {
        $op = $s.SynthesizeTextToStreamAsync($text)
      }
      $t = $asTask.MakeGenericMethod([Windows.Media.SpeechSynthesis.SpeechSynthesisStream]).Invoke($null, @($op))
      if (-not $t.Wait(180000)) { throw 'winrt synthesis timeout' }
      $streamBytes = [int64]$t.Result.Size
      $net = [System.IO.WindowsRuntimeStreamExtensions]::AsStreamForRead($t.Result)
      $fs = [System.IO.File]::Create($it.out_wav)
      $net.CopyTo($fs)
      $fs.Close()
      $written = (Get-Item $it.out_wav).Length
      if ($written -lt $MIN_WAV) {
        Add-Result $it.id $false $text.Length $written ('empty synthesis: stream=' + $streamBytes + ' written=' + $written)
      }
      else {
        Add-Result $it.id $true $text.Length $written $null
      }
    }
    catch { Add-Result $it.id $false $text.Length 0 (Describe-Err $_.Exception) }
  }
}
else { throw ('unknown engine: ' + $m.engine) }

$payload = [ordered]@{
  engine  = $m.engine
  voice   = $m.voice
  results = @($results)
}
[System.IO.File]::WriteAllText($m.result_file, ($payload | ConvertTo-Json -Depth 5), $utf8NoBom)
'HELPER_DONE ok=' + @($results | Where-Object { $_.ok }).Count + '/' + $results.Count
