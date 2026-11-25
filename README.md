# JevChat-Windows：判断模型自定义来源补丁（v0.1.12 版）

给 [JevChat-Windows](https://github.com/jev-chat/jev-chat-windows) **v0.1.12** 的 exe 补一个来源选项：
「判断 · Jev」除了 OpenRouter 和 TypeSafe 直连，还能自己填 Base URL 和密钥，跟起草模型那边的自定义一样。

> 只适用于官方 **v0.1.12** 的 `jev-chat-windows.exe`
> （sha256 `4f0b79c63cbfbb283edcac71c32c07c038089127cb7c2926c9d6a3126119fec8`，18,632,863 字节）。
> 脚本会核对，对不上就不动手。

## 和上游那个补丁仓库的关系

功能来自 [XiaoRui114514/jev-chat-windows-custom-jev](https://github.com/XiaoRui114514/jev-chat-windows-custom-jev)，
那个仓库的补丁**只做 v0.1.11**（写死了 v0.1.11 的 sha256），直接套到 v0.1.12 上会被拒绝。

也不能硬绕过去 —— v0.1.11 → v0.1.12 之间上游动了补丁覆盖的 6 个文件里的 4 个：

| 文件 | v0.1.12 的改动 |
| --- | --- |
| `app/overlay.py` | +225 −137（大改） |
| `main.py` | +22 −10 |
| `app/settings.py` | +9 −1 |
| `core/providers.py`<br>`core/jev_client.py`<br>`core/engine.py` | **完全没动** |

补丁的原理是整块替换这些模块的字节码，直接覆盖会把 v0.1.12 新增的
`app/i18n.py`（界面语言 / 韩语）、`app/chatapps.py`、`app/ocr_windows.py` 架空
—— 因为 `app/worker.py`、`app/capture.py` 还是新版，会去调已经被回退的接口。

所以这里**没有复制上游的补丁包**，而是把上游补丁的净改动
**移植到 v0.1.12 的真实源码上**，再重新编译进 exe：

- `core/providers.py`、`core/jev_client.py`、`core/engine.py`：v0.1.12 一字未改，直接取上游补丁版
- `app/settings.py`、`app/overlay.py`、`main.py`：以 v0.1.12 源码为底，只把补丁改动搬上去
  （保留 v0.1.12 自己的 `bind()` i18n 风格，界面语言切换不会把新增的 Base URL 行刷没）

## 用法

1. 下载本仓库（Code → Download ZIP）解压。
2. 双击 `安装补丁.bat`。
   - 脚本按 sha256 找官方 v0.1.12 的 `jev-chat-windows.exe`：桌面快捷方式、下载、文档目录都翻一遍。
   - 找不到就把 exe（或它所在的文件夹）拖到 `安装补丁.bat` 上。
   - 程序开着也没关系，脚本会先关掉它；原 exe 备份成 `jev-chat-windows.exe.bak`。
3. 打开程序 → 设置 → 模型 → 判断 · Jev：来源选「自定义 · OpenAI 兼容」
   → 填 Base URL（一般要带 `/v1`）→ 填这一家的密钥
   （换来源要重填一次，留空会继续用上一把）→ 点「获取模型」挑一个 → 保存设置。

还原：双击 `卸载补丁.bat`，它会从 `.bak` 拷回来。`检查补丁.bat` 只看状态不改文件。

> 因为官方 v0.1.12 的 release 只有一个 exe，所有下载到的人 sha256 都一样，
> 所以这份补丁包对任何官方 v0.1.12 安装都通用，不用自己重新构建。

## 补了什么

| 文件 | 改动 |
| --- | --- |
| `core/providers.py` | `JEV_PROVIDERS` 多两条：`custom_openai`、`custom_typesafe`；`JEV_CUSTOM` 跟着扩 |
| `core/jev_client.py` | 两种自定义来源的判断和取模型；两条手写 HTTP 合成一个 `_post_json` |
| `core/engine.py` | `analyze()` 多一个 `jev_base_url` 参数 |
| `app/settings.py` | 存/读 `config.json` 里的 `jev_base_url` |
| `app/overlay.py` | 设置页给判断这组加 Base URL 行（TypeSafe 那种提示别带 `/v1`），说明文案跟着改 |
| `main.py` | 把 `jev_base_url` 传给 `analyze()` |

改动就是这 6 个文件，`src/` 下按上游路径原样放着，其余代码没动。

## 两条自定义来源的区别

| 来源 | 接什么 | Base URL |
| --- | --- | --- |
| **自定义 · OpenAI 兼容** | 任意 chat/completions 接口 | 一般要带 `/v1` |
| **自定义 · TypeSafe 兼容** | TypeSafe 协议的镜像，比如 TeamoRouter 的 `jev` | **只到域名，别带 `/v1`**（SDK 自己拼 `/v1/systemone`） |

OpenAI 兼容那条是把 7 道判断题打包成一轮 `chat/completions` 发过去，用你自己的模型答。
**判断质量取决于那个模型读不读得懂这套题目**，不是真 Jev；个别题答不成约定格式会被丢掉，
一次判断全部失败会退回原来的老路（盲起草 + 判断排序一次问完）。
想要真 Jev，选 OpenRouter、TypeSafe 直连，或用 TypeSafe 兼容那条。

## 验证过什么

- **全量字节码校验**：解出补丁后 exe 的 PYZ，5434 个模块全部能 `marshal.loads` 反序列化，
  与原版数量一致；TOC 逐项比对 —— 5445 个模块里，**除补丁的 5 个外其余 5440 个零差异**。
- **构建可复现**：从同一个原版 exe 重新跑一遍，得到的补丁版 sha256 稳定是
  `f6d475a892e82486f82f76ef83ae1e1d18ae7ee1ebe3606030b65ce327a4b2a0`
  （18,712,150 字节）。
- **实跑**：打完的 exe 在 Windows 11 上正常启动；写 `config.json` 把判断来源设成自定义后，
  Base URL 行正确出现并读到值；判断和起草两组各有独立的 Base URL，互不干扰；
  v0.1.12 的界面语言、韩语支持都在，版本号仍是 v0.1.12。

## 限制

- **只认官方 v0.1.12 的 exe**（sha256 `4f0b79c6…`），脚本会核对，对不上就不动手。
  换版本要拿 `tools/build_patch.py` 重新生成一份。
- **程序更新到新版本后补丁就没了**（更新会覆盖 exe），重新打一遍就行；
  注意更新可能把 `.bak` 一起顶掉，重要的话先另存一份原版 exe。
- 两条界面文案改成了「三家给的是同一个 Jev，必填。」和
  「看上面选的来源：OpenRouter、TypeSafe 直连，或你自己的服务。」
  i18n 韩语表里还是按老文案（「两家…」）存的 key，所以切到韩语时这两条会显示中文。功能不受影响。

## 重新构建

需要 **Python 3.11**（exe 里是 3.11 字节码，别的版本编译出来塞不进去）：

```bash
python tools/build_patch.py --exe <官方 exe 路径> --out patch --patched-exe out/jev-chat-windows.exe --app-version v0.1.12
```

`tools/build_patch.py` 是上游补丁作者的脚本，这里只加了一个 `--app-version` 参数
（原版把 `v0.1.11` 写在代码里，不改的话 manifest 会标错版本）。

## 许可与致谢

MIT，见 [LICENSE](LICENSE) 与 [NOTICE](NOTICE)。

- 应用本体与 `src/` 的基础代码：Copyright (c) 2026 rezoch340 and the jev-chat contributors，
  以及 Finderchangchang / jev-chat-jarvis。
- 「判断模型自定义来源」这个补丁的原始实现：XiaoRui114514，
  <https://github.com/XiaoRui114514/jev-chat-windows-custom-jev>。
- 本仓库只做版本移植，功能设计不是原创。
