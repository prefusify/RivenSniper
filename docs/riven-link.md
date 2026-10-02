# OMG 紫卡链接解码

游戏频道中的 `[OMG-…]` 链接由 `riven_link.py` 离线解析。解码读取受控的词条与武器索引，不依赖卖家文本或外部翻译服务。

## 位布局

- 每个词条块包含 5 位索引与 31 位正 float32 数值。
- 词条之后依次为 8 位不透明字段、1 位选择器、类别定宽武器索引、5 位段位要求、2 位未知字段、2 位极性、4 位等级、2 位未知字段和 10 位洗练次数。
- Archgun、Zaw、Kitgun、手枪、步枪、霰弹枪、近战的武器索引宽度分别为 5、4、3、8、8、6、9 位。

## 武器和数值映射

`data/riven_weapon_indices.json` 将客户端 Riven 类型表中的游戏路径映射为武器 slug。`_meta` 记录当前表项数量、映射数量和忽略分类。技能武器、活动对象及内部实现对象按明确规则排除。

`data/riven_values.json` 保存词条基准数值。解码结果经过 `grading.py` 处理，用于词条评级和展示。无法完整解码的链接不会作为有效紫卡推送；一条消息中的其他有效链接仍可处理。

## 校验工具

`scripts/build_riven_weapon_indices.py` 接受客户端类型表及官方英文武器导出，比较并生成映射：

```powershell
uv run python scripts/build_riven_weapon_indices.py --tables <类型表路径> --export-weapons <ExportWeapons_en.json>
```

默认仅比较，显式添加 `--write` 才写入。不能映射且不符合排除规则的对象会使生成失败。
