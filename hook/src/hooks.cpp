#include "hooks.h"
#include "cxxstring.h"
#include "pipe.h"

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <detours/detours.h>

#include <string>
#include <string_view>
#include <unordered_map>

// graphicst フィールドオフセット（df-structures/df.g_src.graphics.xml より計算）
// x64 MSVC レイアウト:
//   +0x00 viewport        (vector<ptr>, 24 bytes)
//   +0x18 main_viewport   (ptr, 8 bytes)
//   +0x20 lower_viewport  (ptr[8], 64 bytes)
//   +0x60 map_port        (vector<ptr>, 24 bytes)
//   +0x78 main_map_port   (ptr, 8 bytes)
//   +0x80 viewport_zoom_factor (int32_t, 4 bytes)
//   +0x84 screenx         (int32_t) ← テキストカーソル X
//   +0x88 screeny         (int32_t) ← テキストカーソル Y
static constexpr ptrdiff_t GPS_SCREENX_OFF = 0x84;
static constexpr ptrdiff_t GPS_SCREENY_OFF = 0x88;

// rich text 描画オブジェクトのフィールド（53.13 steam win64 の逆アセンブルより）
// rich_text_render(this):
//   +0x10 描画領域 X
//   +0x14 描画領域上端 Y
//   +0x180 rich text レイアウトへのポインタ
static constexpr ptrdiff_t RICH_WIDGET_X_OFF      = 0x10;
static constexpr ptrdiff_t RICH_WIDGET_Y_OFF      = 0x14;
static constexpr ptrdiff_t RICH_WIDGET_LAYOUT_OFF = 0x180;

// rich_text_parse(layout, full_text) で受け取った全文を、描画時に参照できるよう保存する。
// SRWLOCK は静的初期化でき、読み取り側（毎フレーム）を共有ロックにできる。
static SRWLOCK g_rich_text_lock = SRWLOCK_INIT;
static std::unordered_map<uintptr_t, std::string> g_rich_text_by_layout;

// rich_text_render() 内部から呼ばれる addst() の単語イベントを抑制する。
// 入れ子呼び出しにも対応するため bool ではなく深度で管理する。
static thread_local uint32_t g_suppress_addst_depth = 0;

static std::string clean_rich_text(std::string_view src) {
    std::string out;
    out.reserve(src.size());

    for (size_t i = 0; i < src.size();) {
        if (src[i] == '[') {
            size_t close = src.find(']', i + 1);
            if (close != std::string_view::npos) {
                auto tag = src.substr(i + 1, close - i - 1);
                if (tag == "B") {
                    // [B] は段落区切り。同じタグが連続しても改行を増やしすぎない。
                    while (!out.empty() && out.back() == ' ') out.pop_back();
                    if (!out.empty() && out.back() != '\n') out.push_back('\n');
                    i = close + 1;
                    continue;
                }
                if (tag.size() >= 2 && tag[0] == 'C' && tag[1] == ':') {
                    // [C:fg:bg:bold] は色指定なので翻訳原文から除去する。
                    i = close + 1;
                    continue;
                }
            }
        }

        char c = src[i++];
        if (c == '\r') continue;
        out.push_back(c);
    }

    // 行末空白、連続空行、文字列全体の前後空白を整理する。
    std::string normalized;
    normalized.reserve(out.size());
    bool previous_space = false;
    bool previous_newline = false;
    for (char c : out) {
        if (c == '\n') {
            while (!normalized.empty() && normalized.back() == ' ') normalized.pop_back();
            if (!normalized.empty() && !previous_newline) normalized.push_back('\n');
            previous_space = false;
            previous_newline = true;
        } else if (c == ' ' || c == '\t') {
            if (!normalized.empty() && !previous_space && !previous_newline) {
                normalized.push_back(' ');
            }
            previous_space = true;
        } else {
            normalized.push_back(c);
            previous_space = false;
            previous_newline = false;
        }
    }
    while (!normalized.empty() &&
           (normalized.back() == ' ' || normalized.back() == '\n')) {
        normalized.pop_back();
    }
    return normalized;
}

// --- addst -----------------------------------------------------------
using addst_fn = void(*)(uintptr_t gps, const CxxString* src, uint8_t justify, uint32_t space);
static addst_fn orig_addst     = nullptr;
static addst_fn orig_addst_top = nullptr;

static void intercept_text(uintptr_t gps, const CxxString* src, uint8_t justify) {
    if (g_suppress_addst_depth != 0) return;
    if (!src) return;
    auto sv = src->view();
    if (sv.empty()) return;
    auto x = *reinterpret_cast<const int32_t*>(gps + GPS_SCREENX_OFF);
    auto y = *reinterpret_cast<const int32_t*>(gps + GPS_SCREENY_OFF);
    pipe_add_text(sv, justify, x, y);
}

static void hooked_addst(uintptr_t gps, const CxxString* src, uint8_t justify, uint32_t space) {
    intercept_text(gps, src, justify);
    orig_addst(gps, src, justify, space);
}

static void hooked_addst_top(uintptr_t gps, const CxxString* src, uint8_t justify, uint32_t space) {
    intercept_text(gps, src, justify);
    orig_addst_top(gps, src, justify, space);
}

// --- addst_flag ------------------------------------------------------
using addst_flag_fn = void(*)(uintptr_t gps, const CxxString* src,
                               uintptr_t a3, uintptr_t a4, uint32_t flag);
