#include "pipe.h"

#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <string>
#include <string_view>
#include <unordered_set>
#include <vector>

static constexpr char PIPE_NAME[] = "\\\\.\\pipe\\df_translation";
static constexpr DWORD FLUSH_INTERVAL_MS = 500;

static HANDLE g_pipe   = INVALID_HANDLE_VALUE;
static HANDLE g_thread = nullptr;
static CRITICAL_SECTION g_cs;
static bool g_running  = false;

struct TextEntry {
    std::string text;
    uint8_t justify;
    int32_t x;
    int32_t y;
};

// 今フレームのテキストバッファ（挿入順保持 + 画面位置で重複排除）
static std::vector<TextEntry> g_frame_buf;
// 重複チェックキー: y<<32|x（同一座標に複数回 addst が呼ばれることがあるため）
static std::unordered_set<uint64_t> g_frame_seen;
static CRITICAL_SECTION g_buf_cs;
static DWORD g_last_flush = 0;

// 文字列中の制御文字をエスケープする（プロトコル上の区切り文字と衝突しないよう）
static std::string escape(std::string_view sv) {
    std::string out;
    out.reserve(sv.size());
    for (char c : sv) {
        if      (c == '\n') { out += "\\n"; }
        else if (c == '\t') { out += "\\t"; }
        else                { out += c;     }
    }
    return out;
}

// クリティカルセクションを保護しつつパイプに書き込む。失敗時はハンドルをリセットする。
static void write_to_pipe(const char* data, DWORD size) {
    EnterCriticalSection(&g_cs);
    HANDLE pipe = g_pipe;
    LeaveCriticalSection(&g_cs);

    if (pipe == INVALID_HANDLE_VALUE) return;

    DWORD written = 0;
    if (!WriteFile(pipe, data, size, &written, nullptr)) {
        // クライアントが切断された
        EnterCriticalSection(&g_cs);
        if (g_pipe == pipe) {
            CloseHandle(g_pipe);
            g_pipe = INVALID_HANDLE_VALUE;
        }
        LeaveCriticalSection(&g_cs);
    }
}

// クライアントの接続を待ち続けるバックグラウンドスレッド
static DWORD WINAPI pipe_thread(LPVOID) {
    while (g_running) {
        HANDLE pipe = CreateNamedPipeA(
            PIPE_NAME,
            PIPE_ACCESS_OUTBOUND,
            PIPE_TYPE_BYTE | PIPE_WAIT,
            PIPE_UNLIMITED_INSTANCES,
            65536, 0,
            NMPWAIT_USE_DEFAULT_WAIT,
            nullptr
        );

        if (pipe == INVALID_HANDLE_VALUE) {
            Sleep(1000);
            continue;
        }

        // クライアントが接続してくるまでブロック
        BOOL ok = ConnectNamedPipe(pipe, nullptr);
        if (!ok && GetLastError() != ERROR_PIPE_CONNECTED) {
            CloseHandle(pipe);
            Sleep(100);
            continue;
        }

        EnterCriticalSection(&g_cs);
        g_pipe = pipe;
        LeaveCriticalSection(&g_cs);

        // g_pipe が自分のハンドルである間は接続中とみなす
        while (g_running) {
            Sleep(200);
            EnterCriticalSection(&g_cs);
            bool still_mine = (g_pipe == pipe);
            LeaveCriticalSection(&g_cs);
            if (!still_mine) break;
        }
        // pipe_thread はハンドルを閉じない（write_to_pipe が閉じる）
    }
    return 0;
}

void pipe_init() {
    InitializeCriticalSection(&g_cs);
    InitializeCriticalSection(&g_buf_cs);
    g_running = true;
    g_last_flush = GetTickCount();
    g_thread = CreateThread(nullptr, 0, pipe_thread, nullptr, 0, nullptr);
}

void pipe_shutdown() {
    g_running = false;

    EnterCriticalSection(&g_cs);
    if (g_pipe != INVALID_HANDLE_VALUE) {
        CloseHandle(g_pipe);
        g_pipe = INVALID_HANDLE_VALUE;
    }
    LeaveCriticalSection(&g_cs);

    if (g_thread) {
        // スレッドが ConnectNamedPipe でブロックしている場合は強制終了
        TerminateThread(g_thread, 0);
        CloseHandle(g_thread);
        g_thread = nullptr;
    }

    DeleteCriticalSection(&g_cs);
    DeleteCriticalSection(&g_buf_cs);
}

void pipe_add_text(std::string_view text, uint8_t justify, int32_t x, int32_t y) {
    if (text.empty()) return;

    uint64_t pos_key = (static_cast<uint64_t>(static_cast<uint32_t>(y)) << 32)
                     |  static_cast<uint64_t>(static_cast<uint32_t>(x));
    std::string s(text);
    EnterCriticalSection(&g_buf_cs);
    if (g_frame_seen.emplace(pos_key).second) {
        g_frame_buf.push_back({std::move(s), justify, x, y});
    }
    LeaveCriticalSection(&g_buf_cs);
}

void pipe_flush_frame() {
    DWORD now = GetTickCount();
    if (now - g_last_flush < FLUSH_INTERVAL_MS) return;
    g_last_flush = now;

    EnterCriticalSection(&g_buf_cs);
    auto texts = std::move(g_frame_buf);
    g_frame_buf.clear();
    g_frame_seen.clear();
    LeaveCriticalSection(&g_buf_cs);

    if (texts.empty()) return;

    // T\t<justify>\t<x>\t<y>\t<text>\n を連続して送信後、F\n でフレーム境界を通知する
    for (const auto& e : texts) {
        std::string msg = "T\t" + std::to_string(e.justify)
                        + "\t" + std::to_string(e.x)
                        + "\t" + std::to_string(e.y)
                        + "\t" + escape(e.text) + "\n";
        write_to_pipe(msg.c_str(), static_cast<DWORD>(msg.size()));
    }
    write_to_pipe("F\n", 2);
}
