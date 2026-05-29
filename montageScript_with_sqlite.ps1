Param(
    [int]$ppage = -1,
    [int]$mpage = -1,
    [string[]]$exDir,
    [switch]$noPMontage,
    [switch]$noMMontage,
    [switch]$testMode,
    [switch]$noMove = $false,
    [string]$RootPath,
    [string]$SQLiteDbPath,
    [switch]$NoHash,
    [switch]$NoVideoProbe
)

$numFilesPerFolder = 24500
$size2BdiviBy = "5100m"
$script:SQLiteReady = $false

Function Assert-CommandExists {
    Param([Parameter(Mandatory=$true)][string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' was not found in PATH. Please install it or add it to PATH."
    }
}

Function Quote-SqlText {
    Param([AllowNull()][object]$Value)
    if ($null -eq $Value) { return "NULL" }
    $text = [string]$Value
    return "'" + $text.Replace("'", "''") + "'"
}

Function Quote-SqlInt {
    Param([AllowNull()][object]$Value)
    if ($null -eq $Value) { return "NULL" }
    return [string]([int64]$Value)
}

Function Quote-SqlBool {
    Param([AllowNull()][object]$Value)
    if ($null -eq $Value) { return "NULL" }
    if ([bool]$Value) { return "1" }
    return "0"
}

Function Invoke-SqliteNonQuery {
    Param([Parameter(Mandatory=$true)][string]$Sql)
    $tempSql = [System.IO.Path]::GetTempFileName()
    try {
        Set-Content -LiteralPath $tempSql -Value $Sql -Encoding UTF8
        & sqlite3 $script:SQLiteDbPath ".read '$tempSql'"
        if ($LASTEXITCODE -ne 0) {
            throw "sqlite3 failed while running SQL from $tempSql"
        }
    }
    finally {
        Remove-Item -LiteralPath $tempSql -Force -ErrorAction SilentlyContinue
    }
}

Function Initialize-SqliteDb {
    Param([Parameter(Mandatory=$true)][string]$DbPath)

    Assert-CommandExists -Name "sqlite3"
    $script:SQLiteDbPath = [System.IO.Path]::GetFullPath($DbPath)
    $dbFolder = Split-Path -Parent $script:SQLiteDbPath
    if ($dbFolder -and -not (Test-Path -LiteralPath $dbFolder)) {
        New-Item -ItemType Directory -Path $dbFolder -Force | Out-Null
    }

    $schema = @"
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS file_properties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_time_utc TEXT NOT NULL,
    root_path TEXT,
    directory_name TEXT,
    name TEXT,
    full_name TEXT UNIQUE,
    extension TEXT,
    base_name TEXT,
    length_bytes INTEGER,
    creation_time_utc TEXT,
    last_access_time_utc TEXT,
    last_write_time_utc TEXT,
    attributes TEXT,
    is_read_only INTEGER,
    sha256 TEXT,
    mime_guess TEXT,
    ffprobe_json TEXT,
    powershell_properties_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_file_properties_directory_name ON file_properties(directory_name);
CREATE INDEX IF NOT EXISTS idx_file_properties_extension ON file_properties(extension);
CREATE INDEX IF NOT EXISTS idx_file_properties_scan_time_utc ON file_properties(scan_time_utc);
"@
    Invoke-SqliteNonQuery -Sql $schema
    $script:SQLiteReady = $true
}

Function Get-VideoProbeJson {
    Param([Parameter(Mandatory=$true)][string]$Path)
    if ($NoVideoProbe) { return $null }
    if (-not (Get-Command ffprobe -ErrorAction SilentlyContinue)) { return $null }

    $json = & ffprobe -v quiet -print_format json -show_format -show_streams -- "$Path" 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $json) { return $null }
    return ($json -join "`n")
}