static addst_flag_fn orig_addst_flag = nullptr;

static void hooked_addst_flag(uintptr_t gps, const CxxString* src,
                               uintptr_t a3, uintptr_t a4, uint32_t flag) {
    intercept_text(gps, src, 0);
    orig_addst_flag(gps, src, a3, a4, flag);
}

// --- rich text -------------------------------------------------------
// 0xD49200: 色タグ付き全文を解析し、単語・色・座標を持つレイアウトを構築する。
using rich_text_parse_fn = void(*)(uintptr_t layout, const CxxString* src);
static rich_text_parse_fn orig_rich_text_parse = nullptr;

static void hooked_rich_text_parse(uintptr_t layout, const CxxString* src) {
    std::string captured;
    if (src) {
        auto sv = src->view();
        if (!sv.empty()) {
            captured.assign(sv.data(), sv.size());
        }
    }

    orig_rich_text_parse(layout, src);

    AcquireSRWLockExclusive(&g_rich_text_lock);
    if (captured.empty()) {
        g_rich_text_by_layout.erase(layout);
    } else {
        g_rich_text_by_layout[layout] = std::move(captured);
    }
    ReleaseSRWLockExclusive(&g_rich_text_lock);
}

// 0xD4B990: レイアウトの各単語を走査し、単語ごとに addst() を呼ぶ。
using rich_text_render_fn = void(*)(uintptr_t widget);
static rich_text_render_fn orig_rich_text_render = nullptr;

static void hooked_rich_text_render(uintptr_t widget) {
    uintptr_t layout = *reinterpret_cast<const uintptr_t*>(
        widget + RICH_WIDGET_LAYOUT_OFF);

    std::string captured;
    AcquireSRWLockShared(&g_rich_text_lock);
    auto it = g_rich_text_by_layout.find(layout);
    if (it != g_rich_text_by_layout.end()) {
        captured = it->second;
    }
    ReleaseSRWLockShared(&g_rich_text_lock);

    if (captured.empty()) {
        orig_rich_text_render(widget);
        return;
    }

    std::string cleaned = clean_rich_text(captured);
    if (!cleaned.empty()) {
        auto x = *reinterpret_cast<const int32_t*>(widget + RICH_WIDGET_X_OFF);
        auto y = *reinterpret_cast<const int32_t*>(widget + RICH_WIDGET_Y_OFF);
        pipe_add_text(cleaned, 0, x, y);
    }

    ++g_suppress_addst_depth;
    orig_rich_text_render(widget);
    --g_suppress_addst_depth;
}

// ---------------------------------------------------------------------

void hooks_install(uintptr_t base, const HookOffsets& off) {
    orig_addst = reinterpret_cast<addst_fn>(base + off.addst);

    DetourTransactionBegin();
    DetourUpdateThread(GetCurrentThread());
    DetourAttach(reinterpret_cast<PVOID*>(&orig_addst),
                 reinterpret_cast<PVOID>(hooked_addst));

    if (off.addst_top != 0) {
        orig_addst_top = reinterpret_cast<addst_fn>(base + off.addst_top);
        DetourAttach(reinterpret_cast<PVOID*>(&orig_addst_top),
                     reinterpret_cast<PVOID>(hooked_addst_top));
    }

    if (off.addst_flag != 0) {
        orig_addst_flag = reinterpret_cast<addst_flag_fn>(base + off.addst_flag);
        DetourAttach(reinterpret_cast<PVOID*>(&orig_addst_flag),
                     reinterpret_cast<PVOID>(hooked_addst_flag));
    }

    if (off.rich_text_parse != 0 && off.rich_text_render != 0) {
        orig_rich_text_parse =
            reinterpret_cast<rich_text_parse_fn>(base + off.rich_text_parse);
        orig_rich_text_render =
            reinterpret_cast<rich_text_render_fn>(base + off.rich_text_render);
        DetourAttach(reinterpret_cast<PVOID*>(&orig_rich_text_parse),
                     reinterpret_cast<PVOID>(hooked_rich_text_parse));
        DetourAttach(reinterpret_cast<PVOID*>(&orig_rich_text_render),
                     reinterpret_cast<PVOID>(hooked_rich_text_render));
    }

    DetourTransactionCommit();
}

void hooks_uninstall() {
    DetourTransactionBegin();
    DetourUpdateThread(GetCurrentThread());

    if (orig_addst)      DetourDetach(reinterpret_cast<PVOID*>(&orig_addst),     reinterpret_cast<PVOID>(hooked_addst));
    if (orig_addst_top)  DetourDetach(reinterpret_cast<PVOID*>(&orig_addst_top), reinterpret_cast<PVOID>(hooked_addst_top));
    if (orig_addst_flag) DetourDetach(reinterpret_cast<PVOID*>(&orig_addst_flag),reinterpret_cast<PVOID>(hooked_addst_flag));
    if (orig_rich_text_parse)
        DetourDetach(reinterpret_cast<PVOID*>(&orig_rich_text_parse),
                     reinterpret_cast<PVOID>(hooked_rich_text_parse));
    if (orig_rich_text_render)
        DetourDetach(reinterpret_cast<PVOID*>(&orig_rich_text_render),
                     reinterpret_cast<PVOID>(hooked_rich_text_render));

    DetourTransactionCommit();

    AcquireSRWLockExclusive(&g_rich_text_lock);
    g_rich_text_by_layout.clear();
    ReleaseSRWLockExclusive(&g_rich_text_lock);
}
