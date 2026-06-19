DFJP 起動手順
=============

使い方
------
1. ZIP の中身を Dwarf Fortress.exe があるフォルダへ展開します。
2. DFJP.exe または「DFJP起動.cmd」を実行します。
3. 初回起動時は RVA 検出を自動実行し、dfint-data\offsets-dfjp-auto.toml を作成します。
4. 翻訳ウインドウが表示されたら、そのあと Dwarf Fortress を起動してください。

設定
----
初回起動時に次のファイルが自動生成されます。

  dfjp-data\config.toml

翻訳エンジン、翻訳先言語、DeepL API キーはこのファイルで設定できます。

主な設定項目:

- translator.engine
    "google" または "deepl"

- translator.target_language
    翻訳先言語コード
    例: ja / en / ko / zh-CN

- deepl.api_key
    DeepL を使う場合の API キー

- overlay.tooltip_opacity
    翻訳ツールチップの透過率
    1.0 に近いほど濃く、低いほど元テキストが見えやすい

- overlay.all_text_vertical_shift_ratio
    all text モードで重なったツールチップを縦にどれくらいずらすか
    0.5 = 半分ずらす / 1.0 = 完全にずらす

- overlay.toggle_hotkey
    モード切り替えホットキー
    "ctrl" / "shift" / "alt"

- debug.log
    true にすると debug.log に動作ログを出力

設定例:

Google 翻訳で日本語表示

  [translator]
  engine = "google"
  target_language = "ja"

  [deepl]
  api_key = ""

  [overlay]
  tooltip_opacity = 0.78
  all_text_vertical_shift_ratio = 1.0
  toggle_hotkey = "ctrl"

  [debug]
  log = true

Google 翻訳で英語表示

  [translator]
  engine = "google"
  target_language = "en"

DeepL で日本語表示

  [translator]
  engine = "deepl"
  target_language = "ja"

  [deepl]
  api_key = "YOUR_DEEPL_API_KEY"

all text モードで元テキストを見やすくする例

  [overlay]
  tooltip_opacity = 0.55
  all_text_vertical_shift_ratio = 1.0
  toggle_hotkey = "ctrl"

Shift キーでモード切り替えする例

  [overlay]
  toggle_hotkey = "shift"

生成されるファイル
------------------
- dfint-data\offsets-dfjp-auto.toml
    Dwarf Fortress のバージョンに対応した自動検出 RVA

- dfjp-data\translation_cache.json
    翻訳キャッシュ

- dfjp-data\debug.log
    config.toml の [debug] log = true のときだけ出力

注意
----
- Dwarf Fortress の更新で RVA が変わった場合、次回起動時に自動再検出します。
- 自動検出に失敗した場合は、DFJP.exe のエラーダイアログを確認してください。
- dfhooks.dll は Dwarf Fortress 本体フォルダに置かれている必要があります。
- 翻訳エンジンや翻訳先言語を切り替えたあとに古い訳文が残る場合は、dfjp-data\translation_cache.json を削除してください。