Function Save-FilePropertiesToSQLite {
    Param(
        [Parameter(Mandatory=$true)][System.IO.FileInfo]$File,
        [Parameter(Mandatory=$true)][string]$Root
    )

    if (-not $script:SQLiteReady) { return }

    $sha256 = $null
    if (-not $NoHash) {
        try { $sha256 = (Get-FileHash -LiteralPath $File.FullName -Algorithm SHA256).Hash }
        catch { $sha256 = $null }
    }

    $ffprobeJson = $null
    if ($File.Extension -match '(?i)^\.(mp4|m4v|mov|mkv|avi|wmv|webm|flv|mpg|mpeg|ts)$') {
        $ffprobeJson = Get-VideoProbeJson -Path $File.FullName
    }

    $psPropsJson = $null
    try {
        $psPropsJson = $File | Select-Object * | ConvertTo-Json -Depth 5 -Compress
    }
    catch {
        $psPropsJson = $null
    }

    $mimeGuess = $null
    switch -Regex ($File.Extension.ToLowerInvariant()) {
        '^\.mp4$' { $mimeGuess = 'video/mp4'; break }
        '^\.m4v$' { $mimeGuess = 'video/x-m4v'; break }
        '^\.mov$' { $mimeGuess = 'video/quicktime'; break }
        '^\.mkv$' { $mimeGuess = 'video/x-matroska'; break }
        '^\.jpg|\.jpeg$' { $mimeGuess = 'image/jpeg'; break }
        '^\.png$' { $mimeGuess = 'image/png'; break }
        '^\.gif$' { $mimeGuess = 'image/gif'; break }
        default { $mimeGuess = $null }
    }

    $sql = @"
INSERT INTO file_properties (
    scan_time_utc, root_path, directory_name, name, full_name, extension, base_name,
    length_bytes, creation_time_utc, last_access_time_utc, last_write_time_utc,
    attributes, is_read_only, sha256, mime_guess, ffprobe_json, powershell_properties_json
) VALUES (
    $(Quote-SqlText ((Get-Date).ToUniversalTime().ToString('o'))),
    $(Quote-SqlText $Root),
    $(Quote-SqlText $File.DirectoryName),
    $(Quote-SqlText $File.Name),
    $(Quote-SqlText $File.FullName),
    $(Quote-SqlText $File.Extension),
    $(Quote-SqlText $File.BaseName),
    $(Quote-SqlInt $File.Length),
    $(Quote-SqlText $File.CreationTimeUtc.ToString('o')),
    $(Quote-SqlText $File.LastAccessTimeUtc.ToString('o')),
    $(Quote-SqlText $File.LastWriteTimeUtc.ToString('o')),
    $(Quote-SqlText $File.Attributes),
    $(Quote-SqlBool $File.IsReadOnly),
    $(Quote-SqlText $sha256),
    $(Quote-SqlText $mimeGuess),
    $(Quote-SqlText $ffprobeJson),
    $(Quote-SqlText $psPropsJson)
)
ON CONFLICT(full_name) DO UPDATE SET
    scan_time_utc=excluded.scan_time_utc,
    root_path=excluded.root_path,
    directory_name=excluded.directory_name,
    name=excluded.name,
    extension=excluded.extension,
    base_name=excluded.base_name,
    length_bytes=excluded.length_bytes,
    creation_time_utc=excluded.creation_time_utc,
    last_access_time_utc=excluded.last_access_time_utc,
    last_write_time_utc=excluded.last_write_time_utc,
    attributes=excluded.attributes,
    is_read_only=excluded.is_read_only,
    sha256=excluded.sha256,
    mime_guess=excluded.mime_guess,
    ffprobe_json=excluded.ffprobe_json,
    powershell_properties_json=excluded.powershell_properties_json;
"@
    Invoke-SqliteNonQuery -Sql $sql
}

Function Save-CurrentDirectoryFilesToSQLite {
    Param([Parameter(Mandatory=$true)][string]$Root)
    if (-not $script:SQLiteReady) { return }

    $files = Get-ChildItem -Path .\ -File -Force -ErrorAction SilentlyContinue
    if ($files.Count -eq 0) { return }

    Write-Host "Recording file properties to SQLite for $($files.Count) files in $PWD"
    foreach ($file in $files) {
        Save-FilePropertiesToSQLite -File $file -Root $Root
    }
}

