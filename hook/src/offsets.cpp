#include "offsets.h"

#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <filesystem>
#include <fstream>
#include <sstream>
#include <string>
#include <algorithm>
#include <cctype>

// 文字列の前後空白を除去する
static std::string trim(const std::string& s) {
    size_t a = s.find_first_not_of(" \t\r\n");
    if (a == std::string::npos) return {};
    size_t b = s.find_last_not_of(" \t\r\n");
    return s.substr(a, b - a + 1);
}

// 16進数または10進数文字列を uint64_t に変換する
static uint64_t parse_uint(const std::string& s) {
    if (s.size() > 2 && s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) {
        return std::stoull(s, nullptr, 16);
    }
    return std::stoull(s, nullptr, 10);
}

// .toml ファイルを解析してチェックサムとオフセットを取得する
struct ParsedToml {
    uint32_t checksum = 0;
    HookOffsets offsets;
};

static ParsedToml parse_toml(const std::string& path) {
    ParsedToml result;
    std::ifstream f(path);
    if (!f.is_open()) return result;

    std::string section;
    std::string line;
    while (std::getline(f, line)) {
        std::string s = trim(line);
        if (s.empty() || s[0] == '#') continue;

        if (s.front() == '[' && s.back() == ']') {
            section = s.substr(1, s.size() - 2);
            continue;
        }

        auto eq = s.find('=');
        if (eq == std::string::npos) continue;

        std::string key = trim(s.substr(0, eq));
        std::string val = trim(s.substr(eq + 1));
        // インラインコメントを除去
        auto hash = val.find('#');
        if (hash != std::string::npos) val = trim(val.substr(0, hash));

        if (val.empty()) continue;

        try {
            uint64_t num = parse_uint(val);
            if (section == "metadata" && key == "checksum") {
                result.checksum = static_cast<uint32_t>(num);
            } else if (section == "offsets") {
                if      (key == "addst")      result.offsets.addst      = num;
                else if (key == "addst_top")  result.offsets.addst_top  = num;
                else if (key == "addst_flag") result.offsets.addst_flag = num;
                else if (key == "rich_text_parse")
                    result.offsets.rich_text_parse = num;
                else if (key == "rich_text_render")
                    result.offsets.rich_text_render = num;
            }
        } catch (...) {
            // 数値以外の値（文字列フィールド等）は無視
        }
    }
    return result;
}

uint32_t get_pe_timestamp() {
    wchar_t path[MAX_PATH] = {};
    GetModuleFileNameW(nullptr, path, MAX_PATH);

    HANDLE file = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ,
                              nullptr, OPEN_EXISTING, 0, nullptr);
    if (file == INVALID_HANDLE_VALUE) return 0;

    IMAGE_DOS_HEADER dos = {};
    DWORD read = 0;
    ReadFile(file, &dos, sizeof(dos), &read, nullptr);

    // PE シグネチャの直後に IMAGE_FILE_HEADER がある
    SetFilePointer(file, dos.e_lfanew + 4, nullptr, FILE_BEGIN);

    IMAGE_FILE_HEADER coff = {};
    ReadFile(file, &coff, sizeof(coff), &read, nullptr);

    CloseHandle(file);
    return coff.TimeDateStamp;
}

std::optional<HookOffsets> load_offsets(const std::string& data_dir, uint32_t checksum) {
    std::error_code ec;
    for (auto& entry : std::filesystem::directory_iterator(data_dir, ec)) {
        if (ec) break;
        if (entry.path().extension() != ".toml") continue;

        auto parsed = parse_toml(entry.path().string());
        if (parsed.checksum == checksum && parsed.offsets.addst != 0) {
            return parsed.offsets;
        }
    }
    return std::nullopt;
}
