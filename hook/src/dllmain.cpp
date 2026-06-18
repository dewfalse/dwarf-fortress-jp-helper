#include "hooks.h"
#include "offsets.h"
#include "pipe.h"

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <filesystem>
#include <string>

static bool g_hooks_installed = false;

// vcpkg の Detours Release ビルドは NDEBUG 未定義のまま _CrtDbgReport を参照する。
// Release 静的 CRT にはこの関数が含まれないため、リンクエラーを防ぐスタブを提供する。
// 戻り値 0 = Ignore（実行継続）。1 = Retry は _CrtDbgBreak() を呼びクラッシュする。
extern "C" int __cdecl _CrtDbgReport(int, const char*, int, const char*, const char*, ...) {
    return 0;
}

// DF.exe と同じディレクトリのパスを返す
static std::string get_game_dir() {
    wchar_t buf[MAX_PATH] = {};
    GetModuleFileNameW(nullptr, buf, MAX_PATH);
    return std::filesystem::path(buf).parent_path().string();
}

extern "C" {

__declspec(dllexport) void dfhooks_init() {
    pipe_init();

    uint32_t checksum = get_pe_timestamp();
    std::string data_dir = get_game_dir() + "\\dfint-data";

    auto offsets = load_offsets(data_dir, checksum);
    if (!offsets) {
        // オフセットが見つからなくても DLL 自体は問題なく動作する
        // pipe は起動済みなので Python 側には接続できる
        return;
    }

    uintptr_t base = reinterpret_cast<uintptr_t>(GetModuleHandleA(nullptr));
    hooks_install(base, *offsets);
    g_hooks_installed = true;
}

__declspec(dllexport) void dfhooks_shutdown() {
    if (g_hooks_installed) {
        hooks_uninstall();
        g_hooks_installed = false;
    }
    pipe_shutdown();
}

// 毎フレーム呼ばれる。500ms ごとにバッファを Python へ送信する。
__declspec(dllexport) void dfhooks_update() {
    pipe_flush_frame();
}

// 以下は DF が要求する可能性があるエクスポート（本実装では未使用）
__declspec(dllexport) void dfhooks_prerender()      {}
__declspec(dllexport) bool dfhooks_sdl_event(void*) { return false; }
__declspec(dllexport) bool dfhooks_ncurses_key(int) { return false; }
__declspec(dllexport) void dfhooks_sdl_loop_fn()    {}

} // extern "C"

BOOL APIENTRY DllMain(HMODULE, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_DETACH) {
        // DF が DLL をアンロードするとき（dfhooks_shutdown が呼ばれない場合の保険）
        if (g_hooks_installed) {
            hooks_uninstall();
        }
    }
    return TRUE;
}
