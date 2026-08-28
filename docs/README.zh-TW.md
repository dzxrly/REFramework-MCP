# REFramework-MCP

[English](../README.md) | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-TW.md)

REFramework-MCP 透過
[REFramework](https://github.com/praydog/REFramework) 將 MCP 用戶端連接至
執行中的 RE Engine 遊戲。它可以索引 REFramework 中繼資料與現有 Lua MOD、
查詢型別和成員、建立存取路徑，並透過行程內 Bridge 執行有邊界的即時探針。

1.0.0 版適配官方 REF Nightly
nightly-01397-684ca77369ec1050e844e8651a9b1d5b7c5aa370。REF Nightly 是本專案
唯一使用的上游發行管道。Python Host 是主程式；C++ 只負責遊戲行程內 Bridge，
以及兩個小型、版本化的 REFramework Service ABI。

詳細設計請見 [C2 技術架構 v1.0.0](C2-Architecture-v1.0.0.zh-CN.md)。

## 組成

- Python MCP Host：中繼資料索引、MOD 用法索引、四圖融合搜尋、AccessPlan
  產生、權限原則、核准與稽核。
- REFramework-MCP Bridge：Windows Named Pipe 外掛，負責持有不透明
  ObjectRef，並把執行階段工作排程到遊戲執行緒。
- Export Service：為 REFramework 增加非同步 JSON 或 SDK 加 JSON 匯出，
  run_generate_sdk 由此實作。
- Probe Service：編譯並執行隔離且受資源限制的 Lua Probe。

伺服器不使用原始位址作為物件身分。執行階段物件會以短期 ObjectRef 回傳，並
綁定至單一 runtime epoch。

## 環境需求

- Windows 10 或 11
- 受支援的 RE Engine 遊戲

原始碼建置還需要：

- Python 3.11 至 3.13
- Visual Studio 2022，安裝「使用 C++ 的桌面開發」
- CMake 3.25 或更新版本
- 遞迴複製且位於已驗證 REF Nightly 原始碼提交的 REFramework

## 安裝

若使用封裝版本，只需下載
reframework-mcp-1.0.0-ref-nightly-01397-windows-x64.zip。解壓縮後，將 game
目錄中的內容複製至實際遊戲目錄；這會安裝已對齊的 dinput8.dll 與
reframework/plugins/reframework_mcp.dll。若之後需要還原其他 REFramework
建置，請先備份遊戲目錄中現有的 dinput8.dll。

接著雙擊 REFramework-MCP.exe。它會開啟主控台輸出記錄，並在
http://127.0.0.1:8765/mcp 提供 MCP；關閉視窗或按 Ctrl+C 即停止伺服器。
封裝版不要求使用者安裝 Python，也不需要 GUI 或設定檔。source-adapter 僅供
稽核與原始碼建置，不需複製至遊戲目錄。

以下步驟說明原始碼安裝方式。

### 1. 安裝 Python Host

在本專案目錄執行：

~~~powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
~~~

預設不需要設定檔：使用本機 stdio、Named Pipe
\\.\pipe\reframework-mcp-v1，資料寫入
%LOCALAPPDATA%\REFramework-MCP。只有在需要修改儲存、傳輸、權限原則或
MOD 目錄時，才將 config.example.toml 複製為 config.toml。

### 2. 將 Export 與 Probe Service 加入 REFramework

請使用位於已驗證 REF Nightly 提交的乾淨儲存庫。一般安裝指令碼會拒絕其他
提交。帶守衛的跨 Nightly 模式只供完整的 REF Nightly Alignment 工作流程使用。

~~~powershell
git clone --recursive https://github.com/praydog/REFramework.git C:\Code\REFramework
git -C C:\Code\REFramework checkout 684ca77369ec1050e844e8651a9b1d5b7c5aa370
git -C C:\Code\REFramework submodule update --init --recursive

.\reframework\export_service\install.ps1 -REFrameworkRoot C:\Code\REFramework
cmake -S C:\Code\REFramework -B C:\Code\REFramework\build -G "Visual Studio 17 2022" -A x64
cmake --build C:\Code\REFramework\build --config Release --target REFramework
~~~

依一般 REFramework 安裝方式，將
C:\Code\REFramework\build\bin\REFramework\dinput8.dll 複製到遊戲目錄。

### 3. 建置並安裝 Bridge 外掛

~~~powershell
cmake -S . -B out\build -G "Visual Studio 17 2022" -A x64 -DREFRAMEWORK_ROOT=C:\Code\REFramework
cmake --build out\build --config Release --target reframework_mcp
cmake --install out\build --config Release --prefix "C:\Games\YourGame"
~~~

最後一條命令會把 reframework_mcp.dll 安裝至所選遊戲目錄下的
reframework\plugins。

發行壓縮檔只由 GitHub Actions 建置。本專案不把手動組裝或提交至儲存庫的套件
當作發行來源。

## 連接 MCP 用戶端

封裝版雙擊 EXE 即以本機 HTTP 模式啟動：

~~~powershell
.\REFramework-MCP.exe
~~~

啟動遊戲後，可在終端機檢查 Bridge：

~~~powershell
.\REFramework-MCP.exe doctor
~~~

若 MCP 用戶端負責啟動 stdio 行程，讓它指向同一個 EXE 並傳入 serve。請使用
絕對路徑：

~~~json
{
  "mcpServers": {
    "reframework": {
      "command": "C:\\Tools\\REFramework-MCP\\REFramework-MCP.exe",
      "args": ["serve"]
    }
  }
}
~~~

明確傳入命令列參數時保留一般 CLI 語意；serve 預設使用 stdio。也可明確選擇
本機 HTTP：

~~~powershell
.\REFramework-MCP.exe serve --transport streamable-http --host 127.0.0.1 --port 8765
~~~

封裝版會自動載入 EXE 同目錄下選用的 config.toml。
不要把 HTTP 端點暴露至本機之外。執行階段工具能讀取遊戲狀態，並可在核准後
修改遊戲狀態。

## 建議工作流程

1. 呼叫 runtime_status，檢查 Bridge、遊戲、TDB 指紋、runtime epoch、
   權限原則及作用中的中繼資料快照。
2. 以 json_only 模式呼叫 run_generate_sdk。輪詢回傳的匯出 Resource，直到
   快照完成索引並啟用。
3. 使用 CLI 索引具代表性的 Lua MOD，再呼叫 search_members 與
   search_usage_examples。精確簽章、多載、參數及回傳型別、已知 MOD 鏈路和
   即時觀測會一同參與排序。
4. 使用 find_access_paths 產生 AccessPlan 候選，再透過
   validate_access_plan 逐節點驗證所選方案。
5. 依序使用 draft_lua_probe、validate_lua_probe 與 run_lua_probe 進行
   有邊界的執行階段探索。
6. 使用觀測 Hook 補充動態關係。invoke_method、set_field、轉換 Hook 與
   windowed Hook 測試需要原則核准；方法呼叫與欄位寫入還要求目前 runtime
   epoch 下通過即時驗證的 AccessPlan。

run_generate_sdk 是非同步操作。json_only 已足以支援搜尋和圖建構；
sdk_and_json 還會寫出一般 REFramework SDK。policy 設為 force 時，需要與
完整請求參數綁定的短期 approval_ref。

## 工具

| Tool | 用途 |
|---|---|
| runtime_status | 回傳 Bridge、執行階段、匯出、快照、圖與權限狀態 |
| run_generate_sdk | 啟動或重用非同步 REFramework 中繼資料匯出 |
| search_types | 在已索引快照中搜尋型別 |
| describe_type | 回傳型別、成員、繼承、泛型、RSZ 與覆蓋資訊 |
| search_members | 搜尋精確多載，融合靜態、MOD、可達性與即時證據 |
| find_type_dependencies | 走訪靜態型別與成員相依關係 |
| list_singletons | 列出即時 Managed 與 Native Singleton 根 |
| inspect_object | 有界檢查欄位、allowlist getter、子 ObjectRef 與儲存檢視 |
| search_usage_examples | 搜尋從 Lua MOD 擷取的語法級用法 |
| find_access_paths | 從四張圖產生並排序多根、強型別 AccessPlan DAG |
| validate_access_plan | 離線並結合目前執行階段驗證 AccessPlan |
| draft_lua_probe | 根據所選 AccessPlan 產生 REFramework Lua 草稿 |
| validate_lua_probe | 檢查語法、符號、風險、生命週期與選用即時編譯 |
| invoke_method | 在即時 Plan 驗證及核准後呼叫精確方法 |
| set_field | 在即時 Plan 驗證及核准後寫入精確欄位 |
| run_lua_probe | 執行已驗證的一次性或限時隔離 Probe |
| install_hook | 安裝有邊界的觀測 Hook 或經核准的轉換 Hook |
| remove_hook | 冪等移除本伺服器持有的 Hook 並封存事件 |

## Resources

大型或需持續讀取的結果透過以下 URI 範本提供：

- reframework://metadata/exports/{job_ref}
- reframework://metadata/snapshots/{snapshot_id}/manifest
- reframework://metadata/snapshots/{snapshot_id}/coverage
- reframework://explorations/{exploration_id}/graph
- reframework://access-plans/{plan_ref}
- reframework://access-plans/{plan_ref}/validation
- reframework://usage/{usage_ref}
- reframework://hooks/{hook_ref}/events
- reframework://probes/{probe_ref}/events

將 exploration_id 設為 current 可讀取目前連線的 runtime epoch。
usage_ref 可以是 usage:{usage_pk}，也可以是用法專案 ID。

## 離線命令

~~~powershell
reframework-mcp --config .\config.toml import-dump C:\path\to\il2cpp_dump.json --manifest C:\path\to\manifest.json
reframework-mcp --config .\config.toml index-mod C:\Mods\Example --game-id mhwilds
reframework-mcp --config .\config.toml serve --index-configured-mods
~~~

MOD 索引器會記錄型別查詢、欄位、方法呼叫、Hook、變數綁定與鏈路順序。若希望
存取路徑排序反映某個遊戲中已驗證的做法，請輸入真實可用的 MOD 專案。

## 執行階段安全與目前邊界

- 寫入操作核准有效期很短，並綁定操作、完整參數雜湊、runtime epoch 與 Plan
  驗證參照。
- invoke_method 與 set_field 會拒絕缺少、僅離線、已過期或屬於舊 epoch 的
  AccessPlan 驗證。
- Getter 預設不執行。inspect_object 只呼叫 getter_allowlist 中明確列出的
  零參數 getter。
- Hook 與 Probe 的佇列、生命週期、事件數、指令數、影格數及輸出位元組均有限制。
- ObjectRef 會過期；原始指標不會進入 MCP、IPC 回應、Probe 輸出或記錄。
- REFramework 公開外掛 API 目前沒有具邊界檢查的受控陣列讀取介面。因此
  1.0.0 會明確回報 System.Array 直接分頁不可用，只透過公開 Field API 暴露
  List 與 Dictionary 的後備儲存檢視，不猜測私有記憶體配置。需要直接讀取
  陣列元素時，應改用已驗證的 Lua Probe。
- REF Nightly 是唯一正式上游。新的官方 Nightly 只有在定時或手動觸發的對齊
  工作流程通過修補、完整 REF 建置、Bridge 建置、ABI 匯出與封裝門檻後才列為
  支援。

## 開發

~~~powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy src
.\.venv\Scripts\python.exe -m build

cmake --build out\build --config Release --target reframework_mcp reframework_export_service_syntax reframework_probe_service_syntax
~~~

18 個工具的輸入輸出契約由 scripts\export_schemas.py 產生，並由
schemas\tool-contracts-v1.sha256 鎖定。測試包含具代表性的 MHST3 與 MHWS
MOD 存取鏈範例。

REF Nightly Alignment 會定時執行，也可在 GitHub Actions 中手動觸發。手動
執行可檢測目前最新官方 Nightly，也可指定某個官方 nightly tag，並可強制重建
已命中成功快取的目標。

## 授權條款

MIT。REFramework 是獨立專案，使用其自身授權條款。
