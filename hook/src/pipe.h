#pragma once
#include <string_view>

// Named Pipe サーバーを起動し、Python クライアントの接続を待つ
void pipe_init();

// Named Pipe サーバーを停止する
void pipe_shutdown();

// テキストをバッファに追加する（addst フックから呼ぶ）
// justify: addst の第3引数（0=左, 1=右, 2=中央）
// x, y: graphicst::screenx / screeny（描画カーソル位置、タイル単位）
void pipe_add_text(std::string_view text, uint8_t justify, int32_t x, int32_t y);

// バッファ内のテキストを一括送信してクリアする（dfhooks_update から呼ぶ）
void pipe_flush_frame();
