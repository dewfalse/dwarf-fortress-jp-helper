# dwarf-fortress-jp-helper

Windows 用の Dwarf Fortress 日本語プレイ補助ツールです。

このプロジェクトは Dwarf Fortress 本体のテキスト描画関数をフックし、ゲーム内で表示された英語テキストを別ウインドウに集約して、日本語訳を表示します。Dwarf Fortress 本体の日本語化そのものではなく、外部の翻訳支援ウインドウを表示する補助ツールです。

## Screenshot

ゲーム画面と翻訳ウインドウを並べた動作例です。

![Dwarf Fortress and DFJP translation window](docs/screenshot_01.png)

## 主な構成

- `hook/`
  - `dfhooks.dll` をビルドする C++ コード
  - Dwarf Fortress の描画関数をフックし、取得した文字列を Named Pipe で送信
- `translator/`
  - PyQt6 ベースの翻訳ウインドウ
  - Google 翻訳 / DeepL を利用可能
- `tools/detect_offsets.py`
  - Dwarf Fortress のバージョン更新で変化する RVA を実行ファイルから自動検出
- `scripts/build_release.ps1`
  - `dfhooks.dll` と `DFJP.exe` をまとめた配布 ZIP を生成

## できること

- Dwarf Fortress の表示テキストを外部ウインドウへ転送
- フレーム中に分散して届く単語列を文単位へ近似的に再構成
- 日本語訳をリアルタイム表示
- Dwarf Fortress 更新後の RVA を自動検出
- Python がない環境向けに `DFJP.exe` を作成

## 対応環境

- Windows
- Dwarf Fortress Steam 版系統

## 使い方

配布 ZIP を使う場合:

1. ZIP の中身を `Dwarf Fortress.exe` があるフォルダへ展開
2. `DFJP.exe` または `DFJP起動.cmd` を実行
3. 初回起動時に `dfint-data/offsets-dfjp-auto.toml` を自動生成
4. 翻訳ウインドウが表示されたら Dwarf Fortress を起動

## 設定ファイル

配布版では初回起動時に `dfjp-data/config.toml` が自動生成されます。
ソースから実行する場合は `translator/config.toml` を編集してください。

主な設定項目:

- `translator.engine`
  - 使用する翻訳エンジン
  - `"google"` または `"deepl"`
- `translator.target_language`
  - 翻訳先言語コード
  - 例: `ja`, `en`, `ko`, `zh-CN`
- `deepl.api_key`
  - DeepL を使う場合の API キー
- `overlay.tooltip_opacity`
  - 翻訳ツールチップの透過率
  - `1.0` に近いほど濃く、低いほど元テキストが見えやすい
- `overlay.all_text_vertical_shift_ratio`
  - all text モードで重なったツールチップを縦にどれくらいずらすか
  - `0.5` = 半分ずらす / `1.0` = 完全にずらす
- `debug.log`
  - `true` のとき、受信テキストや動作ログを `debug.log` に出力

設定例:

Google 翻訳で日本語表示:

```toml
[translator]
engine = "google"
target_language = "ja"

[deepl]
api_key = ""

[overlay]
tooltip_opacity = 0.78
all_text_vertical_shift_ratio = 1.0

[debug]
log = true
```

Google 翻訳で英語表示:

```toml
[translator]
engine = "google"
target_language = "en"
```

DeepL で日本語表示:

```toml
[translator]
engine = "deepl"
target_language = "ja"

[deepl]
api_key = "YOUR_DEEPL_API_KEY"
```

all text モードで元テキストを見やすくする例:

```toml
[overlay]
tooltip_opacity = 0.55
all_text_vertical_shift_ratio = 1.0
```

言語や翻訳エンジンを切り替えたあとに古い翻訳が残る場合は、翻訳キャッシュを削除してください。

- 配布版: `dfjp-data/translation_cache.json`
- ソース実行時: `translator/translation_cache.json`

## ソースからの開発

Python 側:

```powershell
cd translator
uv sync
uv run python main.py
```

RVA 自動検出:

```powershell
python tools\detect_offsets.py "C:\path\to\Dwarf Fortress.exe" --output dfint-data\offsets.toml
```

## リリースビルド

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_release.ps1
```

生成物:

- `dist/DFJP.zip`

## License

MIT License

## 注意

- `dfhooks.dll` を使うため、同名 DLL を利用する他ツールとは競合する場合があります。
- 本プロジェクトは外部翻訳補助ツールであり、ゲーム本体の描画を直接日本語化するものではありません。