function Invoke-FFmpeg {
    param(
        [Parameter(Mandatory)]
        [string[]]$Arguments,

        [string]$FFmpegPath = "ffmpeg"
    )

    $output = & $FFmpegPath @Arguments 2>&1
    $exitCode = $LASTEXITCODE

    if ($exitCode -ne 0) {
        throw "ffmpeg failed with exit code $exitCode.`n$output"
    }

    return $output
}

# This version assumes work starts from a parent directory, so child folders are processed.
Function workInDir {
    Save-CurrentDirectoryFilesToSQLite -Root $script:RootPathResolved

    # Get files section. Just get them all.
    $fileList = Get-ChildItem -Path .\ -File -Name -Exclude "*.db","*.xml","*.ini","*.7z*","*.mp4","*.mp4.*.jpg","*.gif","montage*"
    $mp4list = Get-ChildItem -Path .\ -File -Name *.mp4

    if (($fileList.Length -eq 0) -and ($mp4list.Length -eq 0)) {
        Write-Host "Found no file in $PWD. Moving on."
        return
    }

    if (-not $noMMontage) {
        if ($mp4list.Length -gt 0) {
            Assert-CommandExists -Name "ffmpeg"
            Write-Host "Working on mp4 files"
            foreach ($item in $mp4list) {
                Invoke-FFmpeg -Arguments @(
                "-y",
                "-i", $item,
                "-vf", "fps=1,scale=200:-1,tile",
                "-frames:v", "1",
                "$item.%02d.jpg"
            )
            #     ffmpeg -i $item -vf 'fps=1,scale=200:-1,tile' "$item.%02d.jpg"
            # }
        }
    }

    if (-not $noPMontage) {
        if ($fileList.Length -gt 0) {
            Assert-CommandExists -Name "magick"
            $perFile = 225
            $numOutFiles = [math]::Floor($fileList.Length / $perFile) + 1

            Write-Host 'numOutFiles is ' $numOutFiles

            $outFile = 0
            if (-not ($ppage -eq -1)) { $outFile = ($ppage - 1) }
            do {
                $tempArg = ''
                $startLine = $outFile * $perFile
                Write-Host '$startLine is' $startLine ', page' ($outFile + 1) 'out of' $numOutFiles '|' (Get-Date -UFormat '%R') '|' (Split-Path -Path $pwd -Leaf)
                $finishLine = (($outFile + 1) * $perFile)
                if ($finishLine -gt $fileList.Length) { $finishLine = $fileList.Length }
                for ($line = $startLine; $line -lt $finishLine; $line++) {
                    $tempArg += $fileList[$line] + '[300x300>] '
                }
                $outFileName = 'montage-' + ($outFile + 1) + '.jpg'
                Invoke-Expression -Command "magick montage $tempArg -mode concatenate -set label '%f' -tile 15x15 -background '#AB82FF' $outFileName"
                $outFile++
            } while ($outFile -lt $numOutFiles)
        }
    }
}

if (-not $RootPath) {
    $RootPath = Read-Host "Enter the parent folder to process. Press Enter to use the current folder"
    if ([string]::IsNullOrWhiteSpace($RootPath)) { $RootPath = (Get-Location).Path }
}

$script:RootPathResolved = [System.IO.Path]::GetFullPath($RootPath)
if (-not (Test-Path -LiteralPath $script:RootPathResolved -PathType Container)) {
    throw "RootPath does not exist or is not a folder: $script:RootPathResolved"
}

if (-not $SQLiteDbPath) {
    $defaultDb = Join-Path $script:RootPathResolved "file_properties.sqlite"
    $SQLiteDbPath = Read-Host "Enter SQLite database path. Press Enter to use $defaultDb"
    if ([string]::IsNullOrWhiteSpace($SQLiteDbPath)) { $SQLiteDbPath = $defaultDb }
}

Initialize-SqliteDb -DbPath $SQLiteDbPath

if ($testMode) {
    Write-Host '$exDir is' $exDir
    Write-Host '$ppage is' $ppage
    Write-Host '$mpage is' $mpage
    Write-Host '$RootPath is' $script:RootPathResolved
    Write-Host '$SQLiteDbPath is' $script:SQLiteDbPath
    return
}

Set-Location -LiteralPath $script:RootPathResolved
$dirList = Get-ChildItem -Name -Directory -Path . -Depth 0
$workingFolder = (Get-Location).Path

foreach ($dir in $dirList) {
    if ($null -ne $exDir) {
        if ($exDir -contains $dir) {
            Write-Host "$dir is among excluded directories. Skipping..."
            continue
        }
    }

    Set-Location -LiteralPath (Join-Path $workingFolder $dir)
    Write-Host "Working in $PWD"
    workInDir
    Set-Location -LiteralPath $workingFolder

    Write-Host "Done making montages. Moving stuff..."
    if (-not $noMove) {
        New-Item -ItemType Directory -Path "..\Montages\$dir\montages" -Force | Out-Null
        Move-Item -Path ".\$dir\montage*" -Destination "..\Montages\$dir\montages\" -ErrorAction SilentlyContinue
        Move-Item -Path ".\$dir\*.mp4*.jpg" -Destination "..\Montages\$dir\montages\" -ErrorAction SilentlyContinue
    }

    Write-Host "Moved the montages. Now zipping files"
    $temp = Get-ChildItem -Path ".\$dir" -File | Where-Object {
        ($_.Length -gt 5mb) -and
        ($_.Name -NotLike "*.db") -and
        ($_.Name -NotLike "*.xml") -and
        ($_.Name -NotLike "*.ini") -and
        ($_.Name -NotLike "*.7z*") -and
        ($_.Name -NotLike "*.mp4.*.jpg") -and
        ($_.Name -NotLike "montage*")
    }

    if ($temp.Length -eq 0) {
        Write-Host "No files larger than 5mb. Zipping..."
        $numFiles = (Get-ChildItem -File -Name -Path ".\$dir" -Exclude "*.db","*.xml","*.ini","*.7z*","*.mp4","*.mp4.*.jpg","*.gif","montage*").Length
        if ($numFiles -eq 0) {
            Write-Host "No small files either. Skipping..."
        }
        else {
            Assert-CommandExists -Name "7z"
            7z a -t7z ".\$dir\$dir-$numFiles.7z" ".\$dir\*.*" -mx0 -mhe=on -p'Amaz0nSucks!' -x!'*.xml' -x!'*.db' -x!'*.ini' -x!'montage*' -x!'*.7z*' -sdel "-v$size2BdiviBy"
        }
    }
    else {
        Write-Host "There are files larger than 5mb. Moving temporarily"
        New-Item -ItemType Directory -Path ".\$dir\5mb" -Force | Out-Null
        $temp | Move-Item -Destination ".\$dir\5mb\"
        $numLargeFiles = $temp.Length
        $numFiles = (Get-ChildItem -File -Name ".\$dir").Length
        if ($numFiles -eq 0) {
            Write-Host "No small files either. Skipping..."
        }
        else {
            Write-Host "Zipping $numFiles files in $dir"
            Assert-CommandExists -Name "7z"
            7z a -t7z ".\$dir\$dir-$numFiles.7z" ".\$dir\*.*" -mx0 -mhe=on -p'Amaz0nSucks!' -x!'*.xml' -x!'*.db' -x!'*.ini' -x!'montage*' -x!'*.7z*' -sdel "-v$size2BdiviBy"
        }
        if ($numLargeFiles -gt $numFilesPerFolder) {
            $largeFiles = Get-ChildItem -File -Name ".\$dir\5mb\"
            $counter = 0
            $subFolder = 1
            New-Item -ItemType Directory -Path (".\$dir\{0:d2}" -f $subFolder) -Force | Out-Null
            foreach ($file in $largeFiles) {
                Move-Item -Path ".\$dir\5mb\$file" -Destination (".\$dir\{0:d2}" -f $subFolder)
                $counter++
                if ($counter -eq $numFilesPerFolder) {
                    $subFolder++
                    New-Item -ItemType Directory -Path (".\$dir\{0:d2}" -f $subFolder) -Force | Out-Null
                    $counter = 0
                }
            }
        }
        Move-Item -Path ".\$dir\5mb\*" -Destination ".\$dir\" -ErrorAction SilentlyContinue
        Remove-Item -Path ".\$dir\5mb" -ErrorAction SilentlyContinue
    }

    if (-not $noMove) {
        if (($noPMontage) -or ($noMMontage)) {
            Move-Item -Path ".\$dir" -Destination "..\Go\"
        }
        else {
            Move-Item -Path ".\$dir" -Destination "..\Mont'ed\"
        }
    }
}
$') {
        $ffprobeJson = Get-VideoProbeJson -Path $File.FullName
    }

    $psPropsJson = $null
    try {
        $psPropsJson = $File | Select-Object * | ConvertTo-Json -Depth 5 -Compress
    }
    catch {
        $psPropsJson = $null
    }

    $mimeGuess = $null
    switch -Regex ($File.Extension.ToLowerInvariant()) {
        '^\.mp4$' { $mimeGuess = 'video/mp4'; break }
        '^\.m4v$' { $mimeGuess = 'video/x-m4v'; break }
        '^\.mov$' { $mimeGuess = 'video/quicktime'; break }
        '^\.mkv$' { $mimeGuess = 'video/x-matroska'; break }
        '^\.jpg|\.jpeg$' { $mimeGuess = 'image/jpeg'; break }
        '^\.png$' { $mimeGuess = 'image/png'; break }
        '^\.gif$' { $mimeGuess = 'image/gif'; break }
        default { $mimeGuess = $null }
    }

    $sql = @"
