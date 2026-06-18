#pragma once
#include "offsets.h"
#include <cstdint>

// フックを設定する（dfhooks_init から呼ぶ）
void hooks_install(uintptr_t base, const HookOffsets& offsets);

// フックを解除する（dfhooks_shutdown から呼ぶ）
void hooks_uninstall();
