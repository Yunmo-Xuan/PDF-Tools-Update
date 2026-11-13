; ============================================================================
;  PDF Toolkit - Inno Setup 安装脚本
;
;  需要 Inno Setup 6.3 或更新版本：
;  本文件是 UTF-8 编码且不带 BOM，Inno Setup 从 6.3 起才默认按 UTF-8 解析
;  （6.0~6.2 需要 BOM，否则中文会乱码）。
;
;  版本号不在这里手写：build.bat 会先调用 installer\make_version_info.py
;  从 core\constants.py 生成 version.iss（MyAppVersion / MyAppName /
;  MyAppPublisher / MyAppExeName），本脚本再 #include 进来。
;
;  编译方式（先跑 build.bat 生成 dist\）：
;      iscc installer\pdf-toolkit.iss
;  产物：dist\PDFToolkit-<版本>-Setup.exe
; ============================================================================

#define MyAppURL "https://github.com/Yunmo-Xuan/PDF-Tools-Update"
#include "version.iss"

[Setup]
; 这个 GUID 一旦发布就不能再改（改名会导致旧版无法被新版覆盖升级）。
AppId={{7C4B1E52-9A3D-4F18-B6E7-2D5A8C13F904}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppSupportURL={#MyAppURL}
VersionInfoVersion={#MyAppVersion}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; 默认值 auto 会在「检测到本机以前装过」时直接跳过选目录页，覆盖安装时就等于不让人挑位置。
; 写死 no：每次安装都给一页选安装位置（默认值仍是上一次/当前用户的目录），静默安装不受影响。
DisableDirPage=no
OutputDir=..\dist
OutputBaseFilename=PDFToolkit-{#MyAppVersion}-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 只放行真正的 64 位系统（x64compatible 也含 ARM64），32 位系统装上跑不起来，直接挡掉。
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=..\resources\pdf_icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
; 默认按当前用户安装，不强制要管理员权限；用户仍可在向导里选择"为所有用户安装"
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
; 十种语言和程序界面那十种一一对应，全部用 Inno Setup 7 自带的 .isl（英文是 Default.isl）。
; 每个 .isl 自己带 LanguageID，装到哪种系统的机器上就默认显示哪种语言；
; 认不出来时（比如系统语言是葡萄牙语）会退回列表里第一种，所以把简体中文排在最前面。
; 多选语言后向导左上角会出现语言下拉框；静默安装（/VERYSILENT）不受影响，仍按检测到的语言走。
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "chinesetraditional"; MessagesFile: "compiler:Languages\ChineseTraditional.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
Name: "french"; MessagesFile: "compiler:Languages\French.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "german"; MessagesFile: "compiler:Languages\German.isl"
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "italian"; MessagesFile: "compiler:Languages\Italian.isl"

; 自定义消息必须写在 [CustomMessages] 里（[Messages] 只用来覆盖 Inno 内置的那些），
; {cm:名字} 查的就是这一节；带语言名前缀的按语言覆盖不带前缀的那条底。
[CustomMessages]
; 「创建桌面快捷方式」这句每种 .isl 里都有现成的翻译（CreateDesktopIcon），直接 {cm:} 引用；
; 「附加任务：」这一组标题没有内置消息，得自己按语言给一份。
ExtraTasksGroupDesc=Additional tasks:
chinesesimplified.ExtraTasksGroupDesc=附加任务：
chinesetraditional.ExtraTasksGroupDesc=附加工作：
english.ExtraTasksGroupDesc=Additional tasks:
japanese.ExtraTasksGroupDesc=追加タスク：
korean.ExtraTasksGroupDesc=추가 작업:
french.ExtraTasksGroupDesc=Tâches supplémentaires :
russian.ExtraTasksGroupDesc=Дополнительные задачи:
german.ExtraTasksGroupDesc=Zusätzliche Aufgaben:
spanish.ExtraTasksGroupDesc=Tareas adicionales:
italian.ExtraTasksGroupDesc=Attività aggiuntive:
; 快捷方式上写的名字按向导里选的语言来（软件内的名字跟着界面语言，见 core\lang_*.py 的 app_title）。
; 装出来的目录名、exe 名、注册表里的 AppName 一律还是英文的 PDF Toolkit：
; 那三样是机器认的标识，换语言会牵动路径与卸载记录，只有摆在用户眼前的名字才跟着语言走。
; 卸载/运行那两条也各自给全句：Inno 的 {cm:} 参数里再套 {cm:} 不保险，与其嵌套不如各语言写死一条。
PTAppName=PDF Toolkit
chinesesimplified.PTAppName=PDF 工具箱
chinesetraditional.PTAppName=PDF 工具箱
english.PTAppName=PDF Toolkit
japanese.PTAppName=PDF ツールキット
korean.PTAppName=PDF 툴킷
french.PTAppName=Outils PDF
russian.PTAppName=Инструменты PDF
german.PTAppName=PDF-Werkzeuge
spanish.PTAppName=Herramientas PDF
italian.PTAppName=Strumenti PDF
PTUninstallShortcut=Uninstall PDF Toolkit
chinesesimplified.PTUninstallShortcut=卸载 PDF 工具箱
chinesetraditional.PTUninstallShortcut=解除安裝 PDF 工具箱
english.PTUninstallShortcut=Uninstall PDF Toolkit
japanese.PTUninstallShortcut=PDF ツールキット のアンインストール
korean.PTUninstallShortcut=PDF 툴킷 제거
french.PTUninstallShortcut=Désinstaller Outils PDF
russian.PTUninstallShortcut=Удалить Инструменты PDF
german.PTUninstallShortcut=PDF-Werkzeuge deinstallieren
spanish.PTUninstallShortcut=Desinstalar Herramientas PDF
italian.PTUninstallShortcut=Disinstalla Strumenti PDF
PTLaunchShortcut=Run PDF Toolkit
chinesesimplified.PTLaunchShortcut=运行 PDF 工具箱
chinesetraditional.PTLaunchShortcut=執行 PDF 工具箱
english.PTLaunchShortcut=Run PDF Toolkit
japanese.PTLaunchShortcut=PDF ツールキット を実行
korean.PTLaunchShortcut=PDF 툴킷 실행
french.PTLaunchShortcut=Lancer Outils PDF
russian.PTLaunchShortcut=Запустить Инструменты PDF
german.PTLaunchShortcut=PDF-Werkzeuge starten
spanish.PTLaunchShortcut=Ejecutar Herramientas PDF
italian.PTLaunchShortcut=Avvia Strumenti PDF

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:ExtraTasksGroupDesc}"; Flags: unchecked

[InstallDelete]
; 覆盖安装先把上一版的 _internal 整个删掉，再由 [Files] 解新的一份。
; 不这么做的话装几版就攒几版死文件：Inno 只管覆盖同名文件、不管删，而 PyInstaller 每版的依赖
; 集会变（本机实测抓到三份早就不在包里的：cryptography-49.0.0 / markupsafe-3.0.3 / numpy-2.5.4
; 的 .dist-info 连同包目录一直留在装机目录里）。删了重解只多花几秒，换来「装机目录 = 这次打出来的包」。
Type: filesandordirs; Name: "{app}\_internal"

[Files]
; 打包 PyInstaller 的 onedir 产物（build.bat 固定在 ..\dist）。
Source: "..\dist\PDFToolkit\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; 网页渲染组件（WebView2 运行时）的离线完整安装器（Evergreen Standalone Installer），微软数字签名，
; 大小 214999248 字节，SHA256=F9D45CF7BEF9576D6DF50C7A7525E0BC8C26B6C3BC2C667B494AAFB062A35B6F。
; 它自己就带运行时本体，不联网也能装上——随包就是为了「完全离线的机器一次装成功」。
; 下面那份在线 bootstrapper 本体里没有运行时，运行时要联网从微软服务器下载，离线机器必卡在那一步。
; 代价：包体从约 339 MB 涨到约 554 MB，仍在 1 GB 以内，值得。
; dontcopy：它不是程序文件，不落到 {app}，只在注册表判「缺运行时」时才解到 {tmp} 跑一次（见 _InstallWebView2Runtime）。
Source: "vendor\MicrosoftEdgeWebView2RuntimeInstallerX64.exe"; Flags: dontcopy
; 官方在线安装器（Evergreen Bootstrapper），微软数字签名，
; SHA256=90AE8EB0D9D22D6E51FCCDE49E4D25D266FF9343AD0CC931FAA6F482569CEA62。
; 留作兜底：只有离线完整安装器没装成时才再试它一次（离线机器上多半失败，尽力而为），
; 缺运行时的机器打开程序只会是一片空白，所以两条路都要走。
; dontcopy：同上不落 {app}，用到才解出来跑一次。
Source: "vendor\MicrosoftEdgeWebview2Setup.exe"; Flags: dontcopy

[Icons]
Name: "{group}\{cm:PTAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:PTUninstallShortcut}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{cm:PTAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:PTLaunchShortcut}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: dirifempty; Name: "{app}"
Type: files; Name: "{app}\install.stamp"
; 说明：用户配置在 %APPDATA%\PDFToolkit，日志在 %LOCALAPPDATA%\PDFToolkit\logs。
; 卸载时故意保留，方便重装后沿用设置；需要彻底清理请手动删除这两个目录。

[Code]
const
  // 在线 bootstrapper（兜底用）与离线完整安装器（首选），文件名必须与 [Files] 的 Source 一致。
  WV2SetupName = 'MicrosoftEdgeWebview2Setup.exe';
  WV2OfflineName = 'MicrosoftEdgeWebView2RuntimeInstallerX64.exe';

// 7.0.3 起安装目录改成纯英文（Paddle 的推理器读不了中文绝对路径，旧目录叫「PDF 工具箱」）。
// Inno 会把上次安装的位置预填进选目录页，老用户那一份还是中文路径：不拦下来，
// 覆盖安装（包括 /VERYSILENT）就又装回中文目录里，这次改名等于白做。
// InitializeWizard 时目录页已经填好，直接看那一栏；只认「带非 ASCII 字符的路径」，
// 用户自己挑的英文目录一律尊重，不动。
procedure InitializeWizard;
var
  Cur: String;
  I: Integer;
begin
  Cur := WizardForm.DirEdit.Text;
  for I := 1 to Length(Cur) do
    if Cur[I] > #127 then
    begin
      WizardForm.DirEdit.Text := ExpandConstant('{autopf}\{#MyAppName}');
      Break;
    end;
end;

// 与程序里的启动门禁同一份口径（core\webview2.py）：这四个 EdgeUpdate 通道键里
// 任何一个的 pv 达到内核版本下限，就算运行时存在。下限 86.0.622.0 是 pywebview
// 决定「走 WebView2 还是退回 IE」的那条线。两边写死同一组值，谁改了另一边就露出来。
// Pascal Script 不支持数组常量，所以按键号/段号各给一个取值函数。
function _WV2ClientKey(Index: Integer): String;
begin
  case Index of
    0: Result := '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
    1: Result := '{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}';
    2: Result := '{0D50BFEC-CD6A-4F9A-964C-C7416E3ACB10}';
  else
    Result := '{65C35B14-6C1D-4122-AC46-7148CC9D6497}';
  end;
end;

function _WV2MinSegment(Index: Integer): Integer;
begin
  case Index of
    0: Result := 86;
    1: Result := 0;
    2: Result := 622;
  else
    Result := 0;
  end;
end;

function _WV2Segment(Value: String; Index: Integer): Integer;
var
  Dot: Integer;
begin
  // pv 形如 140.0.3485.66：取第 Index 段（从 0 起）；段数不足或不是整数返回 -1
  Result := -1;
  while (Index > 0) and (Length(Value) > 0) do
  begin
    Dot := Pos('.', Value);
    if Dot = 0 then
      Value := ''
    else
      Value := Copy(Value, Dot + 1, Length(Value) - Dot);
    Index := Index - 1;
  end;
  if Length(Value) = 0 then
    Exit;
  Dot := Pos('.', Value);
  if Dot > 1 then
    Value := Copy(Value, 1, Dot - 1);
  Result := StrToIntDef(Value, -1);
end;

function _WV2BuildOk(Value: String): Boolean;
var
  Index, Segment, Minimum: Integer;
begin
  // 逐段与 86.0.622.0 比，先分出大小的一段说了算
  Result := True;
  for Index := 0 to 3 do
  begin
    Segment := _WV2Segment(Value, Index);
    Minimum := _WV2MinSegment(Index);
    if Segment < 0 then
    begin
      Result := False;
      Break;
    end;
    if Segment > Minimum then
      Break;
    if Segment < Minimum then
    begin
      Result := False;
      Break;
    end;
  end;
end;

function _WebView2Missing: Boolean;
var
  Index: Integer;
  Value: String;
  Base: String;
begin
  // HKCU 是「当前用户装的」，HKLM32 是 EdgeUpdate（32 位程序）写进全机器的那份，
  // HKLM64 兜住手工/离线安装落在 64 位视图里的情况。
  Result := True;
  for Index := 0 to 3 do
  begin
    // 结尾的反斜杠不能省：这是子键路径，少了它就拼成一个不存在的键，
    // 于是任何机器都会判成「缺运行时」，安装器会去起微软在线安装器并弹错误窗口。
    Base := 'Software\Microsoft\EdgeUpdate\Clients\' + _WV2ClientKey(Index);
    // RegQueryStringValue 失败时不会清掉 Value，所以每次都要先归零再查
    Value := '';
    if RegQueryStringValue(HKCU, Base, 'pv', Value) and _WV2BuildOk(Value) then
    begin
      Result := False;
      Exit;
    end;
    Value := '';
    if RegQueryStringValue(HKLM32, Base, 'pv', Value) and _WV2BuildOk(Value) then
    begin
      Result := False;
      Exit;
    end;
    Value := '';
    if RegQueryStringValue(HKLM64, Base, 'pv', Value) and _WV2BuildOk(Value) then
    begin
      Result := False;
      Exit;
    end;
  end;
end;

procedure _InstallWebView2Runtime;
var
  ResultCode: Integer;
  Runner: String;
  OfflineOk: Boolean;
begin
  if _WebView2Missing then
  begin
    // 第一步：随包的离线完整安装器（Evergreen Standalone Installer，205 MB，自带运行时本体）。
    // 装它不需要联网，「完全离线的机器一次装成功」靠的就是这一步。
    Log('未检测到 WebView2 运行时，静默安装随包的离线完整安装器');
    ExtractTemporaryFile(WV2OfflineName);
    Runner := ExpandConstant('{tmp}\' + WV2OfflineName);
    OfflineOk := False;
    if not FileExists(Runner) then
      Log('解包失败，安装器里没有 ' + WV2OfflineName)
    // 参数照微软文档原样写：standalone 安装器同样是 /silent /install；
    // /install 是动作，不给它就不干安装这件事；/norestart 不在它的参数表里，不自创。
    else if ShellExec('', Runner, '/silent /install', '', SW_HIDE,
        ewWaitUntilTerminated, ResultCode) then
    begin
      // 退出码判断：0 = 装上；3010 = 装上但需要重启才完全生效——运行时已经就位，也算成功。
      // 其余任何返回码（含负数，多为进程没起来/被拦）都判失败，走兜底。
      if (ResultCode = 0) or (ResultCode = 3010) then
      begin
        OfflineOk := True;
        Log('WebView2 离线完整安装器返回码 ' + IntToStr(ResultCode) + '，判为安装成功'
          + '（3010 表示需要重启后完全生效）')
      end
      else
        Log('WebView2 离线完整安装器返回码 ' + IntToStr(ResultCode) + '，未判为成功');
    end
    else
      Log('无法启动 WebView2 离线完整安装器');

    // 第二步（兜底）：离线那份没装成，才试原来的微软在线 bootstrapper。
    // 它本体不含运行时、要联网下载，离线机器上这一步多半也失败，但聊胜于无；
    // 两次尝试的返回码各自写进安装日志，事后能分清栽在哪一步。
    if not OfflineOk then
    begin
      Log('离线安装未成功，兜底尝试官方在线安装器（需要联网）');
      ExtractTemporaryFile(WV2SetupName);
      Runner := ExpandConstant('{tmp}\' + WV2SetupName);
      if not FileExists(Runner) then
        Log('解包失败，安装器里没有 ' + WV2SetupName)
      // 参数同微软文档：MicrosoftEdgeWebview2Setup.exe /silent /install，不自创 /norestart。
      else if ShellExec('', Runner, '/silent /install', '', SW_HIDE,
          ewWaitUntilTerminated, ResultCode) then
        Log('WebView2 在线安装器返回码 ' + IntToStr(ResultCode))
      else
        Log('无法启动 WebView2 在线安装器');
    end;

    if not _WebView2Missing then
      Log('WebView2 运行时已就绪')
    else if not WizardSilent then
      MsgBox('这台电脑上还没有 Microsoft Edge WebView2 运行时，安装它之后 PDF Toolkit才能显示界面。'
        + #13#10 + #13#10 + '随包的离线完整安装器和在线安装器这两步都没能装上它。'
        + '不影响程序文件的写入：装上运行时后再启动程序即可（程序启动时也会再提示一次）。',
        mbInformation, MB_OK);
  end
  else
    Log('WebView2 运行时已存在，跳过');
end;

// 每次安装（包括同一个版本反复覆盖安装）都在 exe 旁边留一个批次标记，
// 程序里的首次向导靠它判断「这一回装完还没走过向导」——只比版本号的话，
// 重装同一个版本就再也见不到向导了。便携版没有这个文件，向导退回只比版本号。
// 注意：[Code] 里是 Pascal 语法，注释只能用 //，写分号会让 ISCC 报 'BEGIN' expected。
procedure CurStepChanged(CurStep: TSetupStep);
var
  StampFile: String;
begin
  if CurStep = ssPostInstall then
  begin
    // 先补网页渲染组件，再写批次标记：[Run] 里的「立即运行」就在这之后，
    // 那时运行时已经装好，用户不会在第一启动就看到空界面。
    _InstallWebView2Runtime;
    StampFile := ExpandConstant('{app}\install.stamp');
    if not SaveStringToFile(StampFile,
        GetDateTimeString('yyyy"-"mm"-"dd" "hh":"nn":"ss', #0, #0), False) then
      Log('写入 install.stamp 失败: ' + StampFile);
  end;
end;
