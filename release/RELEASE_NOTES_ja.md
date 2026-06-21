# DFJP リリース説明

DFJP は、Windows 版 Dwarf Fortress 向けの日本語プレイ補助ツールです。
`dfhooks.dll` でゲーム内テキストをフックし、翻訳結果をオーバーレイ表示します。
ゲーム本体を日本語化するものではなく、英語テキストの理解を補助することを目的にしています。

この配布版は Python が入っていない環境でも使えます。

このリリースの配布 ZIP には、チュートリアル付近までの手動翻訳を含んだ
`dfjp-data/manual_translation_rules.tsv` を同梱しています。

## スクリーンショット

![Dwarf Fortress と DFJP オーバーレイ表示](https://raw.githubusercontent.com/dewfalse/dwarf-fortress-jp-helper/main/screenshot_02.png)

## インストール方法

1. リリース ZIP を展開します。
2. 展開した中身を、`Dwarf Fortress.exe` があるフォルダにそのままコピーします。
3. `DFJP起動.cmd` を実行します。
4. 初回起動時は RVA を自動検出し、`dfint-data/offsets-dfjp-auto.toml` を作成します。
5. DFJP が起動したら、そのまま Dwarf Fortress を起動してください。

主な配置ファイル:

- `DFJP.exe`
- `DFJP起動.cmd`
- `dfhooks.dll`
- `README_DFJP.txt`

## 使い方

DFJP 起動後は、Dwarf Fortress の画面上に翻訳オーバーレイが表示されます。

初期状態では Ctrl キーで表示モードを切り替えます。

- `Hover`
  - マウスカーソル位置のテキストだけを翻訳表示
- `All text`
  - 画面内の検出テキスト全体に翻訳を重ねて表示
- `Off`
  - オーバーレイ表示を停止

Ctrl キーを押すたびに、次の順で循環切替します。

`Hover -> All text -> Off`

タスクトレイ操作:

- タスクトレイアイコンの左クリックまたはダブルクリック
  - オーバーレイ表示の ON / OFF を切り替え
- タスクトレイアイコンの右クリック
  - メニューを開く
- タスクトレイメニューの `Exit`
  - DFJP を終了

補足:

- 初見のテキストは翻訳が返るまで少し待つことがあります
- 翻訳中は `...` の表示になります
- Dwarf Fortress の更新で RVA が変わった場合は、次回起動時に自動再検出します

## 設定ファイル

設定ファイルは初回起動時に自動生成されます。

- `dfjp-data/config.toml`

主な設定項目:

- `translator.engine`
  - 使用する翻訳エンジン
  - `"google"` または `"deepl"`
  - デフォルト: `"google"`
- `translator.target_language`
  - 翻訳先言語コード
  - 例: `"ja"`, `"en"`, `"ko"`, `"zh-CN"`
  - デフォルト: `"ja"`
- `deepl.api_key`
  - DeepL を使う場合の API キー
- `overlay.tooltip_opacity`
  - 翻訳ツールチップの透過率
  - デフォルト: `0.78`
- `overlay.all_text_vertical_shift_ratio`
  - all text モードで重なったツールチップを縦にどれくらいずらすか
  - `0.5` = 半分ずらす / `1.0` = 完全にずらす
  - デフォルト: `1.0`
- `overlay.translation_font_size`
  - 翻訳テキストのフォントサイズ
  - デフォルト: `12.0`
- `overlay.toggle_hotkey`
  - モード切替キー
  - `"ctrl"` / `"shift"` / `"alt"`
  - デフォルト: `"ctrl"`
- `manual_rules.collect_detected_text`
  - `true` にすると、検出したテキストを `dfjp-data/manual_translation_rules.tsv` に `exact<TAB>原文<TAB>` の形で追記
  - 手動翻訳候補の収集用
  - デフォルト: `false`
- `debug.log`
  - `true` のとき `dfjp-data/debug.log` に詳細ログを出力
  - 配布時デフォルト: `true`

## 設定例

### 標準設定

```toml
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
```

### Shift キーでモード切替したい場合

```toml
[overlay]
toggle_hotkey = "shift"
```

### 日本語を少し小さめに表示したい場合

```toml
[overlay]
translation_font_size = 11.0
```

### 検出テキストを収集したい場合

```toml
[manual_rules]
collect_detected_text = true
```

## 関連ファイル

- `dfint-data/offsets-dfjp-auto.toml`
  - 自動検出した RVA
- `dfjp-data/translation_cache.json`
  - 翻訳キャッシュ
- `dfjp-data/manual_translation_rules.tsv`
  - 手動翻訳ルール（配布 ZIP ではチュートリアルまでの手動翻訳入り）
- `dfjp-data/manual_translation_rules.template.tsv`
  - `manual_translation_rules.tsv` 再生成用テンプレート
- `dfjp-data/debug.log`
  - デバッグログ

翻訳エンジンや翻訳先言語を変えたあとに古い訳文が残る場合は、`dfjp-data/translation_cache.json` を削除してください。

## ライセンス関連

- `LICENSE`
  - DFJP 本体の MIT License
- `THIRD_PARTY_LICENSES/`
  - 同梱している Python ランタイム・PySide6・各種 Python パッケージのライセンス関連ファイル
- `THIRD_PARTY_LICENSES/THIRD_PARTY_LICENSES.md`
  - 同梱ライセンス一覧

配布版の GUI ランタイムは、ライセンス整理をしやすいよう PySide6 ベースにしています。