INSERT INTO file_properties (
    scan_time_utc, root_path, directory_name, name, full_name, extension, base_name,
    length_bytes, creation_time_utc, last_access_time_utc, last_write_time_utc,
    attributes, is_read_only, sha256, mime_guess, ffprobe_json, powershell_properties_json
) VALUES (
    $(Quote-SqlText ((Get-Date).ToUniversalTime().ToString('o'))),
    $(Quote-SqlText $Root),
    $(Quote-SqlText $File.DirectoryName),
    $(Quote-SqlText $File.Name),
    $(Quote-SqlText $File.FullName),
    $(Quote-SqlText $File.Extension),
    $(Quote-SqlText $File.BaseName),
    $(Quote-SqlInt $File.Length),
    $(Quote-SqlText $File.CreationTimeUtc.ToString('o')),
    $(Quote-SqlText $File.LastAccessTimeUtc.ToString('o')),
    $(Quote-SqlText $File.LastWriteTimeUtc.ToString('o')),
    $(Quote-SqlText $File.Attributes),
    $(Quote-SqlBool $File.IsReadOnly),
    $(Quote-SqlText $sha256),
    $(Quote-SqlText $mimeGuess),
    $(Quote-SqlText $ffprobeJson),
    $(Quote-SqlText $psPropsJson)
)
ON CONFLICT(full_name) DO UPDATE SET
    scan_time_utc=excluded.scan_time_utc,
    root_path=excluded.root_path,
    directory_name=excluded.directory_name,
    name=excluded.name,
    extension=excluded.extension,
    base_name=excluded.base_name,
    length_bytes=excluded.length_bytes,
    creation_time_utc=excluded.creation_time_utc,
    last_access_time_utc=excluded.last_access_time_utc,
    last_write_time_utc=excluded.last_write_time_utc,
    attributes=excluded.attributes,
    is_read_only=excluded.is_read_only,
    sha256=excluded.sha256,
    mime_guess=excluded.mime_guess,
    ffprobe_json=excluded.ffprobe_json,
    powershell_properties_json=excluded.powershell_properties_json;
