DFJP 使い方
===========

使い方
------
1. ZIP の中身を Dwarf Fortress.exe があるフォルダへ展開します。
2. DFJP.exe または「DFJP起動.cmd」を実行します。
3. 初回起動時は RVA を自動検出して dfint-data\offsets-dfjp-auto.toml を作成します。
4. 翻訳表示が出たら、そのまま Dwarf Fortress を起動してください。
5. 起動後は、タスクトレイアイコンのクリックで表示 ON/OFF、設定したホットキーで Hover -> All text -> Off を循環切替できます。

設定
----
設定ファイル:
  dfjp-data\config.toml

主な設定項目:

- translator.engine
    "google" または "deepl"
    デフォルトは "google"

- translator.target_language
    翻訳先言語コード
    例: ja / en / ko / zh-CN
    デフォルトは ja

- deepl.api_key
    DeepL を使う場合の API キー

- overlay.tooltip_opacity
    翻訳ツールチップの透過率
    デフォルトは 0.78

- overlay.all_text_vertical_shift_ratio
    all text モードで重なったツールチップを縦にどれくらいずらすか
    0.5 = 半分ずらす / 1.0 = 完全にずらす
    デフォルトは 1.0

- overlay.translation_font_size
    翻訳テキストのフォントサイズ
    デフォルトは 12.0

- overlay.toggle_hotkey
    オーバーレイ表示切替キー
    押すたびに Hover -> All text -> Off を切り替え
    "ctrl" / "shift" / "alt"
    デフォルトは "ctrl"

- manual_rules.collect_detected_text
    true にすると、ゲーム中に検出したテキストを
    dfjp-data\manual_translation_rules.tsv に
    exact<TAB>原文<TAB> の形で追記
    デフォルトは false

- debug.log
    true のとき debug.log に詳細ログを出力
    配布時のデフォルト設定は true

設定例
------
Google Translate で日本語表示

  [translator]
  engine = "google"
  target_language = "ja"

  [deepl]
  api_key = ""

  [overlay]
  tooltip_opacity = 0.78
  all_text_vertical_shift_ratio = 1.0
  translation_font_size = 12.0
  toggle_hotkey = "ctrl"

  [manual_rules]
  collect_detected_text = false

  [debug]
  log = true

Google Translate で英語表示

  [translator]
  engine = "google"
  target_language = "en"

DeepL で日本語表示

  [translator]
  engine = "deepl"
  target_language = "ja"

  [deepl]
  api_key = "YOUR_DEEPL_API_KEY"

all text モードを少し透けさせる例

  [overlay]
  tooltip_opacity = 0.55
  all_text_vertical_shift_ratio = 1.0
  translation_font_size = 12.0
  toggle_hotkey = "ctrl"

Shift キーで切り替える例

  [overlay]
  toggle_hotkey = "shift"

日本語を少し小さめに表示する例

  [overlay]
  translation_font_size = 11.0

検出テキストを収集する例

  [manual_rules]
  collect_detected_text = true

手動翻訳ルール
--------------
ファイル:
  dfjp-data\manual_translation_rules.tsv

形式:

- exact<TAB>source<TAB>target
- regex<TAB>pattern<TAB>replacement

補足:

- exact は完全一致
- regex は原文全体に対する正規表現
- \1 のようなキャプチャ参照が使えます
- exact の訳文を空にすると、未翻訳候補として扱われ、機械翻訳は上書きしません

例:

  exact	Start new game in existing world	既存の世界で新しいゲームを始める
  regex	^(\d+)(?:st|nd|rd|th) Slate$	\1番目のスレート
  exact	Mining (2 of 8)	

エスケープ:

- \n = 改行
- \t = タブ
- \r = 復帰
- \\ = バックスラッシュ

生成されるファイル
------------------
- dfint-data\offsets-dfjp-auto.toml
    Dwarf Fortress のバージョンに対応した自動検出 RVA

- dfjp-data\translation_cache.json
    翻訳キャッシュ

- dfjp-data\manual_translation_rules.tsv
    手動翻訳ルール

- dfjp-data\debug.log
    config.toml の [debug] log = true のときだけ出力

注意
----
- Dwarf Fortress の更新で RVA が変わった場合、次回起動時に自動再検出します。
- 自動検出に失敗した場合は、DFJP.exe のエラーダイアログを確認してください。
- 翻訳エンジンや翻訳先言語を切り替えたあとに古い訳文が残る場合は、dfjp-data\translation_cache.json を削除してください。
