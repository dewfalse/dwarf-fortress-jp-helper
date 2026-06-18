#pragma once
#include <cstddef>
#include <string_view>

// MSVC x64 における std::string のメモリレイアウト
// Small String Optimization (SSO): 15文字以下はバッファに直接格納
struct CxxString {
    union {
        char  buf[16]; // SSO バッファ
        char* ptr;     // ヒープポインタ（16文字以上）
    };
    size_t len;
    size_t cap; // cap < 16 のとき SSO モード

    std::string_view view() const noexcept {
        if (len == 0) return {};
        const char* p = (cap < 16) ? buf : ptr;
        return {p, len};
    }
};