"@
    Invoke-SqliteNonQuery -Sql $sql
}

Function Save-CurrentDirectoryFilesToSQLite {
    Param([Parameter(Mandatory=$true)][string]$Root)
    if (-not $script:SQLiteReady) { return }

    $files = Get-ChildItem -Path .\ -File -Force -ErrorAction SilentlyContinue
    if ($files.Count -eq 0) { return }

    Write-Host "Recording file properties to SQLite for $($files.Count) files in $PWD"
    foreach ($file in $files) {
        Save-FilePropertiesToSQLite -File $file -Root $Root
    }
}

# This version assumes work starts from a parent directory, so child folders are processed.
Function workInDir {
    Save-CurrentDirectoryFilesToSQLite -Root $script:RootPathResolved

    # Get files section. Just get them all.
    $fileList = Get-ChildItem -Path .\ -File -Name -Exclude "*.db","*.xml","*.ini","*.7z*","*.mp4","*.mp4.*.jpg","*.gif","montage*"
    $mp4list = Get-ChildItem -Path .\ -File -Name *.mp4

    if (($fileList.Length -eq 0) -and ($mp4list.Length -eq 0)) {
        Write-Host "Found no file in $PWD. Moving on."
        return
    }

    if (-not $noMMontage) {
        if ($mp4list.Length -gt 0) {
            Assert-CommandExists -Name "ffmpeg"
            Write-Host "Working on mp4 files"
            foreach ($item in $mp4list) {
                ffmpeg -i $item -vf 'fps=1,scale=200:-1,tile' "$item.%02d.jpg"
            }
        }
    }

    if (-not $noPMontage) {
        if ($fileList.Length -gt 0) {
            Assert-CommandExists -Name "magick"
            $perFile = 225
            $numOutFiles = [math]::Floor($fileList.Length / $perFile) + 1

            Write-Host 'numOutFiles is ' $numOutFiles

            $outFile = 0
            if (-not ($ppage -eq -1)) { $outFile = ($ppage - 1) }
            do {
                $tempArg = ''
                $startLine = $outFile * $perFile
                Write-Host '$startLine is' $startLine ', page' ($outFile + 1) 'out of' $numOutFiles '|' (Get-Date -UFormat '%R') '|' (Split-Path -Path $pwd -Leaf)
                $finishLine = (($outFile + 1) * $perFile)
                if ($finishLine -gt $fileList.Length) { $finishLine = $fileList.Length }
                for ($line = $startLine; $line -lt $finishLine; $line++) {
                    $tempArg += $fileList[$line] + '[300x300>] '
                }
                $outFileName = 'montage-' + ($outFile + 1) + '.jpg'
                Invoke-Expression -Command "magick montage $tempArg -mode concatenate -set label '%f' -tile 15x15 -background '#AB82FF' $outFileName"
                $outFile++
            } while ($outFile -lt $numOutFiles)
        }
    }
}

