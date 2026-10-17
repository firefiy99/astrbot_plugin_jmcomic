# astrbot_plugin_jmcomic（禁漫下载助手）

> 创作者：小星萤
>
> 基于 [hect0x7/JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python)（jmcomic，MIT）封装的 AstrBot 插件，
> 在聊天中即可下载禁漫本子/章节、查询本子详情、站内搜索、查看排行榜。

## 简介

本插件把 jmcomic 的下载、查询能力包装成 AstrBot 指令，下载任务在后台异步执行，
完成后自动把结果发回发起人所在会话，不会阻塞机器人其他消息的处理。

## 功能特性

- 📥 **下载本子/章节**：支持单个、多个车号批量下载，支持 `p章节号` 语法只下载指定章节
- 🔍 **详情查询**：标题、作者、标签、人物、作品、页数、热度、章节列表一览
- 🎯 **站内搜索**：关键词搜索（支持页码），结果含车号可直接下载
- 🏆 **排行榜**：本月 / 本周 / 今日排行
- ⏳ **任务管理**：`jm list` 查看进行中任务，`jm cancel` 取消任务
- 📤 **下载后发送到 QQ**：支持 zip 压缩包、合并 PDF、逐张图片直发三种方式（默认 zip），无需登录服务器即可收到内容
- ⚙️ **配置化**：客户端实现（移动端/网页端）、下载目录、图片格式转换、并发数、代理、
  自定义域名、繁简归一化均可通过插件 WebUI 配置页调整，无需手写 option 文件
- 🔄 请求自动重试、域名自动切换（jmcomic 自带机制）

## 安装

### 方式一：AstrBot WebUI 插件管理

1. 将本插件目录打成 zip（不含 `.git`、`__pycache__`）
2. AstrBot WebUI → 插件管理 → 上传/安装 zip
3. 首次加载时 AstrBot 会自动执行 `pip install -r requirements.txt` 安装 jmcomic 及其依赖

### 方式二：直接放到服务器

```bash
# 以本插件目录为例（服务器上 AstrBot 数据目录为 /root/astrbot/data）
cp -r astrbot_plugin_jmcomic /root/astrbot/data/plugins/
# 进入容器安装依赖（容器内 Python 3.12）
docker exec astrbot pip install -U jmcomic
docker restart astrbot
```

依赖说明：`requirements.txt` 声明 `jmcomic>=2.7.9`，其依赖（commonx / curl-cffi / pillow /
pycryptodome / pyyaml）会一并安装。

## 指令说明

| 指令 | 说明 | 示例 |
| --- | --- | --- |
| `jm download 车号 [p章节] [车号...]` | 下载本子/章节（后台执行，完成后通知） | `jm download 123`、`jm download 123 456 p789` |
| `jm info 车号` | 查看本子详情（不下载） | `jm info 350234` |
| `jm search 关键词 [页码]` | 站内搜索 | `jm search 无修正` |
| `jm rank [月\|周\|日]` | 排行榜（默认本月） | `jm rank 周` |
| `jm list` | 查看进行中的下载任务 | `jm list` |
| `jm cancel 车号` | 取消包含该车号的下载任务 | `jm cancel 123` |
| `jm help` | 显示帮助 | `jm help` |

> 车号支持从任意混合文本中提取数字（jmv 风格），例如 `jm info 350谁还没看过234` 等价于 `jm info 350234`。

## 配置说明（WebUI 插件设置页）

