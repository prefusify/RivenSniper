# 第三方说明

根目录 MIT License 适用于 RivenSniper 自有源代码，不授予第三方商标、游戏图标或其他第三方作品的权利。

## 游戏与市场数据

Warframe 名称、游戏图标、词条名称和游戏内容归 Digital Extremes 等相应权利人所有。`data/` 保存程序运行使用的游戏元数据与数值映射，不包含用户运行数据库、账号凭据或聊天日志。

市场目录、统计与挂单来自 [warframe.market](https://warframe.market/)。武器元数据参考 [WFCD/warframe-items](https://github.com/WFCD/warframe-items)。紫卡基准数值资料来自 [calamity-inc/warframe-riven-info](https://github.com/calamity-inc/warframe-riven-info) 的 `riven_tags.json`；该资料不由本项目重新授予 MIT 许可。OMG 武器索引使用客户端类型表中的游戏路径及武器映射。

## WFCD/warframe-items

上游项目按 MIT License 发布。以下保留其许可证声明；该声明不替代游戏内容本身的权利归属。

```text
MIT License

Copyright (c) 2017 Kaptard

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## 运行依赖与工具

Python 依赖由 `pyproject.toml` 声明、`uv.lock` 锁定，通过 uv 单独下载；各依赖保留其原有许可证。主要依赖包括 NoneBot2、NoneBot OneBot/Discord 适配器、httpx、Pillow 和 websockets。

uv、Python、SnowLuma、Discord、Warframe 客户端、OpenSSH、Frida 和 3proxy 不随本源码包分发。`scripts/provision_wm_proxy.py` 在使用者明确执行时下载固定版本的 3proxy，并校验其 SHA-256；工具的使用及再分发适用各自条款。
