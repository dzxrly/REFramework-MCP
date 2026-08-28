# REFramework-MCP

[English](../README.md) | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-TW.md)

REFramework-MCP 讓 MCP 用戶端透過
[REFramework](https://github.com/praydog/REFramework) 查詢 RE Engine
中繼資料、檢查遊戲內物件並測試 Lua 程式碼。它主要供 MOD 作者使用，減少在
Object Explorer 中反覆尋找型別、成員與呼叫鏈的時間。

本專案只跟隨 REF Nightly。

## 安裝

1. 從 [Releases](https://github.com/dzxrly/REFramework-MCP/releases)
   下載最新的 Windows ZIP。
2. 解壓縮後，將 game 目錄內的檔案複製到遊戲目錄。請先備份現有的
   dinput8.dll。
3. 連按兩下 REFramework-MCP.exe。

EXE 會開啟主控台，並在 http://127.0.0.1:9966/mcp 提供 MCP。關閉視窗或按
Ctrl+C 即可停止。封裝版不需要另外安裝 Python，也沒有 GUI 或必填設定檔。

每個 Release 的名稱都包含 REF Nightly 編號、原始碼提交與 8 位建置 ID。

## 連接 MCP 用戶端

支援 Streamable HTTP 的用戶端可直接連線至
http://127.0.0.1:9966/mcp。

如果用戶端透過 stdio 啟動伺服器，請讓它指向同一個 EXE：

~~~json
{
  "mcpServers": {
    "reframework": {
      "command": "<REFramework-MCP.exe 的絕對路徑>",
      "args": ["serve"]
    }
  }
}
~~~

使用即時工具前先啟動遊戲。下列命令可檢查遊戲內 Bridge：

~~~powershell
.\REFramework-MCP.exe doctor
~~~

若要修改連接埠、資料目錄、權限或 MOD 目錄，可在 EXE 同目錄放置
config.toml。HTTP 監聽位址請保持為 127.0.0.1。

## 常用 MOD 工作流程

1. 呼叫 runtime_status。
2. 以 json_only 模式呼叫 run_generate_sdk，等待快照完成索引。
3. 使用 search_types、describe_type、search_members 與
   find_type_dependencies 找到所需 API。
4. 索引已經可用的 Lua MOD，再使用 search_usage_examples 與
   find_access_paths 重用現有呼叫鏈。
5. 中繼資料不足時，產生、驗證並執行 Lua Probe。

invoke_method、set_field 與修改行為的 Hook 需要核准。遊戲工作階段變更後，
舊的執行階段物件參照會失效。

## MCP 工具

- 中繼資料與物件：runtime_status、run_generate_sdk、search_types、
  describe_type、search_members、find_type_dependencies、list_singletons、
  inspect_object
- 呼叫鏈：search_usage_examples、find_access_paths、validate_access_plan
- Lua 與執行階段操作：draft_lua_probe、validate_lua_probe、run_lua_probe、
  invoke_method、set_field、install_hook、remove_hook

## 離線索引

~~~powershell
.\REFramework-MCP.exe import-dump <dump.json> --manifest <manifest.json>
.\REFramework-MCP.exe index-mod <MOD目錄> --game-id <遊戲ID>
~~~

若要讓呼叫鏈排序貼近某款遊戲，請索引實際可用的 MOD 專案。

## 從原始碼建置

原始碼建置需要 Python 3.11、已安裝 C++ 桌面開發工具的 Visual Studio 2022、
CMake 3.25 或更新版本，以及遞迴複製的 REFramework。

~~~powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev,bundle]"

$refSource = (Resolve-Path "..\REFramework").Path
$baseline = Get-Content .\reframework\nightly-baseline.json -Raw | ConvertFrom-Json
git -C $refSource checkout $baseline.commit
git -C $refSource submodule update --init --recursive

.\reframework\export_service\install.ps1 -REFrameworkRoot $refSource
cmake -S $refSource -B .tmp\ref-build -G "Visual Studio 17 2022" -A x64
cmake --build .tmp\ref-build --config Release --target REFramework

cmake -S . -B out\build -G "Visual Studio 17 2022" -A x64 "-DREFRAMEWORK_ROOT=$refSource"
cmake --build out\build --config Release --target reframework_mcp
cmake --install out\build --config Release --prefix "<遊戲目錄>"
~~~

專案檢查命令：

~~~powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests scripts
.\.venv\Scripts\python.exe -m ruff format --check src tests scripts
.\.venv\Scripts\python.exe -m mypy src
.\.venv\Scripts\python.exe -m build
~~~

## 發布

儲存庫只保留一個 GitHub Actions 工作流程。它每天兩次檢查官方 REF Nightly，
也可手動執行。發現新 Nightly 後，會完成測試、建置、封裝並建立 GitHub
Release；手動執行還可以指定 Nightly tag 或強制再次發布。

## 授權條款

MIT。REFramework 是獨立專案，使用其自身授權條款。
