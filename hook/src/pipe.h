#pragma once
#include <cstdint>
#include <string_view>

enum class PipeTextKind : uint8_t {
    Normal = 0,
    RichBlock = 1,
    RichToken = 2,
};

// Named Pipe サーバーを起動し、Python クライアントの接続を待つ
void pipe_init();

// Named Pipe サーバーを停止する
void pipe_shutdown();

// テキストをバッファに追加する（addst フックから呼ぶ）
// justify: addst の第3引数（0=左, 1=右, 2=中央）
// x, y: graphicst::screenx / screeny（描画カーソル位置、タイル単位）
// mouse_x, mouse_y: DF 内のマウスタイル座標
// mouse_pixel_x, mouse_pixel_y: DF 内のマウスピクセル座標（クライアント左上基準）
// tile_w, tile_h: 現在のタイルサイズ（ピクセル）
void pipe_add_text(
    std::string_view text,
    uint8_t justify,
    int32_t x,
    int32_t y,
    int32_t mouse_x = -1,
    int32_t mouse_y = -1,
    int32_t mouse_pixel_x = -1,
    int32_t mouse_pixel_y = -1,
    int32_t tile_w = -1,
    int32_t tile_h = -1,
    PipeTextKind kind = PipeTextKind::Normal,
    uint64_t group_id = 0
);

// バッファ内のテキストを一括送信してクリアする（dfhooks_update から呼ぶ）
void pipe_flush_frame();
