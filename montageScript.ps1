Param(
    [int]$ppage=-1,
    [int]$mpage=-1,
    [string[]]$exDir,
    [switch]$noPMontage,
    [switch]$noMMontage,
    [switch]$testMode,
    [switch]$noMove=$false
)

$numFilesPerFolder=24500
$size2BdiviBy="5100m"

#This version assumes to be working from a parent directory, so '-recurse' is assumed
Function workInDir{
    #Get files section. Just get them all
    $fileList = get-childitem -path .\ -file -name -exclude "*.db","*.xml","*.ini","*.7z*","*.mp4","*.mp4.*.jpg","*.gif","montage*"
    $mp4list=get-childitem -path .\ -File -Name *.mp4
    if (($fileList.length -eq 0) -and ($mp4list.length -eq 0)){
        Write-Host "Found no file in ",$PWD,". Moving on."
        return
    }
    if(!($noMMontage)){
        if($mp4list.length -gt 0){
            Write-Host "Working on mp4 files"
            foreach ($item in $mp4list){
                ffmpeg -i $item -vf 'fps=1,scale=200:-1,tile' "$item.%02d.jpg"
            }
        }
    }
    if(!($noPMontage)){
        if($fileList.length -gt 0){
            $perFile = 225
            $numOutFiles=[math]::floor($fileList.length/$perFile)+1

            Write-host 'numOutFiles is ',$numOutFiles

            $outFile=0
            if (!($ppage -eq -1)){$outFile=($ppage-1)}
            do{
                $tempArg=''
                $startLine=$outFile*$perFile
                Write-Host '$startLine is',$startLine,', page',($outFile+1),'out of',$numOutFiles,"|",(Get-Date -UFormat '%R'),"|",(Split-path -path $pwd -Leaf)
                $finishLine=(($outFile+1)*$perFile)
                if($finishLine -gt $fileList.length){$finishLine=$fileList.length}
                for ($line=$startLine;$line -lt $finishLine;$line++){
                    #$tempArg+=$fileList[$line]
                    $tempArg+=$fileList[$line]+'[300x300>] '
                }
                $outFileName='montage-'+($outFile+1)+'.jpg'
                Invoke-Expression -Command "magick montage $tempArg -mode concatenate -set label '%f' -tile 15x15 -background '#AB82FF' $outFileName"
                $outFile++
            }while ($outFile -lt $numOutFiles)
        }
    }
}
if ($testMode){
    Write-Host '$recurse is',$recurse#,$recurse.GetType()
    Write-Host '$exDir is',$exDir#,$exDir.GetType()
    Write-Host '$ppage is',$ppage#,$page.GetType()
    Write-Host '$mpage is',$mpage#,$page.GetType()
    return
}

$dirList = Get-ChildItem -Name -Directory -Path . -Depth 0
$workingFolder=$PWD
foreach ($dir in $dirList){
    if (!($exDir -eq $null)){
        if ($exDir -contains $dir){
            Write-Host "$dir is among excluded directories. Skipping..."
            continue
        }
    }
    if ((!($noPMontage)) -and (!($noMMontage))){
        Set-Location -Path $workingFolder\$dir;
        Write-Host "Working in ",$PWD;
        workInDir;
        Set-Location -Path $workingFolder;
    }else{
        Set-Location -Path $workingFolder\$dir;
        Write-Host "Working in ",$PWD;
        workInDir;
        Set-Location -Path $workingFolder;
    }
    Write-Host "Done making montages. Moving stuff..."
    if (!($noMove)){
        mkdir -Path "..\Montages\$dir\montages";
        Move-Item -Path ".\$dir\montage*" -Destination "..\Montages\$dir\montages\";
        Move-Item -Path ".\$dir\*.mp4*.jpg" -Destination "..\Montages\$dir\montages\";
    }
    Write-Host "Moved the montages. Now zipping files";
    #$temp = (get-childitem -Path ".\$dir" -file -name -exclude "*.db","*.xml","*.ini","*.7z*","*.mp4.*.jpg","*.gif","montage*" | ? Length -gt 5mb)
    $temp = (get-ChildItem -Path ".\$dir" -file | where {($_.Length -gt 5mb) -and (($_.Name -NotLike "*.db") -and ($_.Name -NotLike "*.xml") -and ($_.Name -NotLike "*.ini") -and ($_.Name -NotLike "*.7z*") -and ($_.Name -NotLike "*.mp4.*.jpg") -and ($_.Name -NotLike "montage*"))})
    if ($temp.length -eq 0){
        Write-Host "No files larger than 5mb. Zipping..."
        $numFiles = (Get-ChildItem -File -Name -Path ".\$dir" -exclude "*.db","*.xml","*.ini","*.7z*","*.mp4","*.mp4.*.jpg","*.gif","montage*").length
        if($numFiles -eq 0){
            Write-Host "No small files either. Skipping..."
        }else{
            7z a -t7z ".\$dir\$dir-$numFiles.7z" ".\$dir\*.*" -mx0 -mhe=on -p'Amaz0nSucks!' -x!'*.xml' -x!'*.db' -x!'*.ini' -x!'montage*' -x!'*.7z*' -sdel "-v$size2BdiviBy"
        }
    }else{
        Write-Host "There are files larger than 5mb. Moving temporarily"
        mkdir -Path ".\$dir\5mb";
        $temp | Move-Item -Destination ".\$dir\5mb\"
        $numLargeFiles = $temp.Length
        $numFiles = (Get-ChildItem -File -Name ".\$dir").length
        if($numFiles -eq 0){
            Write-Host "No small files either. Skipping..."
        }else{
            Write-Host "Zipping $numFiles files in $dir"
            7z a -t7z ".\$dir\$dir-$numFiles.7z" ".\$dir\*.*" -mx0 -mhe=on -p'Amaz0nSucks!' -x!'*.xml' -x!'*.db' -x!'*.ini' -x!'montage*' -x!'*.7z*' -sdel "-v$size2BdiviBy"
        }
        if ($numLargeFiles -gt $numFilesPerFolder){
            $largeFiles=(Get-ChildItem -File -Name ".\$dir\5mb\")
            $counter=0
            $subFolder=1
            mkdir -Path (".\$dir\{0:d2}" -f $subFolder)
            foreach ($file in $largeFiles){
                Move-Item -Path ".\$dir\5mb\$file" -Destination (".\$dir\{0:d2}" -f $subFolder)
                $counter++
                if ($counter -eq $numFilesPerFolder){
                    $subFolder++
                    mkdir -Path (".\$dir\{0:d2}" -f $subFolder)
                    $counter=0
                }
            }
        }
        Move-Item -Path ".\$dir\5mb\*" -Destination ".\$dir\";
        Remove-Item -Path ".\$dir\5mb";
    }
    if (!($noMove)){
        if(($noPMontage) -or ($noMMontage)){
            Move-Item -Path ".\$dir" -Destination "..\Go\";
        }else{
            Move-Item -Path ".\$dir" -Destination "..\Mont'ed\";
        }
    }
}