if (-not $RootPath) {
    $RootPath = Read-Host "Enter the parent folder to process. Press Enter to use the current folder"
    if ([string]::IsNullOrWhiteSpace($RootPath)) { $RootPath = (Get-Location).Path }
}

$script:RootPathResolved = [System.IO.Path]::GetFullPath($RootPath)
if (-not (Test-Path -LiteralPath $script:RootPathResolved -PathType Container)) {
    throw "RootPath does not exist or is not a folder: $script:RootPathResolved"
}

if (-not $SQLiteDbPath) {
    $defaultDb = Join-Path $script:RootPathResolved "file_properties.sqlite"
    $SQLiteDbPath = Read-Host "Enter SQLite database path. Press Enter to use $defaultDb"
    if ([string]::IsNullOrWhiteSpace($SQLiteDbPath)) { $SQLiteDbPath = $defaultDb }
}

Initialize-SqliteDb -DbPath $SQLiteDbPath

if ($testMode) {
    Write-Host '$exDir is' $exDir
    Write-Host '$ppage is' $ppage
    Write-Host '$mpage is' $mpage
    Write-Host '$RootPath is' $script:RootPathResolved
    Write-Host '$SQLiteDbPath is' $script:SQLiteDbPath
    return
}

Set-Location -LiteralPath $script:RootPathResolved
$dirList = Get-ChildItem -Name -Directory -Path . -Depth 0
$workingFolder = (Get-Location).Path

