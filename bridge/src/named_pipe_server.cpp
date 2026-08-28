#include "named_pipe_server.hpp"

#include <sddl.h>

#include <cstdint>
#include <stdexcept>
#include <utility>
#include <vector>

#include <reframework_mcp/protocol.hpp>

namespace reframework_mcp {
namespace {

class CurrentUserSecurity {
public:
    CurrentUserSecurity() {
        HANDLE token{};
        if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) {
            throw std::runtime_error("OpenProcessToken failed");
        }
        DWORD length{};
        GetTokenInformation(token, TokenUser, nullptr, 0, &length);
        std::vector<std::byte> buffer(length);
        if (!GetTokenInformation(token, TokenUser, buffer.data(), length, &length)) {
            CloseHandle(token);
            throw std::runtime_error("GetTokenInformation failed");
        }
        CloseHandle(token);
        const auto* token_user = reinterpret_cast<const TOKEN_USER*>(buffer.data());
        LPWSTR sid_text{};
        if (!ConvertSidToStringSidW(token_user->User.Sid, &sid_text)) {
            throw std::runtime_error("ConvertSidToStringSidW failed");
        }
        const std::wstring sddl = L"D:P(A;;GA;;;" + std::wstring{sid_text} + L")";
        LocalFree(sid_text);
        if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl.c_str(),
                SDDL_REVISION_1,
                &m_descriptor,
                nullptr)) {
            throw std::runtime_error(
                "ConvertStringSecurityDescriptorToSecurityDescriptorW failed");
        }
        m_attributes.nLength = sizeof(m_attributes);
        m_attributes.lpSecurityDescriptor = m_descriptor;
        m_attributes.bInheritHandle = FALSE;
    }

    ~CurrentUserSecurity() {
        if (m_descriptor != nullptr) {
            LocalFree(m_descriptor);
        }
    }

    SECURITY_ATTRIBUTES* attributes() {
        return &m_attributes;
    }

private:
    PSECURITY_DESCRIPTOR m_descriptor{};
    SECURITY_ATTRIBUTES m_attributes{};
};

} // namespace

NamedPipeServer::NamedPipeServer(std::wstring pipe_name, Handler handler)
    : m_pipe_name{std::move(pipe_name)}, m_handler{std::move(handler)} {
}

NamedPipeServer::~NamedPipeServer() {
    stop();
}

void NamedPipeServer::start() {
    if (m_thread.joinable()) {
        return;
    }
    m_thread = std::jthread{[this](const std::stop_token stop_token) {
        run(stop_token);
    }};
}

void NamedPipeServer::stop() {
    if (!m_thread.joinable()) {
        return;
    }
    m_thread.request_stop();
    {
        std::scoped_lock lock{m_state_mutex};
        if (m_active_pipe != INVALID_HANDLE_VALUE) {
            CancelIoEx(m_active_pipe, nullptr);
        }
    }
    wake();
    m_thread.join();
}

bool NamedPipeServer::running() const noexcept {
    return m_thread.joinable();
}

void NamedPipeServer::run(const std::stop_token stop_token) {
    while (!stop_token.stop_requested()) {
        auto pipe = create_pipe();
        {
            std::scoped_lock lock{m_state_mutex};
            m_active_pipe = pipe;
        }
        const auto connected = ConnectNamedPipe(pipe, nullptr)
            ? TRUE
            : GetLastError() == ERROR_PIPE_CONNECTED;
        if (connected && !stop_token.stop_requested()) {
            serve_client(pipe);
        }
        FlushFileBuffers(pipe);
        DisconnectNamedPipe(pipe);
        CloseHandle(pipe);
        {
            std::scoped_lock lock{m_state_mutex};
            if (m_active_pipe == pipe) {
                m_active_pipe = INVALID_HANDLE_VALUE;
            }
        }
    }
}

void NamedPipeServer::serve_client(HANDLE pipe) {
    uint32_t frame_size{};
    if (!read_exact(pipe, &frame_size, sizeof(frame_size))) {
        return;
    }
    if (frame_size == 0 || frame_size > kMaxFrameBytes) {
        return;
    }
    std::vector<char> bytes(frame_size);
    if (!read_exact(pipe, bytes.data(), frame_size)) {
        return;
    }

    nlohmann::json response;
    try {
        const auto request = nlohmann::json::parse(bytes.begin(), bytes.end());
        response = m_handler(request);
    } catch (const std::exception& error) {
        response = {
            {"protocol", kProtocolVersion},
            {"request_id", ""},
            {"runtime_epoch", nullptr},
            {"ok", false},
            {"data", nlohmann::json::object()},
            {"error", {
                {"code", "BRIDGE_PROTOCOL_ERROR"},
                {"message", error.what()},
                {"details", nlohmann::json::object()},
                {"retryable", false},
            }},
        };
    }
    const auto encoded = response.dump();
    if (encoded.size() > kMaxFrameBytes) {
        return;
    }
    const auto response_size = static_cast<uint32_t>(encoded.size());
    if (!write_all(pipe, &response_size, sizeof(response_size))) {
        return;
    }
    write_all(pipe, encoded.data(), response_size);
}

HANDLE NamedPipeServer::create_pipe() const {
    CurrentUserSecurity security;
    const auto pipe = CreateNamedPipeW(
        m_pipe_name.c_str(),
        PIPE_ACCESS_DUPLEX,
        PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
        4,
        64u * 1024u,
        64u * 1024u,
        0,
        security.attributes());
    if (pipe == INVALID_HANDLE_VALUE) {
        throw std::runtime_error("CreateNamedPipeW failed");
    }
    return pipe;
}

void NamedPipeServer::wake() {
    const auto handle = CreateFileW(
        m_pipe_name.c_str(),
        GENERIC_READ | GENERIC_WRITE,
        0,
        nullptr,
        OPEN_EXISTING,
        0,
        nullptr);
    if (handle != INVALID_HANDLE_VALUE) {
        CloseHandle(handle);
    }
}

bool NamedPipeServer::read_exact(HANDLE pipe, void* output, DWORD size) {
    auto* bytes = static_cast<std::byte*>(output);
    DWORD offset{};
    while (offset < size) {
        DWORD read{};
        if (!ReadFile(pipe, bytes + offset, size - offset, &read, nullptr) || read == 0) {
            return false;
        }
        offset += read;
    }
    return true;
}

bool NamedPipeServer::write_all(HANDLE pipe, const void* input, DWORD size) {
    const auto* bytes = static_cast<const std::byte*>(input);
    DWORD offset{};
    while (offset < size) {
        DWORD written{};
        if (!WriteFile(pipe, bytes + offset, size - offset, &written, nullptr)
            || written == 0) {
            return false;
        }
        offset += written;
    }
    return true;
}

} // namespace reframework_mcp