| 配置项 | 说明 | 默认 |
| --- | --- | --- |
| client_impl | 客户端实现：`api`=移动端（不限 IP 兼容性好）；`html`=网页端（效率高但限制 IP 地区） | api |
| download_dir | 下载保存根目录，留空用 `data/plugin_data/astrbot_plugin_jmcomic/downloads` | 空 |
| image_suffix | 图片格式转换：留空保持原图；`.png`/`.jpg`/`.webp`（动图不转换） | 空 |
| thread_photo | 同时下载的章节数 | 3 |
| thread_image | 每章节同时下载的图片数 | 20 |
| retry_times | 请求失败自动重试次数 | 5 |
| proxy | HTTP 代理地址，如 `http://127.0.0.1:7890`；留空不使用 | 空 |
| domain | 自定义禁漫域名列表（逗号/换行分隔）；留空自动获取可用域名 | 空 |
| normalize_zh | 繁简归一化：`zh-cn` 转简体、`zh-tw` 转繁体、留空不转换 | 空 |
| jm_log | 是否输出 jmcomic 详细请求日志（排查问题用） | false |
| send_mode | 下载完成后如何发送到聊天：`zip`=打包压缩包发送文件；`pdf`=合并整本为 PDF 发送；`images`=逐张发送图片（慎用）；`none`=仅保存服务器并发送文本通知 | zip |
| keep_files | 发送后是否保留服务器本地文件；关闭后 zip/pdf 模式会在打包完成后删除原图文件夹 | true |
| file_server_port | 内置 HTTP 文件服务端口（供 NapCat 从 astrbot 容器拉取 zip/pdf） | 18766 |
| file_server_host | NapCat 访问 astrbot 容器使用的地址：同 docker 网络填容器名 `astrbot`；跨宿主机填服务器公网 IP | astrbot |

下载目录结构：`<download_dir>/<album_id>_<album_title>/`（规则 `Bd_Aid_Atitle`）。

## 数据目录

下载内容保存于 `data/plugin_data/astrbot_plugin_jmcomic/downloads/`（可在配置中修改），
不写入插件自身目录，插件更新/卸载不影响已下载内容。

## 常见问题

1. **提示 jmcomic 未安装**：检查插件依赖是否安装成功，可手动 `docker exec astrbot pip install -U jmcomic` 后重启。
2. **下载失败/IP 地区受限**：改用 `api`（移动端）客户端，或配置代理（如服务器所在地可访问的 HTTP 代理）。
3. **下载很慢**：适当调大 thread_photo / thread_image，注意不要过度并发给站点服务器造成压力。
4. **提示域名不可用**：留空 domain 配置让其自动获取，或手动填入当前可用域名。
5. **zip/pdf 发不到 QQ**：确认 astrbot 与 napcat 容器在同一 docker 网络（如 appnet），且 `file_server_host` 填容器名 `astrbot`（或服务器公网 IP）；插件会在下载时自动拉起内置文件服务。images 模式走 base64 直发，不受此限制。
6. **images 直发刷屏**：images 模式单任务最多发送 50 张，超出部分保留在服务器；建议改用 zip/pdf。

## 免责声明

- 本项目仅用于学习与个人研究用途，请遵守所在地区法律法规；
- 本插件仅作为工具封装，不参与任何内容的制作与分发；
- 请尊重版权，下载内容请勿用于商业或传播用途；
- 友情提示：珍爱 JM，请勿一次性批量下载大量本子，避免给站点服务器增加压力。

## 更新日志

### 2026-10-17 · v1.1.0

- 新增下载完成后的发送模式 `send_mode`：zip（默认，打包压缩包发送文件）/ pdf（合并整本为 PDF）/ images（逐张图片直发，最多 50 张）/ none（仅保存本地）；
- zip/pdf 通过 astrbot 容器内置 HTTP 文件服务（默认端口 18766）让 NapCat 拉取后发送到 QQ，无需共享目录；
- 新增配置：`keep_files`（打包后是否删除原图）、`file_server_port`、`file_server_host`；
- 依赖新增 `img2pdf`（pdf 模式所需）。

### 2026-10-17 · v1.0.0

- 初版发布：下载（多车号/单章节）、详情查询、站内搜索、排行榜、任务列表与取消；
- 全部请求走 jmcomic 异步 API；下载任务后台执行完成后自动通知；
- 提供 WebUI 配置页：客户端实现、下载目录、图片格式、并发、代理、域名、繁简转换等。
