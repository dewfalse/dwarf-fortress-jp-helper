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

翻訳エンジンや DeepL API キーはこのファイルで設定できます。

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
