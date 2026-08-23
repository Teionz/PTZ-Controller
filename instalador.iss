; ============================================================================
;  Instalador profissional do Rastreador PTZ  (Inno Setup)
;  Gera um unico "RastreadorPTZ_Setup.exe":
;    - instala em Arquivos de Programas
;    - cria atalhos no Menu Iniciar e (opcional) na Area de Trabalho
;    - aparece em "Adicionar ou remover programas" com desinstalador
;  Use o 2-CRIAR-INSTALADOR.bat para compilar este script.
; ============================================================================

#define AppNome "Rastreador PTZ"
#define AppVersao "1.0.0"
#define AppPublisher "Protechti"
#define AppExe "RastreadorPTZ.exe"

[Setup]
AppId={{7C1F9E44-3B2A-4D56-9E8F-2A6B4C0D1E77}
AppName={#AppNome}
AppVersion={#AppVersao}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\RastreadorPTZ
DefaultGroupName={#AppNome}
DisableProgramGroupPage=yes
OutputDir=instalador_saida
OutputBaseFilename=RastreadorPTZ_Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppNome}
SetupIconFile=icone.ico

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "desktopicon"; Description: "Criar um atalho na Area de Trabalho"; GroupDescription: "Atalhos:"

[Files]
; Leva TUDO que o PyInstaller gerou dentro de dist\RastreadorPTZ
Source: "dist\RastreadorPTZ\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppNome}";              Filename: "{app}\{#AppExe}"
Name: "{group}\Desinstalar {#AppNome}";  Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppNome}";        Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Abrir o {#AppNome} agora"; Flags: nowait postinstall skipifsilent

; Ao desinstalar, remove os dados do usuario (estado.json, config.ini editado)
[UninstallDelete]
Type: filesandordirs; Name: "{userappdata}\RastreadorPTZ"
