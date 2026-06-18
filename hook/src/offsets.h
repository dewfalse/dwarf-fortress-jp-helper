#pragma once
#include <cstdint>
#include <optional>
#include <string>

struct HookOffsets {
    uint64_t addst            = 0;
    uint64_t addst_top        = 0;
    uint64_t addst_flag       = 0;
    uint64_t rich_text_parse  = 0;
    uint64_t rich_text_render = 0;
};

// DwarfFortress.exe の PE タイムスタンプを取得する
uint32_t get_pe_timestamp();

// dfint-data/ 以下の .toml ファイルを検索し、チェックサムが一致するオフセットを返す
std::optional<HookOffsets> load_offsets(const std::string& data_dir, uint32_t checksum);
