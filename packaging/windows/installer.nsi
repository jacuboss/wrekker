!define APP_NAME "Wrekker"
!define APP_VERSION "${VERSION}"
!define APP_PUBLISHER "Wrekker"
!define APP_URL "https://github.com/wrekker-dj/wrekker"

Name "${APP_NAME} ${APP_VERSION}"
OutFile "Wrekker-Setup-${APP_VERSION}-x64.exe"
InstallDir "$PROGRAMFILES64\Wrekker"
RequestExecutionLevel admin
SetCompressor /SOLID lzma

Page directory
Page instfiles

Section "Wrekker (required)"
  SectionIn RO
  SetOutPath "$INSTDIR"
  File /r "dist\wrekker-windows\*"
  IfFileExists "$INSTDIR\vcredist_x64.exe" 0 +2
    ExecWait '"$INSTDIR\vcredist_x64.exe" /quiet /norestart'
  CreateShortCut "$DESKTOP\Wrekker.lnk" "$INSTDIR\wrekker.exe"
  CreateDirectory "$SMPROGRAMS\Wrekker"
  CreateShortCut "$SMPROGRAMS\Wrekker\Wrekker.lnk" "$INSTDIR\wrekker.exe"
  CreateShortCut "$SMPROGRAMS\Wrekker\Uninstall.lnk" "$INSTDIR\Uninstall.exe"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Wrekker" "DisplayName" "Wrekker"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Wrekker" "UninstallString" "$INSTDIR\Uninstall.exe"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Wrekker" "DisplayVersion" "${APP_VERSION}"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
SectionEnd

Section "Uninstall"
  RMDir /r "$INSTDIR"
  Delete "$DESKTOP\Wrekker.lnk"
  RMDir /r "$SMPROGRAMS\Wrekker"
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Wrekker"
SectionEnd
