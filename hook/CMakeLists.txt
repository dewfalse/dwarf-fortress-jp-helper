cmake_minimum_required(VERSION 3.20)
project(dfjp_hook CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# 静的CRT（/MT）を使用する。x64-windows-static トリプレットの Detours と CRT を合わせる。
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")

find_path(DETOURS_INCLUDE_DIRS "detours/detours.h")
find_library(DETOURS_LIBRARY detours REQUIRED)

add_library(dfjp_hook SHARED
    src/dllmain.cpp
    src/offsets.cpp
    src/pipe.cpp
    src/hooks.cpp
)

target_include_directories(dfjp_hook PRIVATE src ${DETOURS_INCLUDE_DIRS})
target_link_libraries(dfjp_hook PRIVATE ${DETOURS_LIBRARY})

# DF が読み込む DLL 名に合わせる
set_target_properties(dfjp_hook PROPERTIES OUTPUT_NAME "dfhooks")

# x64 Release ビルド推奨
if(MSVC)
    # /utf-8: ソースファイルとコンパイル結果を UTF-8 として扱う（日本語コメント対応）
    target_compile_options(dfjp_hook PRIVATE /utf-8 /W3 /WX-)
endif()
