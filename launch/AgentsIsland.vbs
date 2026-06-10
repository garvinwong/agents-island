' Agents Island 静默启动器（开机自启可放入 shell:startup）
' 路径自动取自脚本位置；WSL 发行版可在同目录 distro.txt 指定（缺省=默认发行版）
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

launchDir = fso.GetParentFolderName(WScript.ScriptFullName)
appRoot   = fso.GetParentFolderName(launchDir)

distroArg = ""
distroFile = fso.BuildPath(launchDir, "distro.txt")
If fso.FileExists(distroFile) Then
    d = Trim(fso.OpenTextFile(distroFile).ReadLine)
    If Len(d) > 0 Then distroArg = " -d " & d
End If

' 1) 拉起 WSL 桥（幂等；用 wslpath 把 Windows 路径转 WSL 路径）
bridgeSh = fso.BuildPath(launchDir, "start_bridge.sh")
cmd = "wsl.exe" & distroArg & " -- bash -c ""bash \""$(wslpath '" & bridgeSh & "')\"""""
sh.Run cmd, 0, True

' 2) 启动 Windows 岛壳
islandPy = fso.BuildPath(fso.BuildPath(appRoot, "win"), "island.py")
sh.Run "pythonw.exe """ & islandPy & """", 0, False
