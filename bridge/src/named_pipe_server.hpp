#pragma once

#include <Windows.h>

#include <nlohmann/json.hpp>

#include <functional>
#include <mutex>
#include <string>
#include <thread>

namespace reframework_mcp {

class NamedPipeServer {
public:
    using Handler = std::function<nlohmann::json(const nlohmann::json&)>;

    NamedPipeServer(std::wstring pipe_name, Handler handler);
    ~NamedPipeServer();

    NamedPipeServer(const NamedPipeServer&) = delete;
    NamedPipeServer& operator=(const NamedPipeServer&) = delete;

    void start();
    void stop();
    [[nodiscard]] bool running() const noexcept;

private:
    void run(std::stop_token stop_token);
    void serve_client(HANDLE pipe);
    HANDLE create_pipe() const;
    void wake();
    static bool read_exact(HANDLE pipe, void* output, DWORD size);
    static bool write_all(HANDLE pipe, const void* input, DWORD size);

    std::wstring m_pipe_name;
    Handler m_handler;
    std::jthread m_thread;
    mutable std::mutex m_state_mutex;
    HANDLE m_active_pipe{INVALID_HANDLE_VALUE};
};

} // namespace reframework_mcp