foreach ($dir in $dirList) {
    if ($null -ne $exDir) {
        if ($exDir -contains $dir) {
            Write-Host "$dir is among excluded directories. Skipping..."
            continue
        }
    }

    Set-Location -LiteralPath (Join-Path $workingFolder $dir)
    Write-Host "Working in $PWD"
    workInDir
    Set-Location -LiteralPath $workingFolder

    Write-Host "Done making montages. Moving stuff..."
    if (-not $noMove) {
        New-Item -ItemType Directory -Path "..\Montages\$dir\montages" -Force | Out-Null
        Move-Item -Path ".\$dir\montage*" -Destination "..\Montages\$dir\montages\" -ErrorAction SilentlyContinue
        Move-Item -Path ".\$dir\*.mp4*.jpg" -Destination "..\Montages\$dir\montages\" -ErrorAction SilentlyContinue
    }

    Write-Host "Moved the montages. Now zipping files"
    $temp = Get-ChildItem -Path ".\$dir" -File | Where-Object {
        ($_.Length -gt 5mb) -and
        ($_.Name -NotLike "*.db") -and
        ($_.Name -NotLike "*.xml") -and
        ($_.Name -NotLike "*.ini") -and
        ($_.Name -NotLike "*.7z*") -and
        ($_.Name -NotLike "*.mp4.*.jpg") -and
        ($_.Name -NotLike "montage*")
    }

    if ($temp.Length -eq 0) {
        Write-Host "No files larger than 5mb. Zipping..."
        $numFiles = (Get-ChildItem -File -Name -Path ".\$dir" -Exclude "*.db","*.xml","*.ini","*.7z*","*.mp4","*.mp4.*.jpg","*.gif","montage*").Length
        if ($numFiles -eq 0) {
            Write-Host "No small files either. Skipping..."
        }
        else {
            Assert-CommandExists -Name "7z"
            7z a -t7z ".\$dir\$dir-$numFiles.7z" ".\$dir\*.*" -mx0 -mhe=on -p'Amaz0nSucks!' -x!'*.xml' -x!'*.db' -x!'*.ini' -x!'montage*' -x!'*.7z*' -sdel "-v$size2BdiviBy"
        }
    }
    else {
        Write-Host "There are files larger than 5mb. Moving temporarily"
        New-Item -ItemType Directory -Path ".\$dir\5mb" -Force | Out-Null
        $temp | Move-Item -Destination ".\$dir\5mb\"
        $numLargeFiles = $temp.Length
        $numFiles = (Get-ChildItem -File -Name ".\$dir").Length
        if ($numFiles -eq 0) {
            Write-Host "No small files either. Skipping..."
        }
        else {
            Write-Host "Zipping $numFiles files in $dir"
            Assert-CommandExists -Name "7z"
            7z a -t7z ".\$dir\$dir-$numFiles.7z" ".\$dir\*.*" -mx0 -mhe=on -p'Amaz0nSucks!' -x!'*.xml' -x!'*.db' -x!'*.ini' -x!'montage*' -x!'*.7z*' -sdel "-v$size2BdiviBy"
        }
        if ($numLargeFiles -gt $numFilesPerFolder) {
            $largeFiles = Get-ChildItem -File -Name ".\$dir\5mb\"
            $counter = 0
            $subFolder = 1
            New-Item -ItemType Directory -Path (".\$dir\{0:d2}" -f $subFolder) -Force | Out-Null
            foreach ($file in $largeFiles) {
                Move-Item -Path ".\$dir\5mb\$file" -Destination (".\$dir\{0:d2}" -f $subFolder)
                $counter++
                if ($counter -eq $numFilesPerFolder) {
                    $subFolder++
                    New-Item -ItemType Directory -Path (".\$dir\{0:d2}" -f $subFolder) -Force | Out-Null
                    $counter = 0
                }
            }
        }
        Move-Item -Path ".\$dir\5mb\*" -Destination ".\$dir\" -ErrorAction SilentlyContinue
        Remove-Item -Path ".\$dir\5mb" -ErrorAction SilentlyContinue
    }

    if (-not $noMove) {
        if (($noPMontage) -or ($noMMontage)) {
            Move-Item -Path ".\$dir" -Destination "..\Go\"
        }
        else {
            Move-Item -Path ".\$dir" -Destination "..\Mont'ed\"
        }
    }
}
